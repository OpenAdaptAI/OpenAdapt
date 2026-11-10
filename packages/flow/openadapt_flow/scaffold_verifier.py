"""Operator tooling: draft effect-oracle scaffolds + plain-language run reads.

Two read-first operator helpers live here.

``flow scaffold-verifier <recording|bundle>``
    First tries to BUILD a working record check from the reads the app made
    after the save (the recording's network capture, ``--db`` or
    ``--verifier``), fault-tests it, and writes ``record_check.proposed.yaml``
    plus ``record_check.proof.json``
    (:mod:`openadapt_flow.record_check_synthesis`). A check that misses an
    injected bad write is written as a draft with the misses listed. Nothing
    is approved automatically.

    When no read returns the saved record, it falls back to the older path:
    it reads the retained action/effect evidence in a recording directory or a
    compiled bundle and emits a DRAFT effect-oracle contract
    (``effect_contract.yaml``) pre-filled from the demonstration's write-shaped
    steps: a proposed system-of-record read, expected postcondition fields with
    explicit ``TODO`` markers, and a deployment ``effects:`` section skeleton
    matching ``docs/EFFECT_KIT.md`` and the shipped
    :class:`~openadapt_flow.runtime.effects.RestRecordVerifier` conventions.

    The output is explicitly a DRAFT requiring human edit. Nothing is
    auto-approved: the file carries a loud header, every observed value that
    needs operator confirmation is marked ``TODO``, and the command prints the
    review + qualification commands next. A demonstration with no consequential
    (write-shaped) step is REFUSED rather than scaffolded.

``flow explain <run-dir>``
    Pure read-only plain-language outcome summary over a completed run's
    artifacts (``report.json``, ``REPORT.md``, ``receipt.json``): what
    happened, why the outcome is the safe one (which check fired for a HALT),
    and the exact suggested next command (re-run, pair an oracle via
    ``scaffold-verifier``, or qualify).

Neither helper executes a workflow or weakens any gate. Neither contacts a
network, except ``scaffold-verifier --probe-reads``, which sends read-only
``GET`` requests to a copy of the app on the local machine
(:mod:`openadapt_flow.read_discovery`).
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from openadapt_flow import artifacts as _artifacts
from openadapt_flow import sealed_set as _sealed
from openadapt_flow.runtime.effects.effect import DEFAULT_IDEMPOTENCY_KEY_FIELD

BUNDLE_MARKERS = ("workflow.json", "workflow.json.enc")
RECORDING_MARKER = "events.jsonl"

#: The draft file this module writes. Deliberately YAML: it is the shape an
#: operator copies into ``deployment.yaml`` (``effects:``) and reviews by hand.
CONTRACT_FILENAME = "effect_contract.yaml"
#: What ``--probe-reads`` tried when no probe confirmed a read.
READ_DISCOVERY_FILE = "read_discovery.json"

if TYPE_CHECKING:
    from openadapt_flow.record_check_proof import Fixture


class ScaffoldRefused(SystemExit):
    """The input has nothing to scaffold from; refuse with a clear message."""


NO_WRITE_REFUSAL = (
    "scaffold-verifier REFUSED: the demonstration has no consequential "
    "(write-shaped) step to verify. An effect oracle asserts that a "
    "WRITE landed in a system of record; this input shows only "
    "read/navigation actions, so there is nothing to scaffold."
)


# ---------------------------------------------------------------------------
# Target inspection (recording dir vs bundle dir)
# ---------------------------------------------------------------------------


def classify_target(path: Path) -> str:
    """Return ``"bundle"`` or ``"recording"`` for an existing artifact path."""
    if not path.exists():
        raise ScaffoldRefused(f"scaffold-verifier: path not found: {path}")
    if _sealed.is_sealed(path):
        return _sealed.header_kind(path)
    if any((path / marker).is_file() for marker in BUNDLE_MARKERS):
        return "bundle"
    if (path / RECORDING_MARKER).is_file():
        return "recording"
    raise ScaffoldRefused(
        f"scaffold-verifier: {path} is neither a workflow bundle "
        f"(workflow.json) nor a recording directory ({RECORDING_MARKER})"
    )


# ---------------------------------------------------------------------------
# Write-step candidates from retained evidence
# ---------------------------------------------------------------------------


@dataclass
class WriteCandidate:
    """One write-shaped step observed in the retained evidence."""

    step_id: str
    action: str
    #: Human-readable target text the write shape was inferred from.
    label: str
    #: Why this step is consequential (mirrors risk.py's honest explanations).
    basis: str
    #: Identity selector for the intended record (observed or compiled).
    match: dict[str, str] = field(default_factory=dict)
    #: Expected post-write field values (observed payload or bound params).
    payload: dict[str, str] = field(default_factory=dict)
    #: Observed idempotency key (field, value) when the record carried one.
    idempotency: Optional[tuple[str, str]] = None

    @property
    def observed_delta(self) -> bool:
        return bool(self.match or self.payload)


def _event_text(event: dict[str, Any]) -> str:
    """Every piece of retained human-readable target text on one event."""
    parts = [str(event.get("text") or "")]
    structural = event.get("structural")
    if isinstance(structural, dict):
        parts.append(str(structural.get("name") or ""))
    identity = event.get("structured_identity")
    if isinstance(identity, str):
        parts.append(identity)
    parts.append(str(event.get("field_label") or ""))
    return " ".join(part for part in parts if part)


def _sor_delta(event: dict[str, Any]) -> tuple[Optional[list], Optional[list]]:
    """The captured before/after system-of-record snapshots, when present."""
    from openadapt_flow.compiler.effect_mining import (
        SOR_AFTER_KEY,
        SOR_BEFORE_KEY,
        _as_records,
    )

    return (
        _as_records(event.get(SOR_BEFORE_KEY)),
        _as_records(event.get(SOR_AFTER_KEY)),
    )


def _record_keys(records: list) -> list[str]:
    """Order-free content keys of a snapshot, for a before/after comparison."""
    import json

    return sorted(json.dumps(r, sort_keys=True, default=str) for r in records)


def _demonstrated_param_values(recording_dir: Path) -> tuple[str, ...]:
    """The demonstration's parameter values (from ``meta.json``), if any."""
    import json

    try:
        meta = json.loads(_sealed.read_text(recording_dir / "meta.json"))
    except (OSError, json.JSONDecodeError):
        return ()
    params = meta.get("params")
    if not isinstance(params, dict):
        return ()
    return tuple(str(value) for value in params.values() if value)


def _candidate_from_record(
    step_id: str,
    action: str,
    label: str,
    record: dict[str, Any],
    *,
    demonstrated_values: tuple[str, ...],
    idempotency_key_field: Optional[str] = DEFAULT_IDEMPOTENCY_KEY_FIELD,
) -> WriteCandidate:
    """Split one observed new record into identity vs payload fields.

    Uses the compiler miner's own helpers so the draft splits identity from
    payload exactly as a real mined contract would (same surrogate-id
    exclusion, same selector rules, same demonstrated-value payload split).
    """
    from openadapt_flow.compiler.effect_mining import (
        _match_selector,
        _param_value_fields,
    )

    payload = _param_value_fields(record, demonstrated_values)
    selector = _match_selector(record, set(payload))
    idempotency = None
    # The app's at-most-once key field, when the caller names one.
    raw_key = record.get(idempotency_key_field) if idempotency_key_field else None
    if idempotency_key_field and raw_key not in (None, ""):
        idempotency = (idempotency_key_field, str(raw_key))
        # The at-most-once key is surfaced separately (it must be bound to a
        # per-run param, never the frozen demo literal), so it never doubles
        # as an identity selector.
        selector.pop(idempotency_key_field, None)
    return WriteCandidate(
        step_id=step_id,
        action=action,
        label=label,
        basis="observed system-of-record delta (one new record)",
        match=selector,
        payload=payload,
        idempotency=idempotency,
    )


_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _origin_of(url: str) -> str:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower() if parts.netloc else ""


def _mutating_requests_by_event(
    recording_dir: Path, times: dict[int, float]
) -> dict[int, list[dict[str, Any]]]:
    """Event index -> the mutating requests the app's page sent for it.

    A request counts when its method is POST, PUT, PATCH or DELETE, the
    server answered it (it did not fail in transit), and it went to the app's
    own origin (the origin of the page that sent it). An event's ``t`` is
    stamped after the screen settles, so a request belongs to the first event
    stamped at or after it; one sent after the last event belongs to the last.
    """

    from openadapt_flow import record_check_synthesis as rcs

    try:
        network = rcs.load_network(recording_dir)
    except rcs.SynthesisError:
        return {}
    if not network or not times:
        return {}
    ordered = sorted(times.items(), key=lambda item: item[1])
    out: dict[int, list[dict[str, Any]]] = {}
    for line in network:
        method = str(line.get("method") or "").upper()
        if method not in _MUTATING_METHODS or line.get("failed"):
            continue
        if not isinstance(line.get("status"), int):
            continue
        app = _origin_of(str(line.get("frame_url") or ""))
        if not app or _origin_of(str(line.get("url") or "")) != app:
            continue
        t = line.get("t")
        if not isinstance(t, (int, float)):
            continue
        owner = next((i for i, et in ordered if et >= float(t)), ordered[-1][0])
        out.setdefault(owner, []).append(line)
    return out


def _candidate_from_request(
    step_id: str,
    action: str,
    label: str,
    requests: list[dict[str, Any]],
    *,
    demonstrated_values: tuple[str, ...],
) -> WriteCandidate:
    """A write observed on the wire: the request and the values it sent."""
    from openadapt_flow import record_check_synthesis as rcs

    shown = ", ".join(
        f"{str(line.get('method')).upper()} "
        f"{rcs._templated(str(line.get('url') or ''), {}).safe}"
        for line in requests[:3]
    )
    payload: dict[str, str] = {}
    for line in requests:
        for key, value in rcs._write_values(
            line, excluded_values=demonstrated_values
        ).items():
            if value in demonstrated_values:
                payload.setdefault(key, value)
    return WriteCandidate(
        step_id=step_id,
        action=action,
        label=label,
        basis=f"observed mutating request to the app ({shown})",
        payload=payload,
    )


def candidates_from_recording(
    recording_dir: Path,
    *,
    idempotency_key_field: Optional[str] = DEFAULT_IDEMPOTENCY_KEY_FIELD,
) -> list[WriteCandidate]:
    """Write-shaped candidates straight from ``events.jsonl`` evidence.

    An event is a write when any of these holds, strongest first: its
    before/after system-of-record snapshots differ; the network capture
    shows the app's page sending a mutating request (POST, PUT, PATCH or
    DELETE to its own origin) for it; its control label is write-shaped
    (:func:`openadapt_flow.risk.is_write_shaped`); or it is an Enter key.
    """
    import json

    from openadapt_flow.risk import is_write_shaped

    events_path = recording_dir / RECORDING_MARKER
    try:
        lines = _sealed.read_text(events_path).splitlines()
    except OSError as exc:
        raise ScaffoldRefused(
            f"scaffold-verifier: could not read {events_path}: {exc}"
        ) from exc

    # Demonstrated parameter values separate a record's PAYLOAD fields (the
    # typed note -> postcondition read-backs) from its identity fields (the
    # match selector), exactly as the compiler's miner does.
    demonstrated_values = _demonstrated_param_values(recording_dir)

    events: list[tuple[int, dict[str, Any]]] = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ScaffoldRefused(
                f"scaffold-verifier: {events_path} line {index + 1} is not JSON: {exc}"
            ) from exc
        if isinstance(event, dict):
            events.append((index, event))
    wire = _mutating_requests_by_event(
        recording_dir,
        {
            index: float(event["t"])
            for index, event in events
            if isinstance(event.get("t"), (int, float))
            and not isinstance(event.get("t"), bool)
        },
    )

    candidates: list[WriteCandidate] = []
    for index, event in events:
        kind = str(event.get("kind") or "")
        step_id = f"step_{index:03d}"  # positional, matching the compiler
        label = _event_text(event)

        # Strongest evidence first: a captured system-of-record snapshot whose
        # after-state holds exactly one NEW record -- the demonstration itself
        # observed the write land.
        before, after = _sor_delta(event)
        if before is not None and after is not None:
            from openadapt_flow.compiler.effect_mining import _new_records

            new_records = _new_records(before, after)
            if len(new_records) == 1:
                candidates.append(
                    _candidate_from_record(
                        step_id,
                        kind,
                        label,
                        new_records[0],
                        demonstrated_values=demonstrated_values,
                        idempotency_key_field=idempotency_key_field,
                    )
                )
                continue
            if _record_keys(before) != _record_keys(after):
                candidates.append(
                    WriteCandidate(
                        step_id=step_id,
                        action=kind,
                        label=label,
                        basis=(
                            "observed system-of-record delta "
                            f"({len(new_records)} new record(s), "
                            f"{len(before)} before, {len(after)} after)"
                        ),
                    )
                )
                continue

        if index in wire:
            candidates.append(
                _candidate_from_request(
                    step_id,
                    kind,
                    label,
                    wire[index],
                    demonstrated_values=demonstrated_values,
                )
            )
            continue

        pointer_write = kind in {"click", "double_click", "right_click"} and (
            is_write_shaped(label)
        )
        submit_key = kind == "key" and (event.get("key") or "").lower() in {
            "enter",
            "return",
        }
        if pointer_write or submit_key:
            basis = (
                "write-shaped control label"
                if pointer_write
                else "submission key with no system-of-record snapshot"
            )
            candidates.append(
                WriteCandidate(step_id=step_id, action=kind, label=label, basis=basis)
            )
    return candidates


def candidates_from_bundle(bundle_dir: Path) -> list[WriteCandidate]:
    """Write-shaped candidates from a compiled bundle's typed steps/effects."""
    from openadapt_flow.ir import ActionKind, Workflow

    workflow = Workflow.load(bundle_dir)
    candidates: list[WriteCandidate] = []
    for step in workflow.steps:
        real_effects = [
            effect for effect in step.effects if not effect.needs_operator_confirmation
        ]
        if not real_effects and step.risk != "irreversible":
            continue
        candidate = WriteCandidate(
            step_id=step.id,
            action=step.action.value,
            label=step.intent,
            basis=(
                "compiled effect contract"
                if real_effects
                else "compile-time risk classification (consequential write)"
            ),
        )
        for effect in real_effects:
            if effect.kind.value == "record_written":
                for key, expr in effect.match.items():
                    candidate.match[key] = (
                        f"{{param: {expr.param}}}" if expr.param else str(expr.literal)
                    )
            elif effect.kind.value == "field_equals" and effect.value is not None:
                key = effect.field or "TODO-field"
                candidate.payload[key] = (
                    f"{{param: {effect.value.param}}}"
                    if effect.value.param
                    else str(effect.value.literal)
                )
        if step.action is ActionKind.TYPE and step.param:
            name = step.field_label or step.param
            candidate.payload[name] = f"{{param: {step.param}}}"
        candidates.append(candidate)
    return candidates


# ---------------------------------------------------------------------------
# Draft rendering
# ---------------------------------------------------------------------------

_DRAFT_HEADER = """\
# OPENADAPT EFFECT-ORACLE DRAFT -- REQUIRES HUMAN EDIT BEFORE USE
#
# Generated by `openadapt-flow scaffold-verifier` from the demonstration
# evidence below. This file is a STARTING POINT, not an approved contract:
#   * resolve EVERY `TODO` against the application's REAL system of record
#     (a person who knows the app must do this),
#   * copy the `deployment.effects` section into your deployment.yaml,
#   * re-run lint/certify and qualify the bundle before any governed run.
# Nothing here is auto-approved, and a draft that is never edited, wired, and
# qualified verifies nothing.
"""


def _yaml_scalar(value: str) -> str:
    """Quote a scalar only when YAML would otherwise misread it."""
    special = ":{}[],&*#?|-<>=!%@`\"'\n"
    if value == "" or any(character in value for character in special):
        return repr(value)
    return value


def render_draft_yaml(
    *,
    source: Path,
    source_kind: str,
    workflow_name: str,
    candidates: list[WriteCandidate],
) -> str:
    """Render the draft contract: deterministic, commented, TODO-marked."""
    lines: list[str] = [_DRAFT_HEADER.rstrip()]
    lines += [
        "",
        f"workflow: {_yaml_scalar(workflow_name)}",
        f"source: {_yaml_scalar(str(source))}",
        f"source_kind: {source_kind}",
        "draft_status: requires-human-edit",
        "",
        "# The write-shaped steps retained by the demonstration.",
        "steps:",
    ]
    for candidate in candidates:
        lines.append(f"  - step_id: {candidate.step_id}")
        lines.append(f"    action: {candidate.action}")
        lines.append(f"    evidence_basis: {_yaml_scalar(candidate.basis)}")
        if candidate.label:
            lines.append(f"    label: {_yaml_scalar(candidate.label[:120])}")
    lines += [
        "",
        "# Proposed per-step effect contracts (what the oracle must prove).",
        "effects:",
    ]
    for candidate in candidates:
        lines += [
            f"  - step_id: {candidate.step_id}",
            "    kind: record_written",
            "    expected_count: 1  # at-most-once for this write",
        ]
        lines.extend(_effect_block_lines(candidate))
    lines += [
        "",
        "# Deployment wiring: copy into your deployment.yaml (docs/EFFECT_KIT.md).",
        "deployment:",
        "  effects:",
        "    kind: rest",
        "    base_url: TODO-system-of-record-base-url",
        "    records_path: TODO-path-that-returns-the-records-document",
        "    records_key: records  # TODO key holding the records list",
        "    # auth names ENV VARS, never credential literals:",
        "    # auth:",
        "    #   bearer_env: SOR_BEARER_TOKEN",
        "",
    ]
    return "\n".join(lines)


def _effect_block_lines(candidate: WriteCandidate) -> list[str]:
    """The per-step proposed contract block for one write candidate."""
    lines: list[str] = []
    if not candidate.observed_delta:
        return lines + [
            f"    # Evidence basis: {candidate.basis}; no system-of-record",
            "    # snapshot was captured for this step, so a human must bind it.",
            "    match: {}  # TODO bind the intended record in the system of record",
            "    postconditions: {}  # TODO expected post-write field values",
        ]
    lines.append("    match:")
    if candidate.match:
        for key, value in candidate.match.items():
            lines.append(
                f"      {_yaml_scalar(key)}: {_yaml_scalar(value)}  # TODO confirm"
            )
    else:
        lines.append(
            "      TODO: stable-identifier  # fields that identify the intended"
            " record across runs"
        )
    lines.append("    postconditions:")
    if candidate.payload:
        for key, value in candidate.payload.items():
            shown = value if value.startswith("{param:") else _yaml_scalar(value)
            lines.append(f"      {_yaml_scalar(key)}: {shown}  # TODO confirm")
    else:
        lines.append("      TODO-field: TODO-value")
    if candidate.idempotency:
        key_field, observed = candidate.idempotency
        lines.append(
            f"    idempotency_key_field: {key_field}  # observed at-most-once key"
        )
        lines.append(
            "    # bind its value to a run param (--param), never the frozen"
            f" demo literal {observed!r}"
        )
    return lines


NEXT_COMMANDS_TEMPLATE = """\
Draft written to {out} ({count} write-shaped step(s))

This is a DRAFT oracle, not an approved contract: it verifies nothing until a
person resolves every TODO, wires the deployment section, and qualifies the
bundle.

Next commands:
  1. edit {out}
  2. openadapt-flow lint <bundle-dir>
  3. openadapt-flow certify <bundle-dir> --policy <policy>
  4. openadapt-flow qualify init <bundle-dir> --target <surface> ...
"""


def write_draft(source: Path, out_dir: Optional[Path] = None) -> tuple[Path, int]:
    """Build and write the draft contract; return ``(path, candidate_count)``."""
    source_kind = classify_target(source)
    if source_kind == "bundle":
        from openadapt_flow.ir import Workflow

        workflow_name = Workflow.load(source).name
        candidates = candidates_from_bundle(source)
    else:
        import json

        try:
            meta = json.loads(_sealed.read_text(source / "meta.json"))
        except (OSError, json.JSONDecodeError):
            meta = {}
        workflow_name = str(meta.get("app_url") or source.name)
        candidates = candidates_from_recording(source)

    if not candidates:
        raise ScaffoldRefused(NO_WRITE_REFUSAL)

    text = render_draft_yaml(
        source=source,
        source_kind=source_kind,
        workflow_name=workflow_name,
        candidates=candidates,
    )
    _refuse_plaintext_output(source, out_dir)
    destination = (out_dir or source) / CONTRACT_FILENAME
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Sealed in memory when the destination is an encrypted bundle/recording.
    _sealed.write_text(destination, text)
    return destination, len(candidates)


def _refuse_plaintext_output(source: Path, out_dir: Optional[Path]) -> None:
    """An encrypted source keeps its drafts inside its own sealed set."""
    if (
        out_dir is not None
        and _sealed.is_sealed_path(source)
        and not _sealed.is_sealed_path(out_dir)
    ):
        raise ScaffoldRefused(
            "scaffold-verifier: the source is encrypted, so its drafts are "
            "written inside it, encrypted. Drop --out; nothing was written."
        )


# ---------------------------------------------------------------------------
# A working record check from the reads the app made
# ---------------------------------------------------------------------------


@dataclass
class RecordCheckResult:
    """What :func:`build_record_check` wrote and said."""

    working: bool
    check_path: Path
    proof_path: Path
    summary: str
    identity_path: Optional[Path] = None


def build_record_check(
    source: Path,
    out_dir: Optional[Path] = None,
    *,
    db: Optional[Path] = None,
    verifier: Optional[Path] = None,
    prove: Optional[str] = None,
    proof_source: Optional[Fixture] = None,
    recording: Optional[Path] = None,
    bundle: Optional[Path] = None,
    complete_read: bool = False,
    fault_gate: bool = False,
    probe_reads: bool = False,
    probe_base: Optional[str] = None,
) -> Optional[RecordCheckResult]:
    """Build, fault-test and write a proposed record check.

    ``source`` is a recording or a bundle; the other one may be given with
    ``recording`` / ``bundle`` (a bundle alone holds no reads, and identity
    coverage needs both). Returns ``None`` when no read returns the saved
    record, so the caller can fall back to the TODO draft.

    ``fault_gate`` with ``db`` runs the fault gate on a copy of the database
    (``openadapt_flow/fault_gate.py``): the faults are written to the copy and
    the check reads them through its own read. A fault it misses keeps the
    check a draft. Either way ``check_coverage.json`` is written.

    ``probe_reads``: when the recording holds no read of the saved record,
    probe the reads its write implies with ``GET`` on the local machine
    (``probe_base`` or the recorded origin, which must be local). A read is
    used only when a probe confirms the written values. Otherwise
    ``read_discovery.json`` records every probe and the read stays
    unavailable (``None`` is returned, for the TODO draft).
    """

    import tempfile

    from openadapt_flow.record_check_proof import ProofUnavailable, require_fixture

    if prove is not None:
        try:
            require_fixture(prove, proof_source)
        except ProofUnavailable as exc:
            raise ScaffoldRefused(f"scaffold-verifier --prove: {exc}") from exc
    elif proof_source is not None:
        raise ScaffoldRefused("scaffold-verifier: a proof source requires --prove")

    from openadapt_flow import record_check_synthesis as rcs

    kind = classify_target(source)
    recording_dir = source if kind == "recording" else recording
    bundle_dir = source if kind == "bundle" else bundle
    if recording_dir is None:
        return None
    for sealed_input in (recording_dir, bundle_dir):
        if sealed_input is not None and _sealed.is_sealed_path(sealed_input):
            _refuse_plaintext_output(sealed_input, out_dir)
            if prove:
                raise ScaffoldRefused(
                    "scaffold-verifier --prove is not available for encrypted "
                    "input yet (the live proof uses a temporary directory). "
                    "Nothing was written."
                )
    writes = (
        candidates_from_bundle(bundle_dir)
        if kind == "bundle" and bundle_dir is not None
        else candidates_from_recording(recording_dir)
    )
    if not writes:
        raise ScaffoldRefused(NO_WRITE_REFUSAL)
    section = None
    if verifier is not None:
        import yaml

        try:
            loaded = yaml.safe_load(Path(verifier).read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise ScaffoldRefused(f"scaffold-verifier: cannot read {verifier}: {exc}")
        section = loaded.get("verifier") if isinstance(loaded, dict) else None
        if not isinstance(section, dict):
            raise ScaffoldRefused(
                f"scaffold-verifier: {verifier} has no `verifier:` section"
            )
    try:
        proposal = rcs.synthesize(
            recording_dir,
            db_path=db,
            verifier=section,
            complete_read=complete_read,
            fault_gate=fault_gate,
            probe_reads=probe_reads,
            probe_base=probe_base,
        )
    except rcs.SynthesisError as exc:
        if db is None and section is None:
            return None
        raise ScaffoldRefused(f"scaffold-verifier: {exc}") from exc
    if not proposal.effects:
        if db is not None or section is not None:
            raise ScaffoldRefused(
                "scaffold-verifier: no read returns a record holding the "
                "recorded values: " + "; ".join(proposal.search.notes or ["none"])
            )
        if proposal.search.read_discovery is not None:
            import json

            _refuse_plaintext_output(recording_dir, out_dir)
            destination = out_dir or recording_dir
            destination.mkdir(parents=True, exist_ok=True)
            _sealed.write_text(
                destination / READ_DISCOVERY_FILE,
                json.dumps(proposal.search.read_discovery, indent=2, sort_keys=True)
                + "\n",
            )
        return None
    if proposal.refused:
        import json

        _refuse_plaintext_output(recording_dir, out_dir)
        destination = out_dir or recording_dir
        destination.mkdir(parents=True, exist_ok=True)
        if proposal.search.read_discovery is not None:
            _sealed.write_text(
                destination / READ_DISCOVERY_FILE,
                json.dumps(proposal.search.read_discovery, indent=2, sort_keys=True)
                + "\n",
            )
        # The proof, without the check: why nothing runnable was written.
        _sealed.write_text(
            destination / rcs.PROOF_NAME,
            json.dumps(proposal.proof(), indent=2, sort_keys=True, default=str) + "\n",
        )
        return None
    if prove:
        from openadapt_flow.record_check_proof import prove_live

        try:
            with tempfile.TemporaryDirectory(prefix="record-check-proof-") as tmp:
                proposal.live = prove_live(
                    prove,
                    proposal.verifier,
                    proposal.effects,
                    proposal.params,
                    write_requests=proposal.search.write_requests,
                    work_dir=Path(tmp),
                    proof_source=proof_source,
                )
        except ProofUnavailable as exc:
            raise ScaffoldRefused(f"scaffold-verifier --prove: {exc}") from exc
    destination = out_dir or recording_dir
    identity_path = None
    if bundle_dir is not None:
        from openadapt_flow import record_check_identity

        report, proposals = record_check_identity.identity_coverage(
            bundle_dir, recording_dir, proposal.params
        )
        proposal.identity = report
        destination.mkdir(parents=True, exist_ok=True)
        identity_path = record_check_identity.write_identity_proposals(
            proposals, destination
        )
    check, proof = rcs.write(proposal, destination)
    return RecordCheckResult(
        working=proposal.working,
        check_path=check,
        proof_path=proof,
        summary=rcs.summary(proposal, check, proof),
        identity_path=identity_path,
    )


def report_draft_fallback(
    source: Path, out_dir: Optional[Path], *, probe_reads: bool = False
) -> None:
    """Print why build_record_check fell back to the TODO draft."""
    destination = out_dir or source
    if probe_reads:
        import json as _json

        found = destination / READ_DISCOVERY_FILE
        status, reason = "unavailable", ""
        try:
            data = _json.loads(_sealed.read_text(found))
            status, reason = str(data.get("status")), str(data.get("reason") or "")
        except (OSError, ValueError, _sealed.SealedSetError):
            pass
        if status == "refused":
            print(
                "Read discovery: REFUSED. A probed read confirmed the written "
                f"values but can't see the whole save, so no check was written: "
                f"{reason} ({found})."
            )
        else:
            print(
                "Read discovery: UNAVAILABLE. No probed read confirmed the "
                "written values, so no read was proposed; a person must name "
                f"one ({found})."
            )
    else:
        from openadapt_flow.record_check_synthesis import PROOF_NAME

        proof_path = destination / PROOF_NAME
        try:
            import json as _json

            proof_data = _json.loads(_sealed.read_text(proof_path))
            if proof_data.get("status") == "refused":
                reason = str(proof_data.get("refusal_reason") or "")
                print(
                    "Record check: REFUSED. A recorded read can't see the "
                    f"whole save, so no check was written: {reason} ({proof_path})."
                )
        except (OSError, ValueError, _sealed.SealedSetError):
            pass


# ---------------------------------------------------------------------------
# flow explain: read-only plain-language run summary
# ---------------------------------------------------------------------------


def _halt_check_line(report: Any) -> str:
    """Name the exact check that fired for a halt, from retained evidence."""
    for result in report.results:
        if result.effect_verified is False:
            return (
                f"the independent effect check on step `{result.step_id}` "
                "REFUTED the declared write against the system of record"
            )
    for result in report.results:
        identity = result.identity
        if identity is not None and identity.status == "mismatch":
            return (
                f"the identity gate on step `{result.step_id}` detected a target "
                "DIFFERENT from the demonstrated one"
            )
    for result in report.results:
        if result.postconditions_ok is False and not result.skipped:
            return (
                f"the screen postcondition on step `{result.step_id}` did not "
                "hold after the action"
            )
    halt = getattr(report, "halt", None)
    if halt is not None and halt.reason:
        return f"the engine halted: {halt.reason}"
    return "a governed check refused to let an unproven step claim success"


def _pending_decision(run_dir: Path) -> tuple[Optional[Path], Optional[str]]:
    """The run's durable pause file and its status, when one exists.

    Read-only. An encrypted pause that can't be opened here still counts as a
    pause; its status is then unknown (``None``).
    """
    from openadapt_flow.runtime.durable.checkpoint import (
        ENC_SUFFIX,
        PENDING_FILENAME,
    )

    plain_path = run_dir / PENDING_FILENAME
    encrypted_path = run_dir / (PENDING_FILENAME + ENC_SUFFIX)
    path = (
        plain_path
        if _artifacts.is_file(plain_path)
        else encrypted_path
        if _artifacts.is_file(encrypted_path)
        else None
    )
    if path is None:
        return None, None
    try:
        from openadapt_flow import crypto
        from openadapt_flow.runtime.durable.checkpoint import CheckpointStore

        pending = CheckpointStore(run_dir, key=crypto.resolve_key(None)).read_pending()
    except Exception:  # noqa: BLE001 - an unreadable pause only hides its status
        return path, None
    return path, (pending.status if pending is not None else None)


def explain_run(run_dir: Path) -> str:
    """Read-only plain-language summary of one completed run directory.

    The first line is the plain result, and the next step follows the run's
    ``transaction_outcome``, never the coarse ``HALTED`` label alone: only an
    outcome that proves no business effect suggests re-running the same
    command. A run that may have written (``RECONCILIATION_REQUIRED``, a
    rollback, or a legacy report with no transaction outcome) asks a person to
    check the record first.
    """
    from openadapt_flow.cli_hints import command
    from openadapt_flow.ir import RunReport
    from openadapt_flow.plain_outcome import plain_result

    report_path = run_dir / "report.json"
    if not _artifacts.is_file(report_path):
        raise SystemExit(
            f"explain: {run_dir} holds no report.json -- nothing to explain "
            "(is this a completed run directory?)"
        )
    try:
        report = RunReport.model_validate_json(_artifacts.read_text(report_path))
    except (OSError, ValueError) as exc:
        raise SystemExit(
            f"explain: {report_path} could not be read as a run report: {exc}"
        ) from exc
    outcome = report.execution_outcome or ("success" if report.success else "FAILED")
    plain = plain_result(report.transaction_outcome, outcome)
    executed = sum(1 for result in report.results if not result.skipped)
    ok_steps = sum(1 for result in report.results if result.ok)
    transaction = report.transaction_outcome or "not recorded"
    report_md = run_dir / "REPORT.md"

    lines = [
        f"Result: {plain.label}. {plain.meaning}",
        f"What happened: run '{report.workflow_name}' finished {outcome} "
        f"(transaction: {transaction}): "
        f"{ok_steps}/{len(report.results)} steps ok ({executed} executed), "
        f"{report.heal_count} heal(s), model calls {report.model_calls}.",
    ]

    # Why: what the engine's own evidence says about this ending.
    receipt = run_dir / "receipt.json"
    if outcome == "VERIFIED":
        lines.append(
            "Why this is safe: every declared effect was independently confirmed "
            "in a system of record before the run claimed success."
        )
        if _artifacts.is_file(receipt):
            lines.append(f"Shareable receipt: {receipt}")
    elif outcome == "HALTED":
        lines.append(f"Why this is safe: {_halt_check_line(report)}.")
        lines.append(
            "The engine stopped instead of acting on unproven state -- that is "
            "the fail-closed contract working, not a defect."
        )
    elif outcome == "COMPLETED_UNVERIFIED":
        lines.append(
            "Why this is safe: the steps completed on screen but nothing "
            "independently proved the writes reached a system of record, so "
            "this outcome must never be reported as success."
        )
    else:
        lines.append(
            "Why this is safe: the run failed loudly and reported failure "
            "instead of guessing."
        )

    # What may need undoing: the change ledger, in run order.
    from openadapt_flow.after_stop import change_ledger_lines

    lines.extend(
        change_ledger_lines(
            report,
            undo_hint=command(
                f"undo list {shlex.quote(str(run_dir))} --bundle <bundle-dir>"
            ),
        )
    )

    # Next: derived from the transaction outcome (through the plain result).
    if plain.key == "done_and_checked":
        lines.append(
            "Next: qualify this workflow for your environment -- "
            + command("qualify init <bundle-dir> --target <surface> ...")
        )
    elif plain.key == "finished_not_checked":
        lines.append(
            "Next: pair an oracle -- "
            + command("scaffold-verifier <recording-or-bundle>")
            + " drafts one; wire deployment.yaml effects:, then re-run under "
            "the standard profile."
        )
    elif plain.nothing_written:
        # HALTED_BEFORE_EFFECT, REJECTED_POLICY, CANCELED, FAILED_PLATFORM: the
        # engine proved no business effect, so re-running is safe once fixed.
        if outcome == "HALTED":
            lines.append(
                f"Next: read {report_md} for the halt evidence, fix the cause, "
                "then re-run the same command."
            )
        else:
            lines.append(
                f"Next: read {report_md}, then re-run the same command once the "
                "cause is fixed."
            )
    else:
        # A write may have landed, or this report can't say. Never suggest a
        # blind re-run.
        lines.append(
            "Next: check the record in the application before anyone runs this "
            f"again, and read {report_md} for the evidence. Don't re-run the "
            "same command until the record is checked."
        )
        pending_path, status = _pending_decision(run_dir)
        if pending_path is not None and status == "rejected":
            lines.append(
                f"A person rejected this run ({pending_path}); it won't continue."
            )
        elif pending_path is not None:
            quoted = shlex.quote(str(run_dir))
            lines.append(
                f"A decision is waiting for a person: {pending_path}. After the "
                f"record is checked, approve with {command('approve ' + quoted)}, "
                f"then continue with {command('resume ' + quoted)}."
            )
    if _artifacts.is_file(report_md):
        lines.append(f"Plain-language evidence: {report_md}")
    return "\n".join(lines)

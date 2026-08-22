"""Operator tooling: draft effect-oracle scaffolds + plain-language run reads.

Two read-first operator helpers live here.

``flow scaffold-verifier <recording|bundle>``
    Reads the retained action/effect evidence in a recording directory or a
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

Neither helper executes a workflow, contacts a network, or weakens any gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

BUNDLE_MARKERS = ("workflow.json", "workflow.json.enc")
RECORDING_MARKER = "events.jsonl"

#: The draft file this module writes. Deliberately YAML: it is the shape an
#: operator copies into ``deployment.yaml`` (``effects:``) and reviews by hand.
CONTRACT_FILENAME = "effect_contract.yaml"


class ScaffoldRefused(SystemExit):
    """The input has nothing to scaffold from; refuse with a clear message."""


# ---------------------------------------------------------------------------
# Target inspection (recording dir vs bundle dir)
# ---------------------------------------------------------------------------


def classify_target(path: Path) -> str:
    """Return ``"bundle"`` or ``"recording"`` for an existing artifact path."""
    if not path.exists():
        raise ScaffoldRefused(f"scaffold-verifier: path not found: {path}")
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


def _demonstrated_param_values(recording_dir: Path) -> tuple[str, ...]:
    """The demonstration's parameter values (from ``meta.json``), if any."""
    import json

    try:
        meta = json.loads((recording_dir / "meta.json").read_text(encoding="utf-8"))
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
) -> WriteCandidate:
    """Split one observed new record into identity vs payload fields.

    Uses the compiler miner's own helpers so the draft splits identity from
    payload exactly as a real mined contract would (same surrogate-id
    exclusion, same selector rules, same demonstrated-value payload split).
    """
    from openadapt_flow.compiler.effect_mining import (
        IDEMPOTENCY_KEY_FIELD,
        _match_selector,
        _param_value_fields,
    )

    payload = _param_value_fields(record, demonstrated_values)
    selector = _match_selector(record, set(payload))
    idempotency = None
    raw_key = record.get(IDEMPOTENCY_KEY_FIELD)
    if raw_key not in (None, ""):
        idempotency = (IDEMPOTENCY_KEY_FIELD, str(raw_key))
        # The at-most-once key is surfaced separately (it must be bound to a
        # per-run param, never the frozen demo literal), so it never doubles
        # as an identity selector.
        selector.pop(IDEMPOTENCY_KEY_FIELD, None)
    return WriteCandidate(
        step_id=step_id,
        action=action,
        label=label,
        basis="observed system-of-record delta (one new record)",
        match=selector,
        payload=payload,
        idempotency=idempotency,
    )


def candidates_from_recording(recording_dir: Path) -> list[WriteCandidate]:
    """Write-shaped candidates straight from ``events.jsonl`` evidence."""
    import json

    from openadapt_flow.risk import is_write_shaped

    events_path = recording_dir / RECORDING_MARKER
    try:
        lines = events_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ScaffoldRefused(
            f"scaffold-verifier: could not read {events_path}: {exc}"
        ) from exc

    # Demonstrated parameter values separate a record's PAYLOAD fields (the
    # typed note -> postcondition read-backs) from its identity fields (the
    # match selector), exactly as the compiler's miner does.
    demonstrated_values = _demonstrated_param_values(recording_dir)

    candidates: list[WriteCandidate] = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ScaffoldRefused(
                f"scaffold-verifier: {events_path} line {index + 1} is not JSON: {exc}"
            ) from exc
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
            meta = json.loads((source / "meta.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
        workflow_name = str(meta.get("app_url") or source.name)
        candidates = candidates_from_recording(source)

    if not candidates:
        raise ScaffoldRefused(
            "scaffold-verifier REFUSED: the demonstration has no consequential "
            "(write-shaped) step to verify. An effect oracle asserts that a "
            "WRITE landed in a system of record; this input shows only "
            "read/navigation actions, so there is nothing to scaffold."
        )

    text = render_draft_yaml(
        source=source,
        source_kind=source_kind,
        workflow_name=workflow_name,
        candidates=candidates,
    )
    destination = (out_dir or source) / CONTRACT_FILENAME
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")
    return destination, len(candidates)


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


def explain_run(run_dir: Path) -> str:
    """Read-only plain-language summary of one completed run directory."""
    from openadapt_flow.ir import RunReport

    report_path = run_dir / "report.json"
    if not report_path.is_file():
        raise SystemExit(
            f"explain: {run_dir} holds no report.json -- nothing to explain "
            "(is this a completed run directory?)"
        )
    try:
        report = RunReport.model_validate_json(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(
            f"explain: {report_path} could not be read as a run report: {exc}"
        ) from exc
    outcome = report.execution_outcome or ("success" if report.success else "FAILED")
    executed = sum(1 for result in report.results if not result.skipped)
    ok_steps = sum(1 for result in report.results if result.ok)

    lines = [
        f"What happened: run '{report.workflow_name}' finished {outcome}: "
        f"{ok_steps}/{len(report.results)} steps ok ({executed} executed), "
        f"{report.heal_count} heal(s), model calls {report.model_calls}."
    ]
    receipt = run_dir / "receipt.json"
    if outcome == "VERIFIED":
        lines.append(
            "Why this is safe: every declared effect was independently confirmed "
            "in a system of record before the run claimed success."
        )
        if receipt.is_file():
            lines.append(f"Shareable receipt: {receipt}")
        lines.append(
            "Next: qualify this workflow for your environment -- "
            "openadapt-flow qualify init <bundle-dir> --target <surface> ..."
        )
    elif outcome == "HALTED":
        lines.append(f"Why this is safe: {_halt_check_line(report)}.")
        lines.append(
            "The engine stopped instead of acting on unproven state -- that is "
            "the fail-closed contract working, not a defect."
        )
        lines.append(
            f"Next: read {run_dir / 'REPORT.md'} for the halt evidence, fix the "
            "cause, then re-run the same command."
        )
    elif outcome == "COMPLETED_UNVERIFIED":
        lines.append(
            "Why this is safe: the steps completed on screen but nothing "
            "independently proved the writes reached a system of record, so "
            "this outcome must never be reported as success."
        )
        lines.append(
            "Next: pair an oracle -- openadapt-flow scaffold-verifier "
            "<recording-or-bundle> drafts one; wire deployment.yaml effects:, "
            "then re-run under the standard profile."
        )
    else:
        lines.append(
            "Why this is safe: the run failed loudly and reported failure "
            "instead of guessing."
        )
        lines.append(
            f"Next: read {run_dir / 'REPORT.md'}, then re-run the same command "
            "once the cause is fixed."
        )
    if (run_dir / "REPORT.md").is_file():
        lines.append(f"Plain-language evidence: {run_dir / 'REPORT.md'}")
    return "\n".join(lines)

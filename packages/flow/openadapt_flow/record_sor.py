"""Read the system of record around a recording and propose its checks.

``openadapt-flow record --verify verify.yaml`` reads the records the
``verifier:`` section points at once before the demonstration starts and once
after it ends. The read goes through the same read-only adapter that
``verify-run`` and ``qualify`` use (REST, SQL, FHIR, GraphQL, file export), not
through the UI, so it works the same for a browser, a desktop app or a remote
display.

The difference between the two reads becomes a proposed effect contract,
``verify.proposed.yaml``:

* a record that appeared becomes a ``record_written`` effect whose ``match``
  holds the record's observed fields, with a recorded parameter in place of
  the typed value wherever a field equals one;
* a field that changed on a record that already existed becomes a
  ``field_equals`` effect on that record;
* a record that disappeared, a nested field that changed, and a field that was
  cleared are listed but not proposed, because there is no effect kind that
  checks them honestly.

Every proposed effect carries ``needs_operator_confirmation: true``.
``verify-run`` refuses the file, and ``qualify`` reports it as a problem,
until a person has reviewed each effect and removed that line. Nothing
proposed here is trusted on its own.

The two reads hold raw records, so they are written to ``sor_snapshots.json``
with mode 0600, next to the recording. The proposed contract copies the
``verifier:`` section as written, with ``${NAME}`` references unexpanded, so no
secret from the environment lands in it.

No model calls. Without ``--verify`` nothing here runs.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectKind,
    EffectState,
    ValueExpr,
)

PROPOSED_CONTRACT_NAME = "verify.proposed.yaml"
SNAPSHOTS_NAME = "sor_snapshots.json"
SNAPSHOTS_SCHEMA = "openadapt.sor-snapshots/v1"

#: The record field that identifies a record across the two reads. A record
#: set where every record carries a distinct ``id`` supports updates; any other
#: record set is compared whole, so an update shows as one record removed and
#: one added.
IDENTITY_FIELD = "id"

_SCALARS = (str, int, float, bool)


class RecordSorError(ValueError):
    """The system-of-record observer could not be set up."""


# -- observer ------------------------------------------------------------------


@dataclass
class SystemOfRecordObserver:
    """A :class:`~openadapt_flow.backend.SystemOfRecordBackend` over a verifier.

    ``system_of_record`` is one fresh read through the verifier's
    ``capture_pre_state``. It returns None when the store cannot be read, so a
    failed read is never mistaken for an empty store.
    """

    verifier: Any
    kind: str

    @property
    def system_of_record(self) -> Optional[list[dict[str, Any]]]:
        try:
            state = self.verifier.capture_pre_state()
        except Exception:  # noqa: BLE001 - any read failure is "not observed"
            return None
        if not isinstance(state, EffectState) or not state.reachable:
            return None
        return [dict(record) for record in state.records]


#: Kinds that never read the system of record; refused before building.
_NOT_SYSTEM_OF_RECORD = frozenset({"none", "onscreen", "ui_readback"})


def _not_tier_one(kind: str) -> RecordSorError:
    return RecordSorError(
        f"verifier kind {kind!r} does not read the system of record outside "
        "the UI; use rest, sql, fhir, graphql or file"
    )


def load_observer(path: str | Path) -> tuple[SystemOfRecordObserver, dict[str, Any]]:
    """Build the observer from the ``verifier:`` section of ``path``.

    Returns the observer and the ``verifier:`` mapping as written in the file
    (environment references unexpanded). Any ``effects`` or ``record`` section
    is ignored, so the same ``verify.yaml`` can be used before and after its
    contract exists.

    Raises:
        RecordSorError: the file has no usable ``verifier:`` section, the
            verifier cannot be built, or it does not read the system of record
            outside the UI (Flow tier 1).
    """

    import yaml

    from openadapt_flow.deployment import EffectsConfig, build_effect_verifier
    from openadapt_flow.qualify.config import _expand
    from openadapt_flow.verification import verifier_effect_tier

    source = Path(path)
    if not source.is_file():
        raise RecordSorError(f"{source} is not a file")
    try:
        data = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise RecordSorError(f"cannot parse {source}: {exc}") from exc
    raw = data.get("verifier") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        raise RecordSorError(f"{source} has no verifier: section")
    missing: list[str] = []
    expanded = _expand({k: v for k, v in raw.items() if k != "session"}, missing)
    if missing:
        raise RecordSorError(
            "environment variable(s) not set: " + ", ".join(sorted(set(missing)))
        )
    try:
        cfg = EffectsConfig.model_validate(expanded)
    except Exception as exc:
        # Never print the exception text: ${ENV} values are already expanded,
        # and pydantic echoes the input (tokens and passwords included).
        errors = getattr(exc, "errors", None)
        detail = (
            "; ".join(
                f"{'.'.join(str(p) for p in item.get('loc', ())) or '(top level)'}: "
                f"{item.get('msg', 'invalid')}"
                for item in errors()
            )
            if callable(errors)
            else type(exc).__name__
        )
        raise RecordSorError(f"invalid verifier section: {detail}") from None
    if cfg.candidates:
        raise RecordSorError("record --verify does not support verifier candidates")
    if cfg.kind in _NOT_SYSTEM_OF_RECORD:
        raise _not_tier_one(cfg.kind)
    try:
        verifier = build_effect_verifier(cfg, {})
    except Exception as exc:  # noqa: BLE001 - plugin factories raise anything
        # Name only the exception type: a plugin's message can echo a DSN or
        # a password from the expanded config.
        raise RecordSorError(
            f"cannot build the verifier ({type(exc).__name__}). While recording, "
            "the verifier has no run parameters, so its read must name its "
            "records with literals."
        ) from None
    # The tier a proposed record check would get: a record read, no
    # on-screen read-back spec.
    probe = Effect(kind=EffectKind.RECORD_WRITTEN, match={"_": ValueExpr(literal="_")})
    try:
        tier = verifier_effect_tier(verifier, probe) if verifier is not None else None
    except Exception:  # noqa: BLE001 - an untyped verifier is refused below
        tier = None
    if tier is None or not tier.reads_system_of_record():
        raise _not_tier_one(cfg.kind)
    return SystemOfRecordObserver(verifier=verifier, kind=cfg.kind), dict(raw)


# -- diff ----------------------------------------------------------------------


@dataclass(frozen=True)
class FieldChange:
    field: str
    before: Any
    after: Any


@dataclass
class RecordChange:
    """One record present in both reads whose fields differ."""

    identity: Any
    before: dict[str, Any]
    after: dict[str, Any]
    changes: list[FieldChange]


@dataclass
class SnapshotDiff:
    created: list[dict[str, Any]] = field(default_factory=list)
    changed: list[RecordChange] = field(default_factory=list)
    removed: list[dict[str, Any]] = field(default_factory=list)
    #: The field records were matched on, or None when compared whole.
    identity_field: Optional[str] = None

    @property
    def empty(self) -> bool:
        return not (self.created or self.changed or self.removed)


def _keyed(records: list[dict[str, Any]]) -> Optional[dict[Any, dict[str, Any]]]:
    out: dict[Any, dict[str, Any]] = {}
    for record in records:
        key = record.get(IDENTITY_FIELD)
        if key is None or not isinstance(key, _SCALARS) or key in out:
            return None
        out[key] = record
    return out


def _whole(record: dict[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, default=str)


def diff_snapshots(
    before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> SnapshotDiff:
    """What changed between two reads of the same record set."""

    keyed_before, keyed_after = _keyed(before), _keyed(after)
    if keyed_before is None or keyed_after is None:
        before_ids = {_whole(r) for r in before}
        after_ids = {_whole(r) for r in after}
        return SnapshotDiff(
            created=[r for r in after if _whole(r) not in before_ids],
            removed=[r for r in before if _whole(r) not in after_ids],
        )
    diff = SnapshotDiff(identity_field=IDENTITY_FIELD)
    for key, record in keyed_after.items():
        old = keyed_before.get(key)
        if old is None:
            diff.created.append(record)
            continue
        changes = [
            FieldChange(name, old.get(name), record.get(name))
            for name in sorted(set(old) | set(record))
            if old.get(name) != record.get(name)
        ]
        if changes:
            diff.changed.append(RecordChange(key, old, record, changes))
    diff.removed = [r for k, r in keyed_before.items() if k not in keyed_after]
    return diff


# -- proposal ------------------------------------------------------------------


@dataclass
class ContractProposal:
    effects: list[Effect] = field(default_factory=list)
    #: What changed but was not proposed, and why.
    not_proposed: list[str] = field(default_factory=list)


def _param_lookup(params: Mapping[str, str]) -> tuple[dict[str, str], list[str]]:
    """Map a recorded value to its parameter name; skip values two share."""

    owners: dict[str, list[str]] = {}
    for name, value in params.items():
        if value is not None and str(value) != "":
            owners.setdefault(str(value), []).append(name)
    lookup = {value: names[0] for value, names in owners.items() if len(names) == 1}
    shared = [
        f"parameters {', '.join(sorted(names))} share one value, so neither is "
        "bound; the observed value is kept as a literal"
        for names in owners.values()
        if len(names) > 1
    ]
    return lookup, shared


def _expr(value: Any, lookup: Mapping[str, str]) -> ValueExpr:
    name = lookup.get(str(value))
    return ValueExpr(param=name) if name else ValueExpr(literal=str(value))


def _describe(expr: ValueExpr) -> str:
    return f"parameter {expr.param!r}" if expr.param else repr(expr.literal)


def propose_effects(
    diff: SnapshotDiff, params: Optional[Mapping[str, str]] = None
) -> ContractProposal:
    """Effects that would check the change ``diff`` shows, all unconfirmed."""

    lookup, shared = _param_lookup(dict(params or {}))
    proposal = ContractProposal(not_proposed=list(shared))

    for record in diff.created:
        match: dict[str, ValueExpr] = {}
        skipped: list[str] = []
        for name, value in sorted(record.items()):
            if name == diff.identity_field or value is None or str(value) == "":
                continue
            if not isinstance(value, _SCALARS):
                skipped.append(name)
                continue
            match[name] = _expr(value, lookup)
        if skipped:
            proposal.not_proposed.append(
                f"new record: nested field(s) {', '.join(skipped)} are not matched"
            )
        if not match:
            proposal.not_proposed.append(
                "a record appeared with no scalar field to match it by"
            )
            continue
        proposal.effects.append(
            Effect(
                kind=EffectKind.RECORD_WRITTEN,
                match=match,
                expected_count=1,
                count_new_only=True,
                needs_operator_confirmation=True,
                probe=(
                    "exactly one new record with "
                    + ", ".join(f"{k}={_describe(v)}" for k, v in match.items())
                    + " (proposed from the records read while recording)"
                ),
            )
        )

    for change in diff.changed:
        identity = {str(diff.identity_field): _expr(change.identity, lookup)}
        for item in change.changes:
            if item.after is None or str(item.after) == "":
                proposal.not_proposed.append(
                    f"changed record: field {item.field!r} was cleared"
                )
                continue
            if not isinstance(item.after, _SCALARS):
                proposal.not_proposed.append(
                    f"changed record: nested field {item.field!r} changed"
                )
                continue
            value = _expr(item.after, lookup)
            proposal.effects.append(
                Effect(
                    kind=EffectKind.FIELD_EQUALS,
                    match=dict(identity),
                    field=item.field,
                    value=value,
                    needs_operator_confirmation=True,
                    probe=(
                        f"field {item.field!r} of record "
                        f"{diff.identity_field}={change.identity!r} equals "
                        f"{_describe(value)} (proposed from the records read "
                        "while recording)"
                    ),
                )
            )

    if diff.removed:
        proposal.not_proposed.append(
            f"{len(diff.removed)} record(s) disappeared; no effect kind checks "
            "a deletion"
        )
    return proposal


# -- files ---------------------------------------------------------------------


def _write_private(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


_CONTRACT_HEADER = """\
# Proposed by `openadapt-flow record --verify` from the records read before and
# after the demonstration. Nothing here is trusted yet. For each effect:
#   - remove match fields that change on every save (timestamps, versions);
#   - check that each {param: ...} is the input that should fill that field;
#   - delete the `needs_operator_confirmation: true` line.
# verify-run refuses this file, and qualify reports a problem, until every
# such line is gone.
"""


def render_contract(verifier: Mapping[str, Any], effects: list[Effect]) -> str:
    import yaml

    doc = {
        "schema_version": "openadapt.verify/v1",
        "verifier": dict(verifier),
        "effects": [
            effect.model_dump(mode="json", exclude_defaults=True) for effect in effects
        ],
    }
    return _CONTRACT_HEADER + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class RecordingObservation:
    """The before read of one recording, and how to finish it."""

    observer: SystemOfRecordObserver
    verifier_section: dict[str, Any]
    before: list[dict[str, Any]]
    before_read_at: str

    @classmethod
    def start(cls, verify_path: str | Path) -> "RecordingObservation":
        observer, section = load_observer(verify_path)
        records = observer.system_of_record
        if records is None:
            raise RecordSorError(
                f"the {observer.kind} verifier could not read the system of record"
            )
        return cls(observer, section, records, _now())

    def finish(
        self, recording: Path, params: Optional[Mapping[str, str]] = None
    ) -> tuple[Optional[Path], ContractProposal, Optional[SnapshotDiff]]:
        """Read again, keep both reads, and write the proposed contract.

        Returns the contract path (None when nothing was proposed), the
        proposal, and the diff (None when the second read failed).
        """

        after = self.observer.system_of_record
        read_at = _now()
        recording.mkdir(parents=True, exist_ok=True)
        if params is None:
            params = _recorded_params(recording)
        snapshots = {
            "schema_version": SNAPSHOTS_SCHEMA,
            "verifier_kind": self.observer.kind,
            "before": {"read_at": self.before_read_at, "records": self.before},
            "after": {"read_at": read_at, "records": after},
        }
        _write_private(
            recording / SNAPSHOTS_NAME,
            json.dumps(snapshots, indent=2, sort_keys=True, default=str) + "\n",
        )
        if after is None:
            return (
                None,
                ContractProposal(
                    not_proposed=["the system of record could not be read again"]
                ),
                None,
            )
        diff = diff_snapshots(self.before, after)
        proposal = propose_effects(diff, params)
        if not proposal.effects:
            return None, proposal, diff
        path = recording / PROPOSED_CONTRACT_NAME
        _write_private(path, render_contract(self.verifier_section, proposal.effects))
        return path, proposal, diff


def _recorded_params(recording: Path) -> dict[str, str]:
    try:
        meta = json.loads((recording / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    params = meta.get("params") if isinstance(meta, dict) else None
    if not isinstance(params, dict):
        return {}
    return {str(k): str(v) for k, v in params.items() if v is not None}


def summary_lines(
    contract: Optional[Path],
    proposal: ContractProposal,
    diff: Optional[SnapshotDiff],
) -> list[str]:
    """What the operator sees after recording: counts and field names only."""

    lines: list[str] = []
    if diff is not None:
        lines.append(
            f"System of record: {len(diff.created)} record(s) added, "
            f"{len(diff.changed)} changed, {len(diff.removed)} removed."
        )
        for change in diff.changed:
            names = ", ".join(c.field for c in change.changes)
            lines.append(f"  changed fields: {names}")
    for note in proposal.not_proposed:
        lines.append(f"  not proposed: {note}")
    if contract is not None:
        lines.append(
            f"Proposed {len(proposal.effects)} check(s) in {contract}. Review "
            "them, then delete each needs_operator_confirmation line."
        )
    elif diff is not None and diff.empty:
        lines.append("No change seen, so no checks were proposed.")
    else:
        lines.append("No checks were proposed.")
    return lines

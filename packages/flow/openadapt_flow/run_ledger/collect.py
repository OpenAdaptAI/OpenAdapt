"""Collectors: read the files other commands already write, one function each.

A collector never edits its source, never calls a model and never touches
the network. Each takes the file it recognises (and its siblings) and returns
a :class:`Contribution`: outcome signals, task ids, timestamps, model usage,
intervals a person spent, field names and verdicts, and (kept apart) the
values the private sidecar holds.

Each collector refuses a schema version it doesn't know. The contribution is
then a single ``schema_unrecognised`` signal and nothing else, so the row is
UNKNOWN rather than half-read. :func:`collect` walks a run directory (or one
file), dispatches each file by name, and merges the contributions.

Main-branch sources and what each one can't tell (also in
``docs/RUN_LEDGER.md``):

* ``summary.json`` + ``guard_events.jsonl`` (agent guard,
  ``openadapt.agent-guard-run/v1``): the guard's verdict, blocked saves, holds
  and run seconds. No wall-clock times, no field names, no agent model usage.
* ``effect-receipt.json`` (verify-run, ``openadapt.effect-receipt/v1``): the
  receipt outcome, the effect verdict, the read-back tier and the executor
  kind. No field names (effect rows are contract hashes).
* ``report.json`` (a compiled run's ``RunReport``, no schema field, recognised
  by shape) and ``receipt.json`` (``openadapt.run-receipt/v2``): the
  transaction outcome, the read-back level, model calls, estimated cost, start
  time and duration, the run's inputs (committed, never copied).
* ``openadapt-agent.run/v1`` records (wrap's run store): status, verified,
  receipt outcome, model calls and cost when known, start and finish times.
* ``item-approval/request-*.json`` and ``answer-*.json``: approval and
  question intervals from ``created_at`` to ``answered_at`` (the times inside
  the files, never file mtimes).
* ``jobs/*.json`` (task ledger, ``openadapt.task-ledger/v1``): question
  intervals from ``asked_at`` to ``answered_at``. Question text is never read.
* ``run1.json`` (compile-agent, ``agent`` = ``AgentLedger.to_dict()``) and
  ``agent_ledger.json`` (the launch demo's agent ledger): metered calls,
  tokens and cost, whether the agent finished, gate refusals.
  ``summary``, ``final_text`` and per-call ``actions`` are never read.

Sources produced by the agent-control-plane pieces: ``flight.json``
(``openadapt.flight-run/v1``), ``guard_ledger_row.json``
(``openadapt.guard-ledger-row/v1``), a lone ``proposed_write.json``, and,
in :mod:`openadapt_flow.run_ledger.readers`, readiness shadow run
directories, P7 fallback results and P6's exception folders. Their
collectors read exactly the keys ``docs/RUN_LEDGER.md`` lists.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, cast

from openadapt_types.approval import CHECKED_HOW_TIER

from openadapt_flow.run_ledger.outcome import (
    AGENT_RUN_STATUS_REASON,
    EFFECT_VERDICT_REASON,
    EXECUTE_OUTCOME_REASON,
    EXECUTION_OUTCOME_REASON,
    GUARD_VERDICT_REASON,
    TRANSACTION_OUTCOME_REASON,
    WRAP_STATUS_REASON,
    Signal,
    guard_block_reason,
    outcome_reason,
    verified_or_tier,
)

GUARD_SUMMARY_SCHEMA = "openadapt.agent-guard-run/v1"
EFFECT_RECEIPT_SCHEMA = "openadapt.effect-receipt/v1"
RUN_RECEIPT_SCHEMA = "openadapt.run-receipt/v2"
AGENT_RUN_RECORD_SCHEMA = "openadapt-agent.run/v1"
ITEM_APPROVAL_REQUEST_SCHEMA = "openadapt.item-approval-request/v1"
ITEM_APPROVAL_ANSWER_SCHEMA = "openadapt.item-approval-answer/v1"
TASK_LEDGER_SCHEMA = "openadapt.task-ledger/v1"
PROPOSED_WRITE_SCHEMA = "openadapt.proposed-write/v1"
FLIGHT_SCHEMA = "openadapt.flight-run/v1"
GUARD_LEDGER_ROW_SCHEMA = "openadapt.guard-ledger-row/v1"
EXCEPTION_ANSWER_SCHEMA = "openadapt.exception-answer/v1"

#: ``openadapt_flow.agent.ledger.ASK_WORKFLOW_NAME``: an item-approval request
#: that is a ledger question, not a save approval.
_ASK_WORKFLOW = "openadapt.task-ledger.question"

_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_LABEL = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_STAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$"
)

#: How far the walk goes, so a recording folder can't make ``add`` slow.
MAX_DEPTH = 5
MAX_FILES = 5000
MAX_BYTES = 16 * 1024 * 1024

#: The executor kinds verify-run names, as ledger kinds.
_EXECUTOR_KIND = {
    "flow_replay": "program",
    "script": "program",
    "agent": "agent",
    "human": "human",
    "unknown": "unknown",
}
_CHECK_RANK = {"matched": 0, "not_checked": 1, "differs": 2}
_TIER_RANK = {"independent_readback": 0, "screen_readback": 1, "unchecked": 2}


@dataclass
class Usage:
    """Model usage one source reports. ``None`` means it doesn't say."""

    calls: Optional[int] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    cost_source: str = "unknown"


@dataclass
class Interval:
    kind: str
    opened_at: str
    decided_at: Optional[str]
    #: Two sources describing one wait (an approval request and the guard
    #: row that mentions it) share a key and are counted once.
    key: str = ""


@dataclass
class Contribution:
    """What one source says about the run."""

    collector: str
    files: list[Path] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)
    executor_kind: Optional[str] = None
    executor_label: Optional[str] = None
    usage: Optional[Usage] = None
    intervals: list[Interval] = field(default_factory=list)
    #: name -> matched | differs | not_checked
    fields: dict[str, str] = field(default_factory=dict)
    fields_differing: int = 0
    #: name -> {"intended": ..., "saved": ...}; private sidecar only.
    private_fields: dict[str, dict[str, Any]] = field(default_factory=dict)
    inputs: Optional[dict[str, Any]] = None
    readback_tiers: list[str] = field(default_factory=list)
    receipt_digest: Optional[str] = None
    #: Field names the run's own task, verify or readiness reference config
    #: declares (``ledger export`` lets only these past the scrubber check).
    declared_fields: set[str] = field(default_factory=set)
    #: Wall-clock moments the source records (an approval asked, a hold, a
    #: read-back issued). They bound the run when it keeps no start time.
    moments: list[str] = field(default_factory=list)
    #: P7: the fallback request id whose item-approval folder holds the
    #: gate's requests and answers (``ledger add --approvals``).
    approvals_key: Optional[str] = None
    #: Config files a source points at (P7's confirmed verify.yaml).
    config_paths: list[Path] = field(default_factory=list)
    #: A dispatcher envelope's ``run_id``: wrap's run store record
    #: (``runs/<run_id>.json``) keeps its start and finish times.
    run_record_id: Optional[str] = None
    #: P7: what a field the record check names counts as (confirmed or not).
    fields_check: Optional[str] = None
    #: P7 ``gate_answers``: request id -> request_sha256.
    gate_requests: dict[str, Optional[str]] = field(default_factory=dict)
    #: ``fields`` are names a read-back saw in the new record, not names
    #: the run declared it would write.
    readback_fields: bool = False
    #: The guard's relative clock (``guard_events.jsonl`` ``t``) when the save
    #: was held for a person: with the approval's created_at it names the
    #: run's start.
    anchor_t: Optional[float] = None
    #: True when the source shows no person was asked during the run (a
    #: shadow hold, a guard run with approval ``not_gated``): the row's
    #: ``intervention_s`` is then 0 rather than unknown.
    no_person: bool = False
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    duration_s: Optional[float] = None

    def signal(self, reason: str, basis: str = "deterministic") -> None:
        self.signals.append(Signal(reason, basis, self.collector))  # type: ignore[arg-type]


# -- reading ------------------------------------------------------------------------


class _Unreadable(Exception):
    pass


def _read_json(path: Path) -> Any:
    try:
        if path.is_symlink() or path.stat().st_size > MAX_BYTES:
            raise _Unreadable(str(path))
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise _Unreadable(str(path)) from exc


def _refused(collector: str, path: Path, reason: str) -> Contribution:
    out = Contribution(collector, files=[path])
    out.signal(reason)
    return out


def _schema_check(
    doc: Any, key: str, expected: str, collector: str, path: Path
) -> Optional[Contribution]:
    """None when ``doc[key]`` is the version this collector reads."""

    if not isinstance(doc, dict):
        return _refused(collector, path, "source_unreadable")
    if doc.get(key) != expected:
        return _refused(collector, path, "schema_unrecognised")
    return None


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _float(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value) or value < 0:
        return None
    return value


def _stamp(value: Any) -> Optional[str]:
    """An RFC 3339 instant with an offset, normalised to microseconds."""

    if not isinstance(value, str) or not _STAMP.match(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _iso(parsed)


def _iso(moment: datetime) -> str:
    return (
        moment.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _epoch(value: Any) -> Optional[str]:
    """A wall-clock epoch in seconds. Monotonic clock readings (small numbers)
    are refused: they don't name a time."""

    number = _float(value)
    if number is None or not (1_000_000_000 < number < 4_000_000_000):
        return None
    return _iso(datetime.fromtimestamp(number, tz=timezone.utc))


def _task_id(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and _TASK_ID.match(value) else None


def _label(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower().replace(" ", "-")
    return lowered if _LABEL.match(lowered) else None


def _field_name(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and _FIELD_NAME.match(value) else None


# -- agent guard ----------------------------------------------------------------------


def collect_agent_guard(path: Path) -> Contribution:
    """``summary.json`` (``openadapt.agent-guard-run/v1``) and the
    ``guard_events.jsonl`` beside it.

    Limits: the guard keeps relative times only, so the row gets
    ``duration_s`` but no ``started_at``. It names no fields and no model
    usage of the agent it guards (its own ``model_calls`` are the guard's).
    """

    name = "agent_guard"
    try:
        doc = _read_json(path)
    except _Unreadable:
        return _refused(name, path, "source_unreadable")
    refused = _schema_check(doc, "schema", GUARD_SUMMARY_SCHEMA, name, path)
    if refused is not None:
        return refused
    out = Contribution(name, files=[path], executor_kind="agent")
    out.executor_label = "agent-guard"
    # ``not_gated``: the guard never asks a person (no approval card).
    out.no_person = doc.get("approval") == "not_gated"
    if (tid := _task_id(doc.get("task_id"))) is not None:
        out.task_ids.append(tid)
    out.duration_s = _float(doc.get("seconds"))
    events: list[dict[str, Any]] = []
    events_path = path.parent / "guard_events.jsonl"
    if events_path.is_file() and not events_path.is_symlink():
        out.files.append(events_path)
        try:
            for line in events_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    event = json.loads(line)
                    if isinstance(event, dict):
                        events.append(event)
        except (OSError, ValueError):
            out.signal("source_unreadable")
    times = [t for t in (_float(e.get("t")) for e in events) if t is not None]
    if doc.get("ended") == "timeout" and times:
        # After its last save decision the guard only waited for the agent
        # to exit; the run's own span ends at that decision.
        out.duration_s = round(max(times), 3)
    held = [
        t
        for e in events
        if e.get("decision") == "held_for_person"
        and (t := _float(e.get("t"))) is not None
    ]
    # Where the save was held for a person: lines the approval request's own
    # created_at up with the guard's relative clock (see ``collect``).
    out.anchor_t = held[0] if held else None
    if events and not held:
        # The guard's own events show no save was held for a person: a save
        # blocked before any card opened asked nobody.
        out.no_person = True
    meta = path.parent / "recording" / "meta.json"
    if meta.is_file() and not meta.is_symlink():
        try:
            created = _read_json(meta).get("created_at")
        except (_Unreadable, AttributeError):
            created = None
        if (moment := _stamp(created)) is not None:
            # The recording is written when the guarded run ends.
            out.files.append(meta)
            out.moments.append(moment)
    scope_codes = {
        str(item.get("code"))
        for item in doc.get("scope_blocked") or []
        if isinstance(item, dict)
    }
    blocked = [b for b in doc.get("blocked") or [] if isinstance(b, dict)]
    for item in blocked:
        code = str(item.get("code") or "")
        out.signal(guard_block_reason(code, scope=code in scope_codes))
    verdict = doc.get("verdict")
    would_block = [w for w in doc.get("would_block") or [] if isinstance(w, dict)]
    save_events = [e for e in events if e.get("kind") == "save"]
    sent = bool(doc.get("hold_ms")) or any(
        e.get("decision") == "continued" for e in save_events
    )
    if verdict is not None:
        reason = outcome_reason(GUARD_VERDICT_REASON, verdict)
        if reason == "readback_verified":
            # A guard VERIFIED is verify-run's: an independent read-back.
            out.readback_tiers.append("independent_readback")
            if would_block:
                # Observe-only let a save through that it would have blocked.
                reason = "observe_only_would_block"
        out.signal(reason)
    else:
        last_save = save_events[-1] if save_events else None
        if last_save is not None and last_save.get("decision") == "held_for_person":
            out.signal("approval_held")
        elif sent:
            out.signal("write_unconfirmed")
        elif not blocked and not (
            events_path.is_file() or _int((doc.get("requests") or {}).get("save")) == 0
        ):
            # No events file and no count of zero saves: nothing shows that
            # no save request went out.
            out.signal("write_unconfirmed")
        elif not blocked:
            ended = doc.get("ended")
            out.signal(
                "timed_out_no_write"
                if ended == "timeout"
                else "crashed_no_write"
                if ended == "tab_closed"
                else "agent_gave_up"
            )
    return out


# -- verify-run ---------------------------------------------------------------------


def collect_effect_receipt(path: Path) -> Contribution:
    """``effect-receipt.json`` (verify-run, ``openadapt.effect-receipt/v1``).

    Limits: effect rows carry contract hashes, not field names, so the row
    gets no ``fields_written`` from here.
    """

    name = "verify_run"
    try:
        doc = _read_json(path)
    except _Unreadable:
        return _refused(name, path, "source_unreadable")
    refused = _schema_check(doc, "schema_version", EFFECT_RECEIPT_SCHEMA, name, path)
    if refused is not None:
        return refused
    receipt = doc.get("receipt")
    if not isinstance(receipt, dict) or (
        receipt.get("schema_version") != EFFECT_RECEIPT_SCHEMA
    ):
        return _refused(name, path, "schema_unrecognised")
    out = Contribution(name, files=[path])
    out.executor_kind = _EXECUTOR_KIND.get(str(receipt.get("executor_kind")), "unknown")
    evidence = doc.get("evidence") if isinstance(doc.get("evidence"), dict) else {}
    readback = evidence.get("readback") if isinstance(evidence, dict) else None
    readback = readback if isinstance(readback, dict) else {}
    for value in (
        readback.get("baseline_read_at"),
        readback.get("read_at"),
        receipt.get("issued_at"),
    ):
        if (moment := _stamp(value)) is not None:
            out.moments.append(moment)
    tier = receipt.get("verification_tier")
    if tier in _TIER_RANK:
        out.readback_tiers.append(str(tier))
    reason = outcome_reason(EXECUTE_OUTCOME_REASON, receipt.get("outcome"))
    if reason == "readback_verified":
        reason = verified_or_tier(tier if isinstance(tier, str) else None)
    out.signal(reason)
    effect = outcome_reason(EFFECT_VERDICT_REASON, receipt.get("effect_verdict"))
    if effect == "readback_refuted" and _store_shows_a_write(doc):
        # The guard's rule (``guard._verdict``): REFUTED only when the store
        # shows a write. An absent effect after a halt before the effect, or
        # a save that never landed, is the outcome's call, not a catch.
        out.signal(effect)
    return out


def _store_shows_a_write(doc: Mapping[str, Any]) -> bool:
    evidence = doc.get("evidence")
    readback = evidence.get("readback") if isinstance(evidence, dict) else None
    rows = readback.get("effects") if isinstance(readback, dict) else None
    return any(
        isinstance(row, dict)
        and row.get("observed_effect") in ("present", "conflicting")
        for row in rows or []
    )


# -- compiled runs ------------------------------------------------------------------


def _is_run_report(doc: Any) -> bool:
    return (
        isinstance(doc, dict)
        and isinstance(doc.get("workflow_name"), str)
        and "started_at" in doc
        and "results" in doc
    )


def collect_run_report(path: Path) -> Contribution:
    """A compiled run's ``report.json`` (``RunReport``) and ``receipt.json``
    (``openadapt.run-receipt/v2``) beside it.

    ``report.json`` has no schema field, so it is recognised by shape. A
    VERIFIED transaction counts as VERIFIED only when the report's read-back
    level or the receipt's assurance is an independent read-back.
    """

    name = "compiled_run"
    try:
        doc = _read_json(path)
    except _Unreadable:
        return _refused(name, path, "source_unreadable")
    if not _is_run_report(doc):
        return _refused(name, path, "schema_unrecognised")
    out = Contribution(name, files=[path], executor_kind="program")
    out.executor_label = "openadapt-flow"
    out.started_at = _stamp(doc.get("started_at"))
    total_ms = _float(doc.get("total_ms"))
    if total_ms is not None:
        out.duration_s = round(total_ms / 1000.0, 3)
        if out.started_at is not None:
            start = datetime.fromisoformat(out.started_at.replace("Z", "+00:00"))
            out.ended_at = _iso(start + timedelta(milliseconds=total_ms))
    out.usage = Usage(
        calls=_int(doc.get("model_calls")),
        cost_usd=_float(doc.get("est_model_cost_usd")),
    )
    if out.usage.cost_usd is not None:
        out.usage.cost_source = "reported"
    params = doc.get("params")
    if isinstance(params, dict):
        out.inputs = dict(params)
    tier: Optional[str] = None
    readback = doc.get("readback")
    if isinstance(readback, dict):
        tiers = cast("Mapping[str, str]", CHECKED_HOW_TIER)
        tier = tiers.get(str(readback.get("checked_how")))
    receipt_path = path.parent / "receipt.json"
    if receipt_path.is_file():
        receipt = collect_run_receipt(receipt_path)
        out.files.extend(receipt.files)
        out.signals.extend(
            s for s in receipt.signals if s.reason != "readback_verified"
        )
        out.receipt_digest = receipt.receipt_digest
        if tier is None and receipt.readback_tiers:
            tier = receipt.readback_tiers[0]
    if tier is not None:
        out.readback_tiers.append(tier)
    outcome = doc.get("transaction_outcome")
    table = TRANSACTION_OUTCOME_REASON
    if outcome is None:
        outcome, table = doc.get("execution_outcome"), EXECUTION_OUTCOME_REASON
    reason = outcome_reason(table, outcome)
    if reason == "readback_verified":
        reason = verified_or_tier(tier)
    out.signal(reason)
    return out


def collect_run_receipt(path: Path) -> Contribution:
    """``receipt.json`` (``openadapt.run-receipt/v2``), the success receipt.

    It exists only for a VERIFIED run; its ``effect_assurance`` (or, on an
    older receipt, ``effect_tier_reached``) says whether the read-back was
    independent.
    """

    name = "run_receipt"
    try:
        doc = _read_json(path)
    except _Unreadable:
        return _refused(name, path, "source_unreadable")
    refused = _schema_check(doc, "schema_version", RUN_RECEIPT_SCHEMA, name, path)
    if refused is not None:
        return refused
    out = Contribution(name, files=[path], executor_kind="program")
    out.executor_label = "openadapt-flow"
    assurance = doc.get("effect_assurance")
    if assurance is None:
        assurance = {
            "independent_system": "independent_readback",
            "independent_session": "screen_readback",
            "persisted_state_reacquisition": "screen_readback",
            "immediate_screen": "screen_readback",
        }.get(str(doc.get("effect_tier_reached")))
    if assurance in _TIER_RANK:
        out.readback_tiers.append(str(assurance))
    digest = doc.get("receipt_digest")
    if isinstance(digest, str) and _HEX64.match(digest):
        out.receipt_digest = digest
    out.usage = Usage(
        calls=_int(doc.get("model_calls")),
        cost_usd=_float(doc.get("est_cost_usd")),
    )
    if out.usage.cost_usd is not None:
        out.usage.cost_source = "reported"
    duration = _int(doc.get("duration_ms"))
    out.duration_s = round(duration / 1000.0, 3) if duration is not None else None
    reason = outcome_reason(TRANSACTION_OUTCOME_REASON, doc.get("outcome"))
    if reason == "readback_verified":
        reason = verified_or_tier(assurance if isinstance(assurance, str) else None)
    out.signal(reason)
    return out


# -- wrap ---------------------------------------------------------------------------


def collect_agent_run_record(path: Path) -> Contribution:
    """A wrap / agent run record (``openadapt-agent.run/v1``) from
    ``openadapt-agent/runs/<run_id>.json``.

    Limits: a wrap result names no fields and no tokens; ``model_calls`` and
    ``cost_usd`` are ``null`` when the agent's usage is unknown. A record still
    ``running`` is a lost reply.
    """

    name = "wrap"
    try:
        doc = _read_json(path)
    except _Unreadable:
        return _refused(name, path, "source_unreadable")
    refused = _schema_check(doc, "schema", AGENT_RUN_RECORD_SCHEMA, name, path)
    if refused is not None:
        return refused
    out = Contribution(name, files=[path], executor_label="openadapt-wrap")
    if (tid := _task_id(doc.get("request_id"))) is not None:
        out.task_ids.append(tid)
    out.started_at = _stamp(doc.get("started_at"))
    out.ended_at = _stamp(doc.get("finished_at"))
    result = doc.get("result")
    if doc.get("state") != "finished" or not isinstance(result, dict):
        out.signal("lost_reply")
        return out
    path_kind = result.get("path")
    fallback_unproved = False
    if path_kind == "agent_fallback":
        # P7: an agent run after a program's drift. Its ``verified`` counts
        # only with the record check that proves it: readable and passed
        # (P7's ``confirmed``). A record without one proves nothing.
        path_kind = "agent"
        # P7 keeps the policy's record in the result's ``fallback``; its
        # task_record_check is the final read (``record_check`` on older ones).
        policy = result.get("fallback")
        check = (
            policy.get("task_record_check")
            if isinstance(policy, dict) and "task_record_check" in policy
            else result.get("record_check")
        )
        from openadapt_flow.run_ledger.readers import policy_agrees

        fallback_unproved = not (
            isinstance(check, dict)
            and check.get("readable") is True
            and check.get("passed") is True
            and isinstance(policy, dict)
            and policy_agrees(policy)
        )
    out.executor_kind = (
        "program" if path_kind == "skill" else "agent" if path_kind == "agent" else None
    )
    out.duration_s = _float(result.get("seconds"))
    out.usage = Usage(
        calls=_int(result.get("model_calls")), cost_usd=_float(result.get("cost_usd"))
    )
    if out.usage.cost_usd is not None:
        out.usage.cost_source = "reported"
    status = result.get("status")
    receipt_outcome = result.get("receipt_outcome")
    if isinstance(status, str) and status in WRAP_STATUS_REASON:
        out.signal(WRAP_STATUS_REASON[status])
    elif result.get("verified") is True and fallback_unproved:
        out.signal("reconciliation_required")
    elif result.get("verified") is True and (
        receipt_outcome == "VERIFIED" or path_kind == "agent"
    ):
        # A compiled run: verify-run's VERIFIED. An agent's first run: the
        # record check read the system of record before and after.
        out.readback_tiers.append("independent_readback")
        out.signal("readback_verified")
    elif isinstance(receipt_outcome, str):
        reason = outcome_reason(EXECUTE_OUTCOME_REASON, receipt_outcome)
        out.signal("completed_unverified" if reason == "readback_verified" else reason)
    elif isinstance(status, str) and status.lower() in AGENT_RUN_STATUS_REASON:
        out.signal(AGENT_RUN_STATUS_REASON[status.lower()])
    else:
        out.signal("write_unconfirmed")
    return out


# -- a person's time ----------------------------------------------------------------


def collect_item_approvals(
    folder: Path, only: Optional[Mapping[str, Optional[str]]] = None
) -> Contribution:
    """``item-approval/request-<id>.json`` and ``answer-<id>.json``.

        One interval per request, from the request's ``created_at`` to the
        answer's ``answered_at``. A request with no answer is still open: the row's
        intervention time is then unknown, and the run is ESCALATED
        (``approval_held``). A declined save is ``person_declined`` (ESCALATED,
    not a catch). A ledger
        question (``workflow_name`` ``openadapt.task-ledger.question``) is a
        ``question`` interval.
    """

    name = "item_approval"
    out = Contribution(name)
    for request_path in sorted(folder.glob("request-*.json")):
        request_id = request_path.name[len("request-") : -len(".json")]
        if only is not None and request_id not in only:
            continue  # not one of the requests the run's gate recorded
        try:
            request = _read_json(request_path)
        except _Unreadable:
            out.files.append(request_path)
            out.signal("source_unreadable")
            continue
        out.files.append(request_path)
        if not isinstance(request, dict) or (
            request.get("schema_version") != ITEM_APPROVAL_REQUEST_SCHEMA
        ):
            out.signal("schema_unrecognised")
            continue
        opened = _stamp(request.get("created_at"))
        if opened is None:
            out.signal("source_unreadable")
            continue
        question = request.get("workflow_name") == _ASK_WORKFLOW
        decided: Optional[str] = None
        decision: Optional[str] = None
        answer_path = folder / f"answer-{request_id}.json"
        if answer_path.is_file():
            out.files.append(answer_path)
            try:
                answer = _read_json(answer_path)
            except _Unreadable:
                answer = None
            if not isinstance(answer, dict) or (
                answer.get("schema_version") != ITEM_APPROVAL_ANSWER_SCHEMA
            ):
                out.signal("schema_unrecognised")
                continue
            if only is not None and only.get(request_id) not in (
                None,
                answer.get("request_sha256"),
            ):
                # The gate recorded another request under this id.
                out.signal("source_unreadable")
                continue
            decided = _stamp(answer.get("answered_at"))
            decision = answer.get("decision")
        out.moments += [m for m in (opened, decided) if m is not None]
        out.intervals.append(
            Interval(
                "question" if question else "approval",
                opened,
                decided,
                key=f"approval:{request_id}",
            )
        )
        if question:
            out.signal("question_asked")
        elif decided is None:
            out.signal("approval_held")
        elif decision != "approve":
            # A signed answer that isn't "approve": a person said no.
            out.signal("person_declined")
    return out


def collect_task_ledger(path: Path) -> Contribution:
    """A task-ledger job (``openadapt.task-ledger/v1``): its questions.

    Only ``asked_at``, ``answered_at`` and ``request_id`` are read. A question
    without ``answered_at`` (waiting, expired or refused) is an open interval.
    """

    name = "task_ledger"
    try:
        doc = _read_json(path)
    except _Unreadable:
        return _refused(name, path, "source_unreadable")
    refused = _schema_check(doc, "schema_version", TASK_LEDGER_SCHEMA, name, path)
    if refused is not None:
        return refused
    out = Contribution(name, files=[path])
    questions = doc.get("questions")
    for question in (questions or {}).values() if isinstance(questions, dict) else []:
        if not isinstance(question, dict):
            continue
        opened = _stamp(question.get("asked_at"))
        if opened is None:
            out.signal("source_unreadable")
            continue
        request_id = question.get("request_id")
        out.intervals.append(
            Interval(
                "question",
                opened,
                _stamp(question.get("answered_at")),
                key=f"approval:{request_id}" if isinstance(request_id, str) else "",
            )
        )
        out.signal("question_asked")
    return out


# -- compile-agent ----------------------------------------------------------------


def _agent_usage(agent: Mapping[str, Any]) -> Usage:
    calls = _int(agent.get("model_calls"))
    tokens_in = _int(agent.get("input_tokens"))
    tokens_out = _int(agent.get("output_tokens"))
    per_call = agent.get("calls")
    if (tokens_in is None or tokens_out is None) and isinstance(per_call, list):
        ins = [_int(c.get("input_tokens")) for c in per_call if isinstance(c, dict)]
        outs = [_int(c.get("output_tokens")) for c in per_call if isinstance(c, dict)]
        if per_call and len(ins) == len(per_call) and None not in ins + outs:
            tokens_in = sum(i for i in ins if i is not None)
            tokens_out = sum(o for o in outs if o is not None)
    cost = _float(agent.get("cost_usd"))
    if agent.get("usage_known") is False:
        cost = None
    return Usage(
        calls=calls,
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        cost_usd=cost,
        cost_source="metered" if cost is not None else "unknown",
    )


def collect_compile_agent(path: Path) -> Contribution:
    """compile-agent's ``run1.json`` (``agent`` is ``AgentLedger.to_dict()``)
    or a bare ``agent_ledger.json``.

    The agent's ``finished`` flag is its own judgement (``basis="model"``): it
    never verifies anything and is ignored when any deterministic source
    speaks. A gate refusal in ``gates`` is a catch.
    """

    name = "compile_agent"
    try:
        doc = _read_json(path)
    except _Unreadable:
        return _refused(name, path, "source_unreadable")
    if not isinstance(doc, dict):
        return _refused(name, path, "source_unreadable")
    if "schema" in doc or "schema_version" in doc:
        return _refused(name, path, "schema_unrecognised")
    agent = doc.get("agent") if path.name == "run1.json" else doc
    if not isinstance(agent, dict) or "model_calls" not in agent:
        return _refused(name, path, "schema_unrecognised")
    out = Contribution(name, files=[path], executor_kind="agent")
    out.executor_label = _label(agent.get("agent")) or _label(agent.get("model"))
    out.usage = _agent_usage(agent)
    out.started_at = _epoch(agent.get("started_at"))
    out.ended_at = _epoch(agent.get("finished_at"))
    out.duration_s = _float(agent.get("wall_seconds")) or _float(doc.get("seconds"))
    for gate in doc.get("gates") or []:
        decision = str(gate.get("decision", "")) if isinstance(gate, dict) else ""
        if decision == "refused_by_hold":
            # The person at the gate said no: a decline, not a catch.
            out.signal("person_declined")
        elif decision.startswith("refused"):
            out.signal("gate_refused")
    if agent.get("finished") is True:
        out.signal("readback_verified", "model")
    elif agent.get("finished") is False:
        out.signal("agent_gave_up", "model")
    return out


# -- contract-only sources (sibling pieces) -----------------------------------------


def collect_proposed_write(path: Path) -> Contribution:
    """A lone ``proposed_write.json`` (``openadapt.proposed-write/v1``), read
    outside a readiness run directory.

    Reads ``task_id`` and ``fields[].name`` / ``value``. Nothing compared it
    with what the person saved (that is ``case.json``, read with the run
    directory), so every field is ``not_checked`` and the run is
    ``shadow_not_compared``. Values go to the private sidecar only.
    """

    name = "proposed_write"
    try:
        doc = _read_json(path)
    except _Unreadable:
        return _refused(name, path, "source_unreadable")
    refused = _schema_check(doc, "schema", PROPOSED_WRITE_SCHEMA, name, path)
    if refused is not None:
        return refused
    out = Contribution(name, files=[path], executor_kind="agent")
    if (tid := _task_id(doc.get("task_id"))) is not None:
        out.task_ids.append(tid)
    for item in doc.get("fields") or []:
        if (
            not isinstance(item, dict)
            or (fname := _field_name(item.get("name"))) is None
        ):
            out.signal("source_unreadable")
            continue
        out.fields[fname] = "not_checked"
        out.private_fields[fname] = {"intended": item.get("value"), "saved": None}
    out.signal("shadow_not_compared")
    return out


def collect_flight(path: Path) -> Contribution:
    """``flight.json`` (``openadapt.flight-run/v1``): one recorded agent run.

    Reads ``task_id``, ``executor_label``, ``started_at``, ``ended_at``,
    ``model`` and ``gate_events``. A ``held`` gate event with no later
    ``allowed`` or ``refused`` is a pending hold (``gate_held``); a ``refused``
    one is a catch (``gate_refused``). ``timeline_path`` isn't read.
    """

    name = "flight"
    try:
        doc = _read_json(path)
    except _Unreadable:
        return _refused(name, path, "source_unreadable")
    refused = _schema_check(doc, "schema", FLIGHT_SCHEMA, name, path)
    if refused is not None:
        return refused
    out = Contribution(name, files=[path], executor_kind="agent")
    if (tid := _task_id(doc.get("task_id"))) is not None:
        out.task_ids.append(tid)
    out.executor_label = _label(doc.get("executor_label"))
    out.started_at = _stamp(doc.get("started_at"))
    out.ended_at = _stamp(doc.get("ended_at"))
    model = doc.get("model")
    if isinstance(model, dict):
        cost = _float(model.get("cost_usd"))
        source = model.get("cost_source")
        out.usage = Usage(
            calls=_int(model.get("calls")),
            input_tokens=_int(model.get("input_tokens")),
            output_tokens=_int(model.get("output_tokens")),
            cost_usd=cost,
            cost_source=(
                str(source)
                if cost is not None and source in ("metered", "reported")
                else "reported"
                if cost is not None
                else "unknown"
            ),
        )
    pending = False
    # A flight with no gate events never stopped for a person.
    out.no_person = doc.get("gate_events") == []
    for event in doc.get("gate_events") or []:
        kind = event.get("kind") if isinstance(event, dict) else None
        if kind == "held":
            pending = True
        elif kind == "allowed":
            pending = False
        elif kind == "refused":
            pending = False
            out.signal("gate_refused")
    if pending:
        out.signal("gate_held")
    return out


def collect_guard_ledger_row(path: Path) -> Contribution:
    """``guard_ledger_row.json`` (``openadapt.guard-ledger-row/v1``, P5, J3).

    Reads ``task_id``, ``fields_checked`` and ``fields_differing`` (lists of
    names), ``approval_opened_at``, ``approval_decided_at`` and
    ``readback_tier``. ``verdict`` is a bucket name P5 chose and is never a
    bucket source here, and neither is anything else in the row: the guard's
    ``summary.json`` and the read-back decide (J3). A differing field shows
    as ``differs``.
    ``approver_kind``, ``hold_ms`` and ``diff_path`` have no place in the row
    and aren't read.
    """

    name = "guard_ledger_row"
    try:
        doc = _read_json(path)
    except _Unreadable:
        return _refused(name, path, "source_unreadable")
    refused = _schema_check(doc, "schema", GUARD_LEDGER_ROW_SCHEMA, name, path)
    if refused is not None:
        return refused
    out = Contribution(name, files=[path])
    if (tid := _task_id(doc.get("task_id"))) is not None:
        out.task_ids.append(tid)
    differing_raw = doc.get("fields_differing")
    differing = (
        {n for n in map(_field_name, differing_raw or []) if n}
        if isinstance(differing_raw, list)
        else set()
    )
    checked = doc.get("fields_checked")
    for fname in (
        [n for n in map(_field_name, checked or []) if n]
        if isinstance(checked, list)
        else []
    ):
        out.fields[fname] = "differs" if fname in differing else "matched"
    for fname in differing:
        out.fields[fname] = "differs"
    out.fields_differing = len(differing)
    tier = doc.get("readback_tier")
    if tier in _TIER_RANK:
        out.readback_tiers.append(str(tier))
    opened = _stamp(doc.get("approval_opened_at"))
    if opened is not None:
        decided = _stamp(doc.get("approval_decided_at"))
        out.moments += [m for m in (opened, decided) if m is not None]
        out.intervals.append(Interval("approval", opened, decided, key=""))
    # No signal: J3 makes the row a source of names, times and the tier only.
    # The guard's summary.json says REFUTED when its read-back differs.
    return out


def collect_exception_answer(path: Path) -> Contribution:
    """``answer-<id>.json`` (``openadapt.exception-answer/v1``) from P6's
    ``$OPENADAPT_HOME/exceptions/<run_ref>/`` (read through
    :func:`openadapt_flow.run_ledger.readers.collect_exception_folder`).

    Reads ``kind``, ``opened_at``, ``decided_at``, ``answer`` and
    ``approver_kind``. A ``second_factor`` kind is a second-factor interval,
    anything else an exception interval; the person's time counts either way.

    The bucket: a person who cleared the exception (``continue`` or
    ``done_by_person``, decided) let the run go on, as an approved approval
    does, so the answer is no bucket source: the run's own result decides
    (a save read back independently is VERIFIED). An exception the person
    stopped (``skip``, ``abort``, ``decline``), one that expired, or one not
    answered yet is ESCALATED (``second_factor`` / ``exception_card``).
    """

    name = "exception"
    try:
        doc = _read_json(path)
    except _Unreadable:
        return _refused(name, path, "source_unreadable")
    refused = _schema_check(doc, "schema", EXCEPTION_ANSWER_SCHEMA, name, path)
    if refused is not None:
        return refused
    out = Contribution(name, files=[path])
    opened = _stamp(doc.get("opened_at"))
    if opened is None:
        out.signal("source_unreadable")
        return out
    decided_at = _stamp(doc.get("decided_at"))
    out.moments += [m for m in (opened, decided_at) if m is not None]
    second = doc.get("kind") == "second_factor"
    card = doc.get("card_id")
    out.intervals.append(
        Interval(
            "second_factor" if second else "exception",
            opened,
            _stamp(doc.get("decided_at")),
            key=f"exception:{card}" if isinstance(card, str) else "",
        )
    )
    cleared = (
        doc.get("answer") in ("continue", "done_by_person")
        and doc.get("approver_kind") == "person"
        and decided_at is not None
    )
    if not cleared:
        out.signal("second_factor" if second else "exception_card")
    return out


# -- the walk -----------------------------------------------------------------------


def _worse(current: Optional[str], new: str) -> str:
    if current is None:
        return new
    return new if _CHECK_RANK[new] > _CHECK_RANK[current] else current


#: The only files the ledger reads, by name: what the producers it knows
#: write. Anything else in a run directory (P3's ``egress/pseudonyms.json``,
#: which holds real values; screenshots; logs; a stray file) is never
#: opened, digested, copied to a sidecar or sent.
ALLOWED_NAMES = frozenset(
    {
        "summary.json",
        "guard_events.jsonl",
        "effect-receipt.json",
        "report.json",
        "receipt.json",
        "run1.json",
        "agent_ledger.json",
        "proposed_write.json",
        "flight.json",
        "guard_ledger_row.json",
        "result.json",
        # P1 readiness run directory
        "run.json",
        "case.json",
        "usage.json",
        "egress.json",
        # the run's own config (declared field names and task ids)
        "task.json",
        "guard_task.json",
        "verify.yaml",
    }
)
#: Allowed by folder and name pattern: (parent folder name, prefix, suffix).
_ALLOWED_PATTERNS = (
    ("item-approval", "request-", ".json"),
    ("item-approval", "answer-", ".json"),
    ("jobs", "job-", ".json"),  # task ledger: job-<hex>.json
    ("runs", "run-", ".json"),  # wrap's run store: run-<hex>.json
    ("egress", "timeline", ".jsonl"),
    # Only created_at is read (the guard's recording metadata).
    ("recording", "meta", ".json"),
    # The shadow hold's held proposals: only held_at is read.
    ("held", "", ".json"),
)
#: A file named directly (``ledger add FILE``) may also be a wrap run record
#: (``run-<id>.json``) or a ``wrap-dispatch --result`` envelope.
_DIRECT_FILE = re.compile(
    r"^(run-[A-Za-z0-9._-]+|[A-Za-z0-9._-]*(result|envelope)[A-Za-z0-9._-]*)\.json$"
)


def allowed_file(path: Path, *, direct: bool = False) -> bool:
    """Whether the ledger may read ``path`` at all (see :data:`ALLOWED_NAMES`)."""

    name, parent = path.name, path.parent.name
    if name == "timeline.jsonl":
        return parent == "egress"
    if name in ALLOWED_NAMES:
        return True
    for folder, prefix, suffix in _ALLOWED_PATTERNS:
        if parent == folder and name.startswith(prefix) and name.endswith(suffix):
            return True
    return direct and bool(_DIRECT_FILE.match(name))


def _files(root: Path) -> list[Path]:
    if root.is_file():
        return [root] if allowed_file(root, direct=True) else []
    found: list[Path] = []
    stack = [(root, 0)]
    while stack and len(found) < MAX_FILES:
        folder, depth = stack.pop()
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if depth < MAX_DEPTH:
                    stack.append((entry, depth + 1))
            elif entry.is_file() and allowed_file(entry):
                found.append(entry)
    return sorted(found)


def _has_schema(path: Path, key: str, family: str) -> bool:
    """Cheap test that a JSON file claims a schema of ``family`` (any version)."""

    try:
        doc = _read_json(path)
    except _Unreadable:
        return False
    value = doc.get(key) if isinstance(doc, dict) else None
    return isinstance(value, str) and value.startswith(family)


Dispatch = Callable[[Path], Optional[Contribution]]


def _dispatch(path: Path) -> Optional[Contribution]:
    name = path.name
    parent = path.parent.name
    if name == "summary.json":
        if _has_schema(path, "schema", "openadapt.agent-guard-run/"):
            return collect_agent_guard(path)
        return None
    if name == "effect-receipt.json":
        return collect_effect_receipt(path)
    if name == "report.json":
        return collect_run_report(path)
    if name == "receipt.json":
        if (path.parent / "report.json").is_file():
            return None  # read with its report
        if _has_schema(path, "schema_version", "openadapt.run-receipt/"):
            return collect_run_receipt(path)
        return None
    if name == "run1.json" or name == "agent_ledger.json":
        return collect_compile_agent(path)
    if name == "proposed_write.json":
        return collect_proposed_write(path)
    if name == "flight.json":
        return collect_flight(path)
    if name == "guard_ledger_row.json":
        return collect_guard_ledger_row(path)
    if name == "result.json":
        from openadapt_flow.run_ledger.readers import (
            collect_fallback_result,
            is_dispatch_result,
        )

        try:
            doc = _read_json(path)
        except _Unreadable:
            return None
        return collect_fallback_result(path) if is_dispatch_result(doc) else None
    if parent == "jobs" and name.endswith(".json"):
        if _has_schema(path, "schema_version", "openadapt.task-ledger/"):
            return collect_task_ledger(path)
        return None
    if name.endswith(".json") and parent == "runs":
        if _has_schema(path, "schema", "openadapt-agent.run/"):
            return collect_agent_run_record(path)
    return None


@dataclass
class Collected:
    """Every contribution for one run, plus the files they came from."""

    root: Path
    contributions: list[Contribution]
    #: Field names the run's guard task or verify.yaml declares.
    config_fields: set[str] = field(default_factory=set)
    #: Config files named with ``ledger add --config``, outside the run.
    config_paths: list[Path] = field(default_factory=list)

    @property
    def declared_fields(self) -> set[str]:
        """Every field name the run's own config declares (finding a)."""

        names = set(self.config_fields)
        for contribution in self.contributions:
            names |= contribution.declared_fields
        return names

    @property
    def files(self) -> list[Path]:
        seen: dict[Path, None] = dict.fromkeys(self.config_paths)
        for contribution in self.contributions:
            for path in contribution.files:
                seen.setdefault(path, None)
        return sorted(seen)

    def relative(self, path: Path) -> str:
        base = self.root if self.root.is_dir() else self.root.parent
        try:
            return path.resolve().relative_to(base.resolve()).as_posix()
        except ValueError:
            return path.name

    def digest(self) -> str:
        """sha256 over each source file's relative path and bytes. The private
        sidecar never lives in the run, so a second ``add`` gets the same one."""

        outer = hashlib.sha256()
        for path in self.files:
            try:
                data = path.read_bytes()
            except OSError:
                data = b""
            outer.update(self.relative(path).encode("utf-8") + b"\0")
            outer.update(hashlib.sha256(data).digest())
        return outer.hexdigest()


def _skipped(files: list[Path], run: Path) -> tuple[list[Path], list[Path]]:
    """Files no collector reads on their own, and readiness shadow run dirs.

    A readiness run directory is read as one source (J1); nothing else in it
    is dispatched. A P7 fallback ``result.json`` speaks for the agent run
    under its ``run/`` folder (J5), so those files aren't read again.
    """

    from openadapt_flow.run_ledger.readers import is_dispatch_result, is_shadow_run

    shadows = sorted(
        {p.parent for p in files if p.name == "run.json" and is_shadow_run(p.parent)}
    )
    covered = list(shadows)
    for path in files:
        if path.name == "result.json" and (path.parent / "run").is_dir():
            try:
                doc = _read_json(path)
            except _Unreadable:
                continue
            if is_dispatch_result(doc):
                covered.append(path.parent / "run")
    kept = [p for p in files if not any(p.is_relative_to(folder) for folder in covered)]
    return kept, shadows


def collect(
    run: Path,
    *,
    exceptions: Optional[Path] = None,
    config: Iterable[Path] = (),
    approvals: Optional[Path] = None,
) -> Collected:
    """Every source in ``run`` (a run directory or one file), plus P6's
    exception folder for the run when ``exceptions`` names one. ``config``
    names the run's guard task or ``verify.yaml`` when they live outside the
    run directory (their field names are the ones ``ledger export`` may
    let through)."""

    from openadapt_flow.run_ledger.readers import (
        collect_exception_folder,
        collect_shadow_run,
        config_files,
        declared_fields,
    )

    run = Path(run)
    # Only a guard task (task.json, guard_task.json) or a verify.yaml.
    config = [Path(c) for c in config if allowed_file(Path(c)) and Path(c).is_file()]
    contributions: list[Contribution] = []
    approval_folders: set[Path] = set()
    found = _files(run)
    files, shadows = _skipped(found, run)
    for folder in shadows:
        contributions.append(collect_shadow_run(folder))
    for path in files:
        if path.parent.name == "item-approval" and path.name.startswith("request-"):
            approval_folders.add(path.parent)
            continue
        contribution = _dispatch(path)
        if contribution is None and path == run:
            from openadapt_flow.run_ledger.readers import (
                collect_fallback_result,
                is_dispatch_result,
            )

            try:
                doc = _read_json(path)
            except _Unreadable:
                doc = None
            if isinstance(doc, dict) and str(doc.get("schema", "")).startswith(
                "openadapt-agent.run/"
            ):
                # One wrap run record named directly, wherever it was copied.
                contribution = collect_agent_run_record(path)
            elif is_dispatch_result(doc):
                # A wrap-dispatch envelope saved with --result FILE.
                contribution = collect_fallback_result(path)
        if contribution is not None:
            contributions.append(contribution)
    for folder in sorted(approval_folders):
        contribution = collect_item_approvals(folder)
        if contribution.files:
            contributions.append(contribution)
    if exceptions is not None:
        contribution = collect_exception_folder(exceptions)
        if contribution is not None:
            contributions.append(contribution)
    from openadapt_flow.run_ledger.readers import fallback_neighbours, record_times

    for contribution in contributions:
        if contribution.run_record_id and contribution.started_at is None:
            contribution.started_at, contribution.ended_at, record = record_times(
                contribution, run
            )
            if record is not None:
                contribution.files.append(record)

    for fallback in [c for c in contributions if c.approvals_key]:
        # P7's item-approval gate: <approvals>/<fallback request id>/item-approval,
        # given with --approvals or found near the run by that exact id.
        approval_dir, result_extra = fallback_neighbours(
            fallback, run, Path(approvals) if approvals is not None else None
        )
        if approval_dir is not None:
            contribution = collect_item_approvals(
                approval_dir, only=fallback.gate_requests or None
            )
            if contribution.files:
                contributions.append(contribution)
        if result_extra is not None:
            contributions.append(result_extra)
            config = [*config, *result_extra.config_paths]
    opened = sorted(
        i.opened_at
        for c in contributions
        if c.collector in ("item_approval", "guard_ledger_row")
        for i in c.intervals
        if i.kind == "approval"
    )
    for contribution in contributions:
        if contribution.anchor_t is not None and opened:
            start = datetime.fromisoformat(opened[0].replace("Z", "+00:00"))
            begin = start - timedelta(seconds=contribution.anchor_t)
            contribution.started_at = _iso(begin)
            if contribution.duration_s is not None:
                contribution.ended_at = _iso(
                    begin + timedelta(seconds=contribution.duration_s)
                )
    collected = Collected(
        root=run,
        contributions=contributions,
        config_fields=declared_fields(
            [*config_files(files), *config],
            [tid for c in contributions for tid in c.task_ids],
        ),
        config_paths=[p for p in config if p.is_file()],
    )
    declared = collected.declared_fields
    for contribution in contributions:
        if contribution.readback_fields and declared:
            # A read-back lists every field of the new record (id, key ...);
            # keep the ones the run's own config says it writes.
            contribution.fields = {
                n: c for n, c in contribution.fields.items() if n in declared
            }
    return collected


__all__ = [
    "Collected",
    "Contribution",
    "Interval",
    "Usage",
    "collect",
    "collect_agent_guard",
    "collect_agent_run_record",
    "collect_compile_agent",
    "collect_effect_receipt",
    "collect_exception_answer",
    "collect_flight",
    "collect_guard_ledger_row",
    "collect_item_approvals",
    "collect_proposed_write",
    "collect_run_receipt",
    "collect_run_report",
    "collect_task_ledger",
]

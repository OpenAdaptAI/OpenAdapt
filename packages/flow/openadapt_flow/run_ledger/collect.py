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

Contract-only sources, produced by sibling pieces: ``proposed_write.json``
(``openadapt.proposed-write/v1``), ``scorecard.json``
(``openadapt.readiness-scorecard/v1``), ``flight.json``
(``openadapt.flight-run/v1``), ``guard_ledger_row.json``
(``openadapt.guard-ledger-row/v1``) and ``exceptions/answer-*.json``
(``openadapt.exception-answer/v1``). Their collectors read exactly the keys
``docs/RUN_LEDGER.md`` lists.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

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
SCORECARD_SCHEMA = "openadapt.readiness-scorecard/v1"
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
    tier = receipt.get("verification_tier")
    if tier in _TIER_RANK:
        out.readback_tiers.append(str(tier))
    reason = outcome_reason(EXECUTE_OUTCOME_REASON, receipt.get("outcome"))
    if reason == "readback_verified":
        reason = verified_or_tier(tier if isinstance(tier, str) else None)
    out.signal(reason)
    effect = outcome_reason(EFFECT_VERDICT_REASON, receipt.get("effect_verdict"))
    if effect == "readback_refuted":
        out.signal(effect)
    return out


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
        tier = CHECKED_HOW_TIER.get(readback.get("checked_how"))  # type: ignore[call-overload]
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


def collect_item_approvals(folder: Path) -> Contribution:
    """``item-approval/request-<id>.json`` and ``answer-<id>.json``.

    One interval per request, from the request's ``created_at`` to the
    answer's ``answered_at``. A request with no answer is still open: the row's
    intervention time is then unknown, and the run is ESCALATED
    (``approval_held``). A declined save is ``approval_declined``. A ledger
    question (``workflow_name`` ``openadapt.task-ledger.question``) is a
    ``question`` interval.
    """

    name = "item_approval"
    out = Contribution(name)
    for request_path in sorted(folder.glob("request-*.json")):
        request_id = request_path.name[len("request-") : -len(".json")]
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
            decided = _stamp(answer.get("answered_at"))
            decision = answer.get("decision")
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
            out.signal("approval_declined")
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
        if isinstance(gate, dict) and str(gate.get("decision", "")).startswith(
            "refused"
        ):
            out.signal("gate_refused")
    if agent.get("finished") is True:
        out.signal("readback_verified", "model")
    elif agent.get("finished") is False:
        out.signal("agent_gave_up", "model")
    return out


# -- contract-only sources (sibling pieces) -----------------------------------------


def collect_proposed_write(path: Path) -> Contribution:
    """``proposed_write.json`` (``openadapt.proposed-write/v1``), shadow mode.

    Reads ``task_id`` and ``fields: [{name, proposed, saved}]``: what the
    candidate would have saved and what the person saved. A missing ``saved``
    is ``not_checked``. Values go to the private sidecar only. Nothing was
    written by the candidate, so a full match is ``halted_before_effect``.
    """

    name = "proposed_write"
    try:
        doc = _read_json(path)
    except _Unreadable:
        return _refused(name, path, "source_unreadable")
    refused = _schema_check(doc, "schema", PROPOSED_WRITE_SCHEMA, name, path)
    if refused is not None:
        return refused
    out = Contribution(name, files=[path])
    if (tid := _task_id(doc.get("task_id"))) is not None:
        out.task_ids.append(tid)
    for item in doc.get("fields") or []:
        if (
            not isinstance(item, dict)
            or (fname := _field_name(item.get("name"))) is None
        ):
            out.signal("source_unreadable")
            continue
        proposed = item.get("proposed")
        if "saved" not in item:
            check = "not_checked"
        else:
            check = "matched" if item["saved"] == proposed else "differs"
        out.fields[fname] = _worse(out.fields.get(fname), check)
        out.private_fields[fname] = {"intended": proposed, "saved": item.get("saved")}
    out.fields_differing = sum(1 for c in out.fields.values() if c == "differs")
    if out.fields_differing:
        out.signal("shadow_field_mismatch")
    elif out.fields and all(c == "matched" for c in out.fields.values()):
        out.signal("halted_before_effect")
    else:
        out.signal("completed_unverified")
    return out


def collect_scorecard(path: Path, task_ids: Iterable[str]) -> Contribution:
    """``scorecard.json`` (``openadapt.readiness-scorecard/v1``).

    A scorecard covers many tasks. Only ``tasks: [{task_id, matched}]`` is
    read, and only the entry for this run's task; ``matched: false`` is a
    shadow-mode mismatch.
    """

    name = "scorecard"
    try:
        doc = _read_json(path)
    except _Unreadable:
        return _refused(name, path, "source_unreadable")
    refused = _schema_check(doc, "schema", SCORECARD_SCHEMA, name, path)
    if refused is not None:
        return refused
    out = Contribution(name, files=[path])
    wanted = set(task_ids)
    for entry in doc.get("tasks") or []:
        if isinstance(entry, dict) and entry.get("task_id") in wanted:
            if entry.get("matched") is False:
                out.signal("shadow_field_mismatch")
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
    """``guard_ledger_row.json`` (``openadapt.guard-ledger-row/v1``).

    Reads ``task_id``, ``verdict``, ``fields_checked`` (a list of names, or a
    count), ``fields_differing``, ``approval_opened_at``,
    ``approval_decided_at`` and ``readback_tier``. ``diff_path`` and
    ``approver_kind`` aren't read. When some fields differ but the row doesn't
    say which, each named field is ``not_checked`` and the count stands.
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
    differing = _int(doc.get("fields_differing")) or 0
    out.fields_differing = differing
    checked = doc.get("fields_checked")
    if isinstance(checked, list):
        for item in checked:
            if (fname := _field_name(item)) is not None:
                out.fields[fname] = "matched" if differing == 0 else "not_checked"
    tier = doc.get("readback_tier")
    if tier in _TIER_RANK:
        out.readback_tiers.append(str(tier))
    opened = _stamp(doc.get("approval_opened_at"))
    if opened is not None:
        out.intervals.append(
            Interval("approval", opened, _stamp(doc.get("approval_decided_at")), key="")
        )
    reason = outcome_reason(GUARD_VERDICT_REASON, doc.get("verdict"))
    if reason == "readback_verified":
        reason = verified_or_tier(tier if isinstance(tier, str) else None)
    out.signal(reason)
    if differing:
        out.signal("readback_refuted")
    return out


def collect_exception_answer(path: Path) -> Contribution:
    """``exceptions/answer-<id>.json`` (``openadapt.exception-answer/v1``).

    Reads ``kind``, ``opened_at`` and ``decided_at``; ``answer`` isn't read. A
    ``second_factor`` kind is a second-factor interval, anything else an
    exception interval. Either way a person was needed: ESCALATED.
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
    out.signal("second_factor" if second else "exception_card")
    return out


# -- the walk -----------------------------------------------------------------------


def _worse(current: Optional[str], new: str) -> str:
    if current is None:
        return new
    return new if _CHECK_RANK[new] > _CHECK_RANK[current] else current


def _files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
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
            elif entry.is_file():
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
    if parent == "exceptions" and name.startswith("answer-") and name.endswith(".json"):
        return collect_exception_answer(path)
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

    @property
    def files(self) -> list[Path]:
        seen: dict[Path, None] = {}
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


def collect(run: Path) -> Collected:
    """Every source in ``run`` (a run directory or one file)."""

    run = Path(run)
    contributions: list[Contribution] = []
    approvals: set[Path] = set()
    scorecards: list[Path] = []
    for path in _files(run):
        if path.parent.name == "item-approval" and path.name.startswith("request-"):
            approvals.add(path.parent)
            continue
        if path.name == "scorecard.json":
            scorecards.append(path)
            continue
        contribution = _dispatch(path)
        if (
            contribution is None
            and path == run
            and _has_schema(path, "schema", "openadapt-agent.run/")
        ):
            # One wrap run record named directly, wherever it was copied.
            contribution = collect_agent_run_record(path)
        if contribution is not None:
            contributions.append(contribution)
    for folder in sorted(approvals):
        contribution = collect_item_approvals(folder)
        if contribution.files:
            contributions.append(contribution)
    task_ids = [tid for c in contributions for tid in c.task_ids]
    for path in scorecards:
        contributions.append(collect_scorecard(path, task_ids))
    return Collected(root=run, contributions=contributions)


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
    "collect_scorecard",
    "collect_task_ledger",
]

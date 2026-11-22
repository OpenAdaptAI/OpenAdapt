"""Readers for the agent-control-plane pieces' files (joins J1, J4 and J5).

Each reads exactly what its producer writes (synthetic copies are under
``tests/fixtures/run_ledger``) and returns a :class:`Contribution`. The bucket
is decided only in :mod:`openadapt_flow.run_ledger.outcome`.

* :func:`collect_shadow_run`: a readiness task run directory (P1): ``run.json``
  (``openadapt.readiness-task-run/v1``), ``proposed_write.json``,
  ``case.json`` (``openadapt.readiness-case/v1``), ``usage.json``
  (``openadapt.agent-usage/v1``) and ``egress.json`` (P3's egress summary)
  when the agent ran behind the flight proxy. Nothing else in the directory
  is read: a ``guard_ledger_row.json`` or ``guard_events.jsonl`` there is the
  shadow hold's, not a save's.
* :func:`collect_fallback_result`: P7's fallback ``result.json`` and the
  ``wrap-dispatch`` result envelope. The envelope's ``outcome`` (a bucket
  name) is not read; the bucket comes from ``status``, ``verified`` and the
  record check.
* :func:`collect_exception_folder`: P6's
  ``$OPENADAPT_HOME/exceptions/<run_ref>/`` (``card-*.json`` and
  ``answer-*.json``).
* :func:`declared_fields`: the field names the run's own task or verify
  config declares, which ``ledger export`` lets through the scrubber check.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from openadapt_flow.run_ledger.collect import (
    Contribution,
    Interval,
    Usage,
    _epoch,
    _field_name,
    _float,
    _int,
    _label,
    _read_json,
    _refused,
    _stamp,
    _task_id,
    _Unreadable,
    _worse,
    collect_exception_answer,
)
from openadapt_flow.run_ledger.outcome import (
    EXECUTE_OUTCOME_REASON,
    WRAP_STATUS_REASON,
    fallback_reason,
    outcome_reason,
    shadow_reason,
)

READINESS_RUN_SCHEMA = "openadapt.readiness-task-run/v1"
READINESS_CASE_SCHEMA = "openadapt.readiness-case/v1"
AGENT_USAGE_SCHEMA = "openadapt.agent-usage/v1"
PROPOSED_WRITE_SCHEMA = "openadapt.proposed-write/v1"
EXCEPTION_CARD_SCHEMA = "openadapt.exception-card/v1"
GUARD_TASK_SCHEMA = "openadapt.guard-task/v1"
VERIFY_SCHEMA = "openadapt.verify/v1"

#: P7's ``FALLBACK_PATH`` and the dispatcher's other ``path`` values.
_DISPATCH_PATHS = ("agent_fallback", "program", "held")
#: ``readiness/compare.py`` field kinds, as ledger field checks.
_CASE_KIND_CHECK = {
    "matched": "matched",
    "mismatched": "differs",
    "missing": "differs",
    "extra": "differs",
    "wrong_record": "differs",
    "uncompared": "not_checked",
}


def is_shadow_run(folder: Path) -> bool:
    """A directory with a readiness ``run.json`` is a shadow run (J1)."""

    path = folder / "run.json"
    if not path.is_file() or path.is_symlink():
        return False
    try:
        doc = _read_json(path)
    except _Unreadable:
        return False
    return isinstance(doc, dict) and str(doc.get("schema", "")).startswith(
        "openadapt.readiness-task-run/"
    )


def _optional(folder: Path, name: str, schema: str, out: Contribution) -> Any:
    """``folder/name`` when present and of ``schema``; ``None`` when absent.
    A present file that can't be read or claims another version is a
    ``schema_unrecognised`` / ``source_unreadable`` signal."""

    path = folder / name
    if not path.is_file() or path.is_symlink():
        return None
    out.files.append(path)
    try:
        doc = _read_json(path)
    except _Unreadable:
        out.signal("source_unreadable")
        return None
    if not isinstance(doc, dict) or doc.get("schema") != schema:
        out.signal("schema_unrecognised")
        return None
    return doc


def _timeline_moments(path: Path, out: Contribution) -> list[str]:
    """The first and last ``at`` in P3's egress ``timeline.jsonl``: when the
    proxy saw the agent's first and last model traffic. Nothing else in the
    timeline is read."""

    if not path.is_file() or path.is_symlink() or path.name != "timeline.jsonl":
        return []
    try:
        if path.stat().st_size > 16 * 1024 * 1024:
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    found: list[str] = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and (moment := _stamp(event.get("at"))):
            found.append(moment)
    if not found:
        return []
    out.files.append(path)
    ordered = sorted(found)
    return [ordered[0], ordered[-1]]


def _usage_from(doc: Mapping[str, Any]) -> Usage:
    cost = _float(doc.get("cost_usd"))
    return Usage(
        calls=_int(doc.get("calls")),
        input_tokens=_int(doc.get("input_tokens")),
        output_tokens=_int(doc.get("output_tokens")),
        cost_usd=cost,
        cost_source="reported" if cost is not None else "unknown",
    )


def _egress_usage(doc: Mapping[str, Any], base: Optional[Usage]) -> Usage:
    """P3's egress summary (J7): the proxy's own count of model calls and
    cost. Metered cost beats the agent's report; tokens stay the agent's."""

    out = Usage(**vars(base)) if base is not None else Usage()
    calls = _int(doc.get("model_calls"))
    if calls is not None:
        out.calls = calls
    cost = _float(doc.get("cost_usd"))
    if cost is not None:
        out.cost_usd = cost
        out.cost_source = (
            "metered" if doc.get("cost_source") == "metered" else "reported"
        )
    return out


def collect_shadow_run(folder: Path) -> Contribution:
    """One readiness task run directory (P1, J1). See the module docstring.

    Duration is ``agent_wall_s``. Model usage comes from ``run.json``
    ``usage`` (P1's copy, which carries the priced cost), else
    ``usage.json``; both are the agent's own report (``reported``). An
    ``egress.json`` replaces calls and cost with the proxy's metered count.
    Field checks and the private values come from ``case.json``; without it
    every proposed field is ``not_checked`` and the run is
    ``shadow_not_compared``.
    """

    name = "readiness_shadow"
    run_path = folder / "run.json"
    try:
        run = _read_json(run_path)
    except _Unreadable:
        return _refused(name, run_path, "source_unreadable")
    if not isinstance(run, dict) or run.get("schema") != READINESS_RUN_SCHEMA:
        return _refused(name, run_path, "schema_unrecognised")
    out = Contribution(name, files=[run_path], executor_kind="agent")
    if (tid := _task_id(run.get("task_id"))) is not None:
        out.task_ids.append(tid)
    out.duration_s = _float(run.get("agent_wall_s"))

    usage_doc = _optional(folder, "usage.json", AGENT_USAGE_SCHEMA, out)
    run_usage = run.get("usage")
    usage: Optional[Usage] = None
    if isinstance(run_usage, dict):
        usage = _usage_from(run_usage)
        if usage.cost_usd is None and _float(run.get("model_cost_usd")) is not None:
            usage.cost_usd = _float(run.get("model_cost_usd"))
            usage.cost_source = "reported"
    elif usage_doc is not None:
        usage = _usage_from(usage_doc)
    model = (run_usage if isinstance(run_usage, dict) else usage_doc or {}).get("model")
    out.executor_label = _label(model) or "readiness-shadow"
    egress_path = folder / "egress.json"
    if egress_path.is_file() and not egress_path.is_symlink():
        out.files.append(egress_path)
        try:
            egress = _read_json(egress_path)
        except _Unreadable:
            egress = None
            out.signal("source_unreadable")
        if isinstance(egress, dict) and "mode" in egress:
            usage = _egress_usage(egress, usage)
        elif egress is not None:
            out.signal("schema_unrecognised")
    else:
        # P1 also copies the egress summary into run.json (``egress``); a
        # refused session there has no ``mode`` and no counts.
        copy = run.get("egress")
        if (
            isinstance(copy, dict)
            and "mode" in copy
            and copy.get("refused") is not True
        ):
            usage = _egress_usage(copy, usage)
    out.usage = usage

    proposed = _optional(folder, "proposed_write.json", PROPOSED_WRITE_SCHEMA, out)
    case = _optional(folder, "case.json", READINESS_CASE_SCHEMA, out)
    proposed_names: list[str] = []
    proposed_values: dict[str, Any] = {}
    if (held := _stamp((proposed or {}).get("held_at"))) is not None:
        out.moments.append(held)  # when the shadow hold stopped the save
    for held_path in sorted((folder / "held").glob("*.json"))[:64]:
        # The hold's own record of each save it stopped (an extra target's
        # too). Only ``held_at`` is read; the file holds the values.
        if held_path.is_symlink() or not held_path.name[:-5].isdigit():
            continue
        try:
            held_doc = _read_json(held_path)
        except _Unreadable:
            continue
        if isinstance(held_doc, dict) and (moment := _stamp(held_doc.get("held_at"))):
            out.moments.append(moment)
            out.files.append(held_path)
    out.moments += _timeline_moments(folder / "egress" / "timeline.jsonl", out)
    for item in (proposed or {}).get("fields") or []:
        if isinstance(item, dict) and (fname := _field_name(item.get("name"))):
            proposed_names.append(fname)
            proposed_values[fname] = item.get("value")
    case_names: set[str] = set()
    compared: dict[str, str] = {}
    # compare.py returns before it builds the field rows for FAILED_SCOPE,
    # a task that didn't run and a run with no proposal; then the case
    # declares nothing about the proposed names.
    rows_built = case is not None and (
        case.get("status")
        in ("MATCHED", "MISMATCHED", "WRONG_RECORD", "NOT_COMPARABLE")
        or (
            case.get("status") == "NOT_CHECKED"
            and case.get("reason") in ("uncompared_fields", "no_reference_fields")
        )
    )
    if case is not None:
        if (tid := _task_id(case.get("task_id"))) is not None:
            out.task_ids.append(tid)
        for row in case.get("fields") or []:
            if (
                not isinstance(row, dict)
                or (fname := _field_name(row.get("name"))) is None
            ):
                continue
            kind = row.get("kind")
            check = _CASE_KIND_CHECK.get(str(kind), "not_checked")
            compared[fname] = _worse(compared.get(fname), check)
            out.private_fields[fname] = {
                "intended": row.get("proposed"),
                "saved": row.get("human"),
            }
            case_names.add(fname)
            # compare.py lists every proposed name the reference doesn't
            # declare as ``extra`` (or ``uncompared`` with no human value).
            undeclared = kind == "extra" or (
                kind == "uncompared" and row.get("human") is None
            )
            if not undeclared:
                out.declared_fields.add(fname)
    for fname in proposed_names:
        if fname in case_names:
            continue
        compared[fname] = _worse(compared.get(fname), "not_checked")
        out.private_fields.setdefault(
            fname, {"intended": proposed_values.get(fname), "saved": None}
        )
        if rows_built:
            # Once compare.py built the rows it lists every proposed field
            # the reference doesn't declare; one it leaves out is an
            # expected-record or ignored field the task declared.
            out.declared_fields.add(fname)
    # A shadow run writes nothing, so the row has no fields_written and no
    # fields_differing: the comparison is in case.json (and the values in
    # the private sidecar), and the reason says whether it matched.
    del compared

    landed = _int(run.get("landed_writes"))
    if case is not None and _int(case.get("landed_writes")):
        landed = max(landed or 0, _int(case.get("landed_writes")) or 0)
    status = case.get("status") if case is not None else None
    run_egress = run.get("egress")
    out.signal(
        shadow_reason(
            status if isinstance(status, str) else None,
            run.get("status") if isinstance(run.get("status"), str) else None,
            landed,
            egress_refused=isinstance(run_egress, dict)
            and run_egress.get("refused") is True,
        )
    )
    # The shadow hold never asks a person: the person's own save is a
    # separate recorded run. Nobody spent time on this one.
    out.no_person = True
    return out


# -- P7 fallback (J5) ---------------------------------------------------------------


def is_dispatch_result(doc: Any) -> bool:
    """P7's fallback ``result.json`` (``SkillRun.to_dict()``, with
    ``replay``) or the ``wrap-dispatch`` envelope (``WrappedTask.call``'s
    result plus ``outcome`` and ``fallback``). Neither has a schema field, so
    both are recognised by shape; compile-agent's ``compiled/result.json``
    has no ``path`` and isn't one."""

    return (
        isinstance(doc, dict)
        and doc.get("path") in _DISPATCH_PATHS
        and isinstance(doc.get("status"), str)
        and "verified" in doc
        and ("replay" in doc or ("outcome" in doc and "fallback" in doc))
    )


def _nothing_written(check: Any) -> bool:
    """P7's ``_nothing_written``: both reads of the record check succeeded
    (``readable: true``) and saw no change. An unreadable store also reports
    zero counts, so a check without ``readable: true`` (an older one, or a
    failed read) proves nothing."""

    return (
        isinstance(check, Mapping)
        and check.get("readable") is True
        and all(
            _int(check.get(key)) == 0 and not isinstance(check.get(key), bool)
            for key in ("created", "changed", "removed")
        )
    )


def _something_written(check: Any) -> bool:
    return isinstance(check, Mapping) and any(
        (_int(check.get(key)) or 0) > 0 for key in ("created", "changed", "removed")
    )


def collect_fallback_result(path: Path) -> Contribution:
    """P7's ``<cache>/fallback/<task>/<id>/result.json`` or a dispatcher
    envelope (J5).

    On ``path: agent_fallback`` the reason comes from
    :func:`~openadapt_flow.run_ledger.outcome.fallback_reason`: ``status``,
    P7's ``verified`` (a passed record check of the task's record and every
    gate approved by the person) and the record check
    (``replay.fallback.task_record_check``). A VERIFIED fallback's record
    check read the system of record before and after, an independent
    read-back. ``replay.program`` (the program's earlier halt) is never read
    for a signal: it is what the fallback answered. Gates carry one time
    each, not an opened/decided pair, so a person's time on them is unknown.

    On ``path: program`` / ``held`` the envelope is read like a wrap result.
    """

    name = "wrap_fallback"
    try:
        doc = _read_json(path)
    except _Unreadable:
        return _refused(name, path, "source_unreadable")
    if not is_dispatch_result(doc):
        return _refused(name, path, "schema_unrecognised")
    out = Contribution(name, files=[path], executor_label="openadapt-wrap")
    run_id = doc.get("run_id")
    if isinstance(run_id, str) and re.fullmatch(r"run-[0-9a-f]{8,64}", run_id):
        out.run_record_id = run_id

    def _dict(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    compile_doc = _dict(doc.get("compile"))
    replay = _dict(doc.get("replay"))
    # result.json keeps the policy's record in replay.fallback; the
    # dispatcher envelope carries the same object as its top-level fallback.
    fallback = _dict(replay.get("fallback")) or _dict(doc.get("fallback"))
    program = _dict(replay.get("program"))
    tid = _task_id(doc.get("request_id")) or _task_id(compile_doc.get("task_key"))
    if tid is not None:
        out.task_ids.append(tid)
    out.duration_s = _float(doc.get("seconds"))
    cost = _float(doc.get("cost_usd")) if doc.get("usage_known") is True else None
    out.usage = Usage(
        calls=_int(doc.get("model_calls")),
        cost_usd=cost,
        cost_source="reported" if cost is not None else "unknown",
    )
    status = doc.get("status")
    if doc.get("path") != "agent_fallback":
        out.executor_kind = "program"
        if status in WRAP_STATUS_REASON:
            out.signal(WRAP_STATUS_REASON[str(status)])
        elif status == "NO_ADMITTED_PROGRAM":
            out.signal("policy_rejected_no_write")  # nothing ran
        elif doc.get("verified") is True and doc.get("receipt_outcome") == "VERIFIED":
            out.readback_tiers.append("independent_readback")
            out.signal("readback_verified")
        elif isinstance(doc.get("receipt_outcome"), str):
            reason = outcome_reason(EXECUTE_OUTCOME_REASON, doc["receipt_outcome"])
            out.signal(
                "completed_unverified" if reason == "readback_verified" else reason
            )
        else:
            out.signal("unmapped_outcome")
        return out

    out.executor_kind = "agent"
    run1 = _dict(compile_doc.get("run1"))
    # An item-approval gate (P7's ``gate_answers``) keeps each save's
    # request in ``<approvals>/<fallback request id>/item-approval/``, with
    # the times a person was asked and answered; ``ledger add --approvals``
    # reads it there. A terminal gate keeps one time per save, not a pair.
    answers = fallback.get("gate_answers")
    key = fallback.get("request_id")
    if isinstance(key, str) and _task_id(key) is not None:
        # The fallback's own request id names its folders: the item-approval
        # requests and, for an envelope, the result.json with field names.
        out.approvals_key = key
    if isinstance(answers, list):
        for answer in answers:
            rid = answer.get("request_id") if isinstance(answer, dict) else None
            if isinstance(rid, str) and re.fullmatch(r"[0-9a-f]{32}", rid):
                sha = answer.get("request_sha256")
                out.gate_requests[rid] = sha if isinstance(sha, str) else None
    for gate in run1.get("gates") or []:
        if not isinstance(gate, dict):
            continue
        at = _epoch(gate.get("at"))
        if at is not None:
            out.moments.append(at)
        if (
            at is not None
            and not out.gate_requests
            and str(gate.get("decision", "")).endswith("_by_hold")
        ):
            out.intervals.append(Interval("approval", at, None, key=f"gate:{at}"))
    check = fallback.get("task_record_check")
    if check is None:
        check = doc.get("record_check")
    # P7's ``confirmed``: the final read was readable and passed.
    confirmed = (
        isinstance(check, Mapping)
        and check.get("readable") is True
        and check.get("passed") is True
    )
    reason = fallback_reason(
        status,
        verified=doc.get("verified") is True,
        confirmed=confirmed,
        nothing_written=_nothing_written(check),
        something_written=_something_written(check),
        program_halted_before_effect=program.get("receipt_outcome")
        == "HALTED_BEFORE_EFFECT",
    )
    if reason == "readback_verified":
        out.readback_tiers.append("independent_readback")
    out.signal(reason)
    out.fields_check = "matched" if confirmed else "not_checked"
    # The names the record check read back in the task's new record
    # (``result.json`` only; the envelope doesn't carry them). ``collect``
    # keeps only the ones the run's config declares when it declares any.
    record = _dict(doc.get("record_check")) or _dict(run1.get("record_check"))
    names = record.get("fields")
    if isinstance(names, list) and record.get("created") == 1:
        for fname in [n for n in map(_field_name, names) if n]:
            out.fields[fname] = "matched" if confirmed else "not_checked"
        out.readback_fields = True
    return out


def _named_dirs(base: Path, name: str, *, depth: int = 5) -> list[Path]:
    """Folders called exactly ``name`` under ``base`` (no links, bounded)."""

    found: list[Path] = []
    stack = [(base, 0)]
    seen = 0
    while stack and seen < 5000:
        folder, level = stack.pop()
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            continue
        for entry in entries:
            seen += 1
            if entry.is_symlink() or not entry.is_dir():
                continue
            if entry.name == name:
                found.append(entry)
            if level < depth:
                stack.append((entry, level + 1))
    return sorted(found)


def record_times(
    contribution: Contribution, run: Path
) -> tuple[Optional[str], Optional[str], Optional[Path]]:
    """A dispatcher envelope's start and finish, from wrap's run store record
    ``runs/<run_id>.json`` near it (``openadapt-agent.run/v1`` with the same
    ``request_id``). Only ``started_at`` and ``finished_at`` are read; the
    envelope is already the record's result."""

    run_id = contribution.run_record_id
    base = run.parent if run.is_file() else run
    for folder in _named_dirs(base, "runs", depth=6):
        path = folder / f"{run_id}.json"
        if not path.is_file() or path.is_symlink():
            continue
        try:
            doc = _read_json(path)
        except _Unreadable:
            continue
        if (
            isinstance(doc, dict)
            and doc.get("schema") == "openadapt-agent.run/v1"
            and doc.get("run_id") == run_id
            and doc.get("request_id") in contribution.task_ids
        ):
            return _stamp(doc.get("started_at")), _stamp(doc.get("finished_at")), path
    return None, None, None


def fallback_neighbours(
    fallback: Contribution, run: Path, approvals: Optional[Path]
) -> tuple[Optional[Path], Optional[Contribution]]:
    """P7's files beside a fallback run, found by its own request id.

    * The item-approval folder: ``<approvals>/<id>/item-approval`` with
      ``--approvals``, else a folder named exactly ``<id>`` near the run
      (under the directory holding the result or envelope) that has an
      ``item-approval`` folder. Only the requests the run's ``gate_answers``
      name, with the same ``request_sha256``, are read.
    * For an envelope (no field names of its own): the fallback's own
      ``result.json`` (a folder named ``<id>`` holding one whose
      ``replay.fallback.request_id`` is ``<id>``), for the record check's
      field names, and the task's confirmed ``compiled/verify.yaml`` beside
      the cache (``<cache>/fallback/<task>/<id>/`` -> ``<cache>/<task>``).
    """

    key = fallback.approvals_key
    assert key is not None
    base = run.parent if run.is_file() else run
    approval_dir: Optional[Path] = None
    if approvals is not None:
        candidate = approvals / key / "item-approval"
        approval_dir = candidate if candidate.is_dir() else None
    named = _named_dirs(base, key)
    if approval_dir is None and fallback.gate_requests:
        for folder in named:
            if (folder / "item-approval").is_dir():
                approval_dir = folder / "item-approval"
                break
    extra: Optional[Contribution] = None
    result_dir = None
    if run.is_file() or (run / "result.json") not in fallback.files:
        for folder in named:
            result = folder / "result.json"
            if result in fallback.files or not result.is_file():
                continue
            try:
                doc = _read_json(result)
            except _Unreadable:
                continue
            replay = doc.get("replay") if isinstance(doc, dict) else None
            mine = (
                isinstance(replay, dict)
                and isinstance(replay.get("fallback"), dict)
                and replay["fallback"].get("request_id") == key
            )
            if mine and is_dispatch_result(doc):
                result_dir = folder
                record = doc.get("record_check")
                names = record.get("fields") if isinstance(record, dict) else None
                extra = Contribution("wrap_fallback_fields", files=[result])
                if isinstance(names, list) and record.get("created") == 1:
                    check = fallback.fields_check or "not_checked"
                    for fname in [n for n in map(_field_name, names) if n]:
                        extra.fields[fname] = check
                    extra.readback_fields = True
                break
    else:
        result_dir = run
    if result_dir is not None:
        task_dir = result_dir.parent
        verify = task_dir.parent.parent / task_dir.name / "compiled" / "verify.yaml"
        if verify.is_file() and not verify.is_symlink():
            extra = extra or Contribution("wrap_fallback_fields")
            extra.config_paths.append(verify)
    return approval_dir, extra


# -- P6 exceptions (J4) -------------------------------------------------------------


def collect_exception_folder(folder: Path) -> Optional[Contribution]:
    """``$OPENADAPT_HOME/exceptions/<run_ref>/`` (P6's registry).

    One interval per card, from the card's ``opened_at`` to the answer's
    ``decided_at``; a card with no answer yet, or an expired one
    (``decided_at: null``), is an open interval. The card's ``task_id`` is a
    task id source, so a card from another task makes the row
    ``task_id_conflict``. Card summaries and answers' signatures aren't read.
    """

    if not folder.is_dir() or folder.is_symlink():
        return None
    out = Contribution("exception")
    answered: set[str] = set()
    for answer_path in sorted(folder.glob("answer-*.json")):
        part = collect_exception_answer(answer_path)
        out.files.extend(part.files)
        out.signals.extend(part.signals)
        out.intervals.extend(part.intervals)
        answered.add(answer_path.name[len("answer-") : -len(".json")])
    for card_path in sorted(folder.glob("card-*.json")):
        out.files.append(card_path)
        try:
            card = _read_json(card_path)
        except _Unreadable:
            out.signal("source_unreadable")
            continue
        if not isinstance(card, dict) or card.get("schema") != EXCEPTION_CARD_SCHEMA:
            out.signal("schema_unrecognised")
            continue
        if (tid := _task_id(card.get("task_id"))) is not None:
            out.task_ids.append(tid)
        if card_path.name[len("card-") : -len(".json")] in answered:
            continue
        opened = _stamp(card.get("opened_at"))
        if opened is None:
            out.signal("source_unreadable")
            continue
        second = card.get("kind") == "second_factor"
        card_id = card.get("card_id")
        out.intervals.append(
            Interval(
                "second_factor" if second else "exception",
                opened,
                None,
                key=f"exception:{card_id}" if isinstance(card_id, str) else "",
            )
        )
        out.signal("second_factor" if second else "exception_card")
    return out if out.files else None


# -- declared field names (finding a) -------------------------------------------------


def _guard_task_fields(path: Path, task_ids: set[str]) -> set[str]:
    try:
        doc = _read_json(path)
    except _Unreadable:
        return set()
    if not isinstance(doc, dict) or doc.get("schema_version") != GUARD_TASK_SCHEMA:
        return set()
    if doc.get("task_id") not in task_ids:
        return set()  # another task's config declares nothing for this run
    names: set[str] = set()
    for key in ("expected_record", "intended_fields"):
        value = doc.get(key)
        if isinstance(value, dict):
            names.update(n for n in map(_field_name, value) if n)
    return names


def _verify_fields(path: Path) -> set[str]:
    try:
        import yaml

        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - an unreadable config declares nothing
        return set()
    if not isinstance(doc, dict) or doc.get("schema_version", VERIFY_SCHEMA) != (
        VERIFY_SCHEMA
    ):
        # A compile-agent verify.yaml has no schema_version; one that names
        # another version declares nothing.
        return set()
    names: set[str] = set()
    for effect in doc.get("effects") or []:
        match = effect.get("match") if isinstance(effect, dict) else None
        if isinstance(match, dict):
            names.update(n for n in map(_field_name, match) if n)
    return names


#: File names a run's own config goes by: the guard task (``task.json``,
#: readiness episodes' ``guard_task.json``) and a confirmed ``verify.yaml``.
#: A ``verify.proposed.yaml`` is a proposal, not a declaration.
_CONFIG_NAMES = ("task.json", "guard_task.json", "verify.yaml")


def declared_fields(files: Iterable[Path], task_ids: Iterable[str]) -> set[str]:
    """Field names declared by a guard task (``openadapt.guard-task/v1``:
    ``expected_record`` and ``intended_fields``) whose ``task_id`` is one of
    the run's, or a ``verify.yaml`` (``openadapt.verify/v1``: each effect's
    ``match``) among ``files``.
    Readiness reference fields come from ``case.json`` through
    :func:`collect_shadow_run`. Anything else declares nothing."""

    ids = set(task_ids)
    names: set[str] = set()
    for path in files:
        if path.suffix in (".yaml", ".yml"):
            names |= _verify_fields(path)
        elif path.suffix == ".json":
            names |= _guard_task_fields(path, ids)
    return names


def config_files(files: Iterable[Path]) -> list[Path]:
    """The run's own config files among the files found in it."""

    return [p for p in files if p.name in _CONFIG_NAMES]


__all__ = [
    "collect_exception_folder",
    "collect_fallback_result",
    "collect_shadow_run",
    "config_files",
    "declared_fields",
    "is_dispatch_result",
    "is_shadow_run",
]

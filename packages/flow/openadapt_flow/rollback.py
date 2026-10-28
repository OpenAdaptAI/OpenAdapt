"""Compensating rollback after a stop, verified by a whole-store diff.

A stop isn't a rollback: a run that saved at step 3 and stopped at step 8
leaves step 3's record behind. When a deployment turns rollback on
(``rollback:`` in ``deployment.yaml``), Flow reads the whole system of record
before the run, and after a stop:

1. reads it again (a fresh read) and compares. Nothing changed means nothing
   landed, proven by the read itself;
2. undoes each change in the run's ledger, latest first, with the
   compensation a person approved for that step: a declared undo, or an
   ``undo propose`` proposal accepted with ``undo accept`` after it passed on a
   test copy. A write whose delivery is uncertain, or whose effect wasn't
   confirmed, is compensated only when a fresh read shows its effect is there;
   absent means there is nothing to undo, and a read that can't tell stops the
   rollback;
3. reads the store a last time and compares it with the read from before the
   run. Equal is ``ROLLED_BACK``. Anything else is
   ``RECONCILIATION_REQUIRED`` with the exact diff: counts and column names in
   ``report.json``, the rows in ``<run-dir>/rollback/diff.json`` (owner-only).

Each compensation runs in a new session through the normal replayer, so the
identity checks, risk gate, the deployment's approval gate and the
compensation's own effect check all apply. Nothing runs that a person hasn't
approved: an unapproved proposal is never used. The same procedure runs on a
test copy in ``rollback qualify`` (``rollback_qualify.py``), so what is
qualified is what runs. See docs/ROLLBACK.md.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from openadapt_flow.after_stop import (
    RecordsReader,
    UndoRefused,
    judge_undo_report,
    read_prior_state,
    resolve_undo_params,
    undo_workflow,
)
from openadapt_flow.ir import (
    ChangeLedgerEntry,
    RollbackRecord,
    RollbackStepRecord,
    RunReport,
    UndoStep,
    Workflow,
)
from openadapt_flow.rollback_state import (
    StateDiff,
    StateReader,
    StateUnreadable,
    diff_states,
)
from openadapt_flow.runtime.effects.db_snapshot import Snapshot

ROLLBACK_DIRNAME = "rollback"
DIFF_FILENAME = "diff.json"


class CompensationRunner(Protocol):
    """Runs one compensation in a new session; returns its report."""

    def run(
        self,
        workflow: Workflow,
        *,
        bundle_dir: Path,
        run_dir: Path,
        params: dict[str, Any],
    ) -> Optional[RunReport]: ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def snapshot_sha256(snapshot: Snapshot) -> str:
    """A digest of a read, for evidence (never the rows themselves)."""
    payload = json.dumps(snapshot.to_json(), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def approved_undo(workflow: Workflow, step_id: str) -> Optional[UndoStep]:
    """The compensation a person approved for ``step_id``, if any."""
    return next(
        (
            u
            for u in workflow.undo_steps
            if u.undoes == step_id and not u.awaiting_approval
        ),
        None,
    )


def _write_private(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f".{path.name}.{os.getpid()}")
    fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
    os.replace(staging, path)


# ---------------------------------------------------------------------------
# Is the step's change there? (a fresh read, for an uncertain write)
# ---------------------------------------------------------------------------


def fresh_presence(
    entry: ChangeLedgerEntry,
    undo: UndoStep,
    workflow: Workflow,
    *,
    run_dir: Path,
    params: dict[str, Any],
    read_records: Optional[RecordsReader],
) -> tuple[str, str]:
    """``(present | absent | unknown, why)`` for one ledger entry, from a fresh
    read of the system of record.

    - An undo that finds its record (``locate``): the records matching the
      save that weren't there before it ran. One is present, none is absent,
      more is unknown.
    - An undo that restores old values (``restore``): the record holds the
      run's new value (present), its old value (absent), or something else
      (unknown).
    - Otherwise the step's own declared effects are judged against the read:
      every one confirmed is present, every one refuted with nothing observed
      is absent, anything else is unknown.
    """
    from openadapt_flow.after_stop import _matching, _resolved_selector
    from openadapt_flow.traversal import iter_workflow_steps

    records = read_records() if read_records is not None else None
    if records is None:
        return "unknown", "the system of record could not be read"
    prior = read_prior_state(run_dir, entry.step_id)
    if undo.locate is not None:
        existing = (prior or {}).get("existing_ids", {}).get(undo.id)
        selector = _resolved_selector(undo.locate.match, params)
        if not isinstance(existing, list) or selector is None:
            return "unknown", "the run kept no record list from before the step"
        new = [
            r
            for r in _matching(records, selector)
            if str(r.get(undo.locate.field)) not in set(existing)
        ]
        if len(new) == 1:
            return "present", "one new record matches the step"
        if not new:
            return "absent", "no new record matches the step"
        return "unknown", f"{len(new)} new records match the step"
    if undo.restore is not None:
        selector = _resolved_selector(undo.restore.match, params)
        rows = _matching(records, selector) if selector is not None else []
        values = (prior or {}).get("restore", {}).get(undo.id)
        if len(rows) != 1 or not isinstance(values, dict):
            return "unknown", "the step's record or its old values can't be found"
        step = next(
            (s for s in iter_workflow_steps(workflow) if s.id == entry.step_id), None
        )
        effects = list(step.effects) if step is not None else []
        new_values = {
            e.field: e.value.resolve(params)
            for e in effects
            if e.field is not None and e.value is not None
        }
        row = rows[0]
        fields = list(undo.restore.fields)
        if all(str(row.get(f)) == str(values.get(f)) for f in fields):
            return "absent", "the record still holds its old values"
        if fields and all(
            f in new_values and str(row.get(f)) == str(new_values[f]) for f in fields
        ):
            return "present", "the record holds the run's new values"
        return "unknown", "the record holds neither the old nor the new values"
    return _judge_declared(entry, workflow, records, params)


def _judge_declared(
    entry: ChangeLedgerEntry,
    workflow: Workflow,
    records: list[dict[str, Any]],
    params: dict[str, Any],
) -> tuple[str, str]:
    from openadapt_flow.runtime.effects._common import judge_records
    from openadapt_flow.runtime.effects.effect import EffectState, Verdict
    from openadapt_flow.traversal import iter_workflow_steps

    step = next(
        (s for s in iter_workflow_steps(workflow) if s.id == entry.step_id), None
    )
    effects = list(step.effects) if step is not None else []
    if not effects:
        return "unknown", "the step declares no effect to check"
    verdicts = []
    for effect in effects:
        resolved = effect.resolve(params)
        if resolved.requires_baseline:
            return "unknown", "the step's check needs a read from before it ran"
        baseline = EffectState(substrate="rollback", reachable=True, records=[])
        verdicts.append(
            judge_records(resolved, baseline, records, substrate="rollback")
        )
    if all(v.verdict is Verdict.CONFIRMED for v in verdicts):
        return "present", "every declared effect is in the system of record"
    if all(v.verdict is Verdict.REFUTED and v.observed_count == 0 for v in verdicts):
        return "absent", "no declared effect is in the system of record"
    return "unknown", "the declared effects are partly there"


# ---------------------------------------------------------------------------
# The rollback
# ---------------------------------------------------------------------------


def roll_back(
    report: RunReport,
    workflow: Workflow,
    *,
    run_dir: Path,
    bundle_dir: Path,
    reader: StateReader,
    before: Optional[Snapshot],
    runner: CompensationRunner,
    read_records: Optional[RecordsReader] = None,
    ignore_columns: Optional[dict[str, list[str]]] = None,
) -> Optional[RollbackRecord]:
    """Undo what a stopped run left, then prove the store is as it was.

    ``before`` is the read taken before the run (None when it failed: the
    record says the rollback was unarmed and nothing runs). ``read_records``
    reads the collection the compensations' own checks use; it settles
    uncertain writes and finds created records. Returns None for a run that
    finished. Never raises for a failed read or compensation; those end as
    ``not_rolled_back`` with the reason.
    """
    if report.success:
        return None
    run_dir = Path(run_dir)
    allow = list(getattr(reader, "allow_tables", ()) or ())
    scope = "database" if getattr(reader, "scope", "") == "database" else "collection"
    record = RollbackRecord(
        status="not_rolled_back",
        reason="",
        scope=scope,  # type: ignore[arg-type]
        allow_tables=allow,
        ignored_columns={k: list(v) for k, v in (ignore_columns or {}).items()},
        started_at=_now(),
    )

    def finish(
        status: str, reason: str, diff: Optional[StateDiff] = None
    ) -> RollbackRecord:
        record.status = status  # type: ignore[assignment]
        record.reason = reason
        if diff is not None and not diff.empty:
            record.remaining_diff = diff.summary()
            path = run_dir / ROLLBACK_DIRNAME / DIFF_FILENAME
            _write_private(path, diff.to_private_json())
            record.diff_path = str(path.relative_to(run_dir))
        record.finished_at = _now()
        return record

    if before is None:
        return finish(
            "unarmed",
            "the system of record couldn't be read before the run, so nothing "
            "is compared and no compensation runs",
        )
    record.before_sha256 = snapshot_sha256(before)
    try:
        after_stop = reader.read()
    except StateUnreadable as exc:
        return finish("not_rolled_back", f"after the stop, {exc}; nothing ran")
    record.after_stop_sha256 = snapshot_sha256(after_stop)
    landed = diff_states(before, after_stop, ignore_columns=ignore_columns)
    record.after_stop_diff = landed.summary()
    if landed.empty:
        record.final_sha256 = record.after_stop_sha256
        return finish(
            "nothing_landed",
            "after the stop the system of record matches the read from before "
            "the run; nothing to undo",
        )

    from openadapt_flow.after_stop import _has_loop

    entries = sorted(
        (e for e in report.change_ledger if e.may_need_undo),
        key=lambda e: -e.position,
    )
    counts: dict[str, int] = {}
    for entry in report.change_ledger:
        counts[entry.step_id] = counts.get(entry.step_id, 0) + 1
    stop_reason: Optional[str] = None
    if report.loop_iterations or _has_loop(workflow):
        stop_reason = (
            "the workflow has a loop: a compensation runs with the run's base "
            "parameters, not one iteration's"
        )
    params = dict(report.params)
    for entry in entries if stop_reason is None else []:
        step_record = RollbackStepRecord(
            step_id=entry.step_id,
            position=entry.position,
            decision="refused",
            reason="",
        )
        record.steps.append(step_record)
        if counts[entry.step_id] > 1:
            step_record.reason = "the step ran more than once"
            stop_reason = step_record.reason
            break
        undo = approved_undo(workflow, entry.step_id)
        if undo is None:
            step_record.reason = (
                "no compensation a person approved for this step (declare one, "
                "or accept a verified proposal)"
            )
            stop_reason = f"step {entry.step_id}: {step_record.reason}"
            break
        step_record.undo_id = undo.id
        if entry.attempt_state == "delivery_uncertain" or entry.status != "confirmed":
            presence, why = fresh_presence(
                entry,
                undo,
                workflow,
                run_dir=run_dir,
                params=params,
                read_records=read_records,
            )
            step_record.fresh_read = presence  # type: ignore[assignment]
            if presence == "absent":
                step_record.decision = "not_needed"
                step_record.reason = f"a fresh read shows nothing to undo: {why}"
                continue
            if presence != "present":
                step_record.reason = (
                    "the write's delivery or effect is uncertain and a fresh "
                    f"read couldn't settle it ({why}); not compensated on a guess"
                )
                stop_reason = f"step {entry.step_id}: {step_record.reason}"
                break
        try:
            undo_params = resolve_undo_params(undo, run_dir, params, read_records)
        except UndoRefused as exc:
            step_record.reason = f"the compensation couldn't be prepared: {exc}"
            stop_reason = f"step {entry.step_id}: {step_record.reason}"
            break
        sub_dir = run_dir / ROLLBACK_DIRNAME / undo.id
        try:
            undo_report = runner.run(
                undo_workflow(workflow, undo),
                bundle_dir=bundle_dir,
                run_dir=sub_dir,
                params=undo_params,
            )
        except CompensationRefused as exc:
            step_record.reason = f"the compensation can't run here: {exc}"
            stop_reason = f"step {entry.step_id}: {step_record.reason}"
            break
        except Exception as exc:  # noqa: BLE001 - recorded, never hidden
            undo_report = None
            step_record.reason = f"the compensation crashed ({type(exc).__name__})"
        confirmed, judged = judge_undo_report(undo, undo_report)
        step_record.undo_outcome = (
            undo_report.transaction_outcome if undo_report is not None else None
        )
        if not confirmed:
            step_record.decision = "failed"
            step_record.reason = step_record.reason or judged
            stop_reason = f"step {entry.step_id}: compensation not confirmed ({judged})"
            break
        step_record.decision = "compensated"
        step_record.reason = judged

    try:
        final = reader.read()
    except StateUnreadable as exc:
        return finish(
            "not_rolled_back",
            f"the final read failed ({exc}); what was undone can't be proven",
            landed,
        )
    record.final_sha256 = snapshot_sha256(final)
    remaining = diff_states(before, final, ignore_columns=ignore_columns)
    if remaining.empty:
        return finish(
            "rolled_back",
            "every change was compensated and the system of record matches the "
            "read from before the run"
            + (f" (a step was left: {stop_reason})" if stop_reason else ""),
        )
    reason = (
        stop_reason
        or "the compensations ran but the system of record doesn't match the "
        "read from before the run"
    )
    return finish(
        "not_rolled_back",
        f"{reason}. Still different: {remaining.describe()}",
        remaining,
    )


def apply_rollback(
    report: RunReport,
    workflow: Workflow,
    record: Optional[RollbackRecord],
    run_dir: Path,
) -> RunReport:
    """Attach ``record`` and re-derive the transaction outcome; save the report."""
    from openadapt_flow.transaction import stamp_transaction_outcome

    if record is None:
        return report
    report.rollback = record
    stamp_transaction_outcome(report, workflow)
    report.save(run_dir)
    return report


def rollback_lines(report: RunReport) -> list[str]:
    """Plain lines for the halt message and ``explain``."""
    record = report.rollback
    if record is None:
        return []
    head = {
        "rolled_back": "Rolled back: the system of record matches the read from before the run.",
        "nothing_landed": "Nothing landed: the system of record matches the read from before the run.",
        "not_rolled_back": "Not rolled back. A person must repair the record.",
        "unarmed": "Rollback wasn't armed: the system of record couldn't be read before the run.",
    }[record.status]
    lines = [head, f"  {record.reason}"]
    for step in record.steps:
        lines.append(
            f"  step `{step.step_id}`: {step.decision}"
            + (f" with `{step.undo_id}`" if step.undo_id else "")
            + f" ({step.reason})"
        )
    if record.diff_path:
        lines.append(f"  The full difference is in {record.diff_path} (owner-only).")
    return lines


class CompensationRefused(RuntimeError):
    """A compensation can't run here. Nothing was executed."""


def is_api_only(workflow: Workflow) -> bool:
    """Every step acts through an API binding that never falls back to the GUI."""
    return bool(workflow.steps) and all(
        step.api_binding is not None and step.api_binding.on_unavailable == "halt"
        for step in workflow.steps
    )


class ReplayerRunner:
    """Runs each compensation through a newly configured replayer.

    ``make_replayer`` builds a replayer on the run's backend with the run's
    effect verifier, API actuator and approval gate. ``fresh_session``
    returns the backend to a signed-out start (a web page with its cookies
    and storage cleared, back at the entry URL). Without it, only an
    API-only compensation runs: a GUI compensation recorded from a fresh
    session would act on whatever screen the stop left.
    """

    def __init__(
        self,
        make_replayer: Callable[[], Any],
        fresh_session: Optional[Callable[[], None]] = None,
    ) -> None:
        self.make_replayer = make_replayer
        self.fresh_session = fresh_session

    def run(
        self,
        workflow: Workflow,
        *,
        bundle_dir: Path,
        run_dir: Path,
        params: dict[str, Any],
    ) -> Optional[RunReport]:
        if not is_api_only(workflow):
            if self.fresh_session is None:
                raise CompensationRefused(
                    "this backend can't start a fresh session, so only an "
                    "API-only compensation runs here"
                )
            self.fresh_session()
        replayer = self.make_replayer()
        report: RunReport = replayer.run(
            workflow, params=params, bundle_dir=bundle_dir, run_dir=run_dir
        )
        return report


def web_fresh_session(
    page: Any, entry_url: Optional[str]
) -> Optional[Callable[[], None]]:
    """A browser page back to a signed-out start at ``entry_url``."""
    if page is None or not entry_url:
        return None

    def reset() -> None:
        page.context.clear_cookies()
        try:
            page.evaluate("() => { localStorage.clear(); sessionStorage.clear(); }")
        except Exception:  # noqa: BLE001 - a page with no storage access
            pass
        page.goto(entry_url)

    return reset


class RollbackPlan:
    """What a run needs to roll back after a stop: how to read the store,
    how to read the compensations' own collection, and columns to ignore."""

    def __init__(
        self,
        reader: StateReader,
        *,
        read_records: Optional[RecordsReader] = None,
        ignore_columns: Optional[dict[str, list[str]]] = None,
    ) -> None:
        self.reader = reader
        self.read_records = read_records
        self.ignore_columns = dict(ignore_columns or {})

    def arm(self) -> Optional[Snapshot]:
        """The read from before the run, or None when the store can't be read."""
        try:
            return self.reader.read()
        except StateUnreadable:
            return None

    def finish(
        self,
        report: RunReport,
        workflow: Workflow,
        *,
        before: Optional[Snapshot],
        run_dir: Path,
        bundle_dir: Path,
        runner: CompensationRunner,
    ) -> RunReport:
        record = roll_back(
            report,
            workflow,
            run_dir=run_dir,
            bundle_dir=bundle_dir,
            reader=self.reader,
            before=before,
            runner=runner,
            read_records=self.read_records,
            ignore_columns=self.ignore_columns,
        )
        return apply_rollback(report, workflow, record, run_dir)


def plan_from_deployment(cfg: Any, effect_verifier: Any) -> Optional[RollbackPlan]:
    """The deployment's rollback plan, or None when ``rollback`` is off."""
    from openadapt_flow.after_stop import records_reader
    from openadapt_flow.rollback_state import DatabaseReader, VerifierReader

    section = getattr(cfg, "rollback", None)
    if section is None or not section.enabled:
        return None
    reader: StateReader
    if section.database is not None:
        reader = DatabaseReader.from_frame_database(section.database)
    elif effect_verifier is not None:
        reader = VerifierReader(effect_verifier)
    else:
        raise ValueError(
            "rollback needs rollback.database or an effects verifier to read "
            "the system of record"
        )
    return RollbackPlan(
        reader,
        read_records=records_reader(effect_verifier),
        ignore_columns={k: list(v) for k, v in section.ignore_columns.items()},
    )


__all__ = [
    "CompensationRefused",
    "CompensationRunner",
    "ReplayerRunner",
    "RollbackPlan",
    "is_api_only",
    "plan_from_deployment",
    "web_fresh_session",
    "apply_rollback",
    "approved_undo",
    "fresh_presence",
    "roll_back",
    "rollback_lines",
    "snapshot_sha256",
]

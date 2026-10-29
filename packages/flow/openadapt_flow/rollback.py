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
    Attributor,
    Change,
    StateDiff,
    StateReader,
    StateUnreadable,
    changes,
    diff_states,
    keep_rows,
)
from openadapt_flow.runtime.effects.db_snapshot import Snapshot

ROLLBACK_DIRNAME = "rollback"
#: Written before the first compensation runs: from then on the run's store
#: may have changed, and the run is never resumed (runtime.durable.resume).
COMPENSATED_MARKER = f"{ROLLBACK_DIRNAME}/compensated"
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


def _private_dir(path: Path) -> None:
    """An owner-only directory, never through a symlink."""
    if path.is_symlink():
        raise OSError(f"{path} is a symlink; nothing is written through it")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _write_private(path: Path, payload: dict[str, Any]) -> None:
    _private_dir(path.parent)
    staging = path.with_name(f".{path.name}.{os.getpid()}")
    # A leftover staging file would keep its own mode: start from nothing.
    staging.unlink(missing_ok=True)
    fd = os.open(
        staging,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
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


KIND_ORDER = ("discard_draft", "restore", "void", "delete")


def undoable(workflow: Workflow, step_id: str, *, allow_delete: bool) -> bool:
    """Has this save an approved compensation the deployment lets run?"""
    undo = approved_undo(workflow, step_id)
    if undo is None:
        return False
    return allow_delete or (undo.kind or "delete") != "delete"


def saves_without_undo(workflow: Workflow, *, allow_delete: bool) -> list[str]:
    """The consequential steps a rollback couldn't undo: each needs a
    person's approval before it runs."""
    from openadapt_flow.run_gate import is_consequential
    from openadapt_flow.traversal import iter_workflow_steps

    return [
        step.id
        for step in iter_workflow_steps(workflow)
        if is_consequential(step, workflow, require_current_risk_certification=False)
        and not undoable(workflow, step.id, allow_delete=allow_delete)
    ]


def _certain(entry: ChangeLedgerEntry) -> bool:
    return entry.status == "confirmed" and entry.attempt_state != "delivery_uncertain"


def _unseen_writes(
    pending: list[ChangeLedgerEntry],
    workflow: Workflow,
    *,
    run_dir: Path,
    params: dict[str, Any],
    read_records: Optional[RecordsReader],
) -> Optional[str]:
    """When the read shows nothing landed: a ledger write it contradicts. A
    confirmed write did land; an uncertain one counts as absent only when a
    fresh read of its own record says so."""
    for entry in pending:
        if _certain(entry):
            return f"step {entry.step_id} confirmed a write"
        undo = approved_undo(workflow, entry.step_id)
        if undo is None:
            return f"step {entry.step_id} may have written (no compensation to read it back)"
        presence, why = fresh_presence(
            entry,
            undo,
            workflow,
            run_dir=run_dir,
            params=params,
            read_records=read_records,
        )
        if presence != "absent":
            return f"step {entry.step_id} may have written ({why})"
    return None


def _unsettled(
    pending: list[ChangeLedgerEntry], steps: list[RollbackStepRecord]
) -> Optional[str]:
    """A ledger write no compensation settled (compensated, or shown absent)."""
    done = {s.step_id for s in steps if s.decision in ("compensated", "not_needed")}
    left = [e.step_id for e in pending if e.step_id not in done]
    if not left:
        return None
    return "step(s) " + ", ".join(left) + " weren't compensated"


def _excuser(reader: Any, after: Any) -> Any:
    """The reader's app-profile row rules for this read, if it has any."""
    make = getattr(reader, "excuser", None)
    return make(after) if make is not None else None


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
    assume_stopped: bool = False,
    attributor: Optional[Attributor] = None,
    allow_delete: bool = False,
    exclusive: bool = False,
) -> Optional[RollbackRecord]:
    """Undo what a stopped run left, then prove the run's changes are gone.

    ``before`` is the read taken before the run (None when it failed: the
    record says the rollback was unarmed and nothing runs). ``read_records``
    reads the collection the compensations' own checks use; it settles
    uncertain writes and finds created records.

    A compensation runs only while every change in the store since ``before``
    is positively the run's own (``Attributor.is_own``): checked at the stop,
    before and after each compensation, and in the audit window at the end.
    Anything else (another user's write, or a row attribution can't vouch
    for) goes to a person with the exact difference; nothing of theirs is
    ever compensated away. Without an attributor nothing is compensated,
    unless the store is ``exclusive`` to the run (a test copy). A ``delete``
    compensation (or one that declares no kind) runs only with
    ``allow_delete``.

    The change ledger and the reads must agree: a confirmed write the read
    doesn't show, or a write left uncompensated, is never called rolled back
    or nothing landed.

    Returns None for a run that finished. Never raises for a failed read or
    compensation; those end as ``not_rolled_back`` with the reason.
    ``assume_stopped`` treats a finished run as stopped there (``rollback
    qualify`` stops runs on purpose).
    """
    if report.success and not assume_stopped:
        return None
    run_dir = Path(run_dir)
    allow = list(getattr(reader, "allow_tables", ()) or ())
    scope = "database" if getattr(reader, "scope", "") == "database" else "collection"
    record = RollbackRecord(
        status="not_rolled_back",
        reason="",
        scope=scope,  # type: ignore[arg-type]
        allow_tables=allow,
        profile_id=getattr(reader, "profile_id", None),
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

    try:
        # The compensations' own run directories go here too.
        _private_dir(run_dir / ROLLBACK_DIRNAME)
    except OSError as exc:
        return finish("not_rolled_back", f"no private place for its records ({exc})")
    if before is None:
        return finish(
            "unarmed",
            "the system of record couldn't be read before the run, so nothing "
            "is compared and no compensation runs",
        )
    record.before_sha256 = snapshot_sha256(before)
    try:
        after_stop = reader.read(before=before)
    except StateUnreadable as exc:
        return finish("not_rolled_back", f"after the stop, {exc}; nothing ran")
    record.after_stop_sha256 = snapshot_sha256(after_stop)
    landed = diff_states(before, after_stop, excuse=_excuser(reader, after_stop))
    params = dict(report.params)
    pending = [e for e in report.change_ledger if e.may_need_undo]
    run_changes = changes(landed)
    record.after_stop_diff = landed.summary()
    if not run_changes:
        unseen = _unseen_writes(
            pending, workflow, run_dir=run_dir, params=params, read_records=read_records
        )
        if unseen:
            return finish(
                "not_rolled_back",
                "the read shows nothing the run did, but the change ledger "
                f"says {unseen}; the read can't see that write (an allow-listed "
                "table, a column the app profile leaves out, or a read of one "
                "collection), so a person checks",
            )
        record.final_sha256 = record.after_stop_sha256
        return finish(
            "nothing_landed",
            "after the stop nothing the run did is in the system of record",
        )
    if attributor is None and not exclusive:
        return finish(
            "not_rolled_back",
            "rollback.attribution isn't set (or the run's own user couldn't be "
            "resolved), so another user's edit to these rows can't be told from "
            "the run's; nothing was compensated. Different now: " + landed.describe(),
            landed,
        )

    def not_own(diff: StateDiff) -> list[Change]:
        """Rows that changed since the first read and aren't positively the
        run's own: another user's, or ones attribution can't vouch for."""
        if exclusive:
            return []
        assert attributor is not None
        window = attributor.window()
        moved = {(t.table, k): c for t in diff.tables for k, _b, _a, c in t.changed}
        return [
            c
            for c in changes(diff)
            if not attributor.is_own(c, window, moved.get(c[:2], ()))
        ]

    # Compensate only while the store holds nothing but the run's own changes:
    # anything else, a person sees first.
    foreign = not_own(landed)
    if foreign:
        record.conflicts = len(foreign)
        return finish(
            "not_rolled_back",
            f"{len(foreign)} row(s) changed since the run started aren't "
            "positively the run's own (another user's, or attribution can't "
            "show whose); nothing was compensated, so no one's change is lost. "
            "A person decides. Different now: " + landed.describe(),
            landed,
        )
    run_keys = {c[:2] for c in run_changes}

    from openadapt_flow.after_stop import _has_loop

    entries = sorted(pending, key=lambda e: -e.position)
    counts: dict[str, int] = {}
    for entry in report.change_ledger:
        counts[entry.step_id] = counts.get(entry.step_id, 0) + 1
    stop_reason: Optional[str] = None
    if report.loop_iterations or _has_loop(workflow):
        stop_reason = (
            "the workflow has a loop: a compensation runs with the run's base "
            "parameters, not one iteration's"
        )
    #: ``(table, field, value)``: a voided record a ``void`` compensation keeps.
    retained: set[tuple[str, str, str]] = set()
    #: Each run row as the stop left it: a kept void row must have changed.
    at_stop = {c[:2]: c[3] for c in run_changes}
    #: The latest read: after the stop, then after each compensation.
    last = after_stop

    def drifted(now: Snapshot, since: Snapshot) -> Optional[str]:
        """Why the store can't take a compensation now: a run row moved since
        the last read, a row the run didn't make changed, or a change since
        the first read isn't positively the run's own."""
        since_last = changes(diff_states(since, now, excuse=_excuser(reader, now)))
        moved = [c for c in since_last if c[:2] in run_keys]
        if moved:
            record.conflicts = len(moved)
            return (
                f"{len(moved)} row(s) the run wrote changed since the last read; "
                "not compensated, so that change isn't lost"
            )
        others = not_own(diff_states(before, now, excuse=_excuser(reader, now)))
        if others:
            record.conflicts = len(others)
            return (
                f"{len(others)} row(s) changed that aren't positively the run's "
                "own (another user's, or attribution can't show whose)"
            )
        return None

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
        kind = undo.kind or "delete"
        if kind == "delete" and not allow_delete:
            step_record.reason = (
                "its compensation hard-deletes a record"
                + ("" if undo.kind else " (it declares no kind)")
                + " and this deployment doesn't allow deletes (rollback.allow_delete); "
                "use a void, restore or discard compensation"
            )
            stop_reason = f"step {entry.step_id}: {step_record.reason}"
            break
        # Right before each compensation, the whole store again.
        try:
            now = reader.read(before=before)
        except StateUnreadable as exc:
            step_record.reason = f"the read before compensating failed ({exc})"
            stop_reason = step_record.reason
            break
        why_not = drifted(now, last)
        if why_not is not None:
            step_record.reason = why_not
            stop_reason = why_not
            break
        try:
            undo_params = resolve_undo_params(undo, run_dir, params, read_records)
        except UndoRefused as exc:
            step_record.reason = f"the compensation couldn't be prepared: {exc}"
            stop_reason = f"step {entry.step_id}: {step_record.reason}"
            break
        sub_dir = run_dir / ROLLBACK_DIRNAME / undo.id
        # From here the store may change: the run can't be resumed.
        (run_dir / COMPENSATED_MARKER).touch()
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
        if kind == "void" and undo.locate is not None:
            located = str(undo_params.get(undo.locate.param, ""))
            retained |= {(table, undo.locate.field, located) for table in undo.retains}
        # What the compensation did: only the run's own rows may have moved,
        # and the store may still hold nothing but the run's own changes.
        try:
            after = reader.read(before=before)
        except StateUnreadable as exc:
            stop_reason = (
                f"the read after compensating step {entry.step_id} failed ({exc})"
            )
            break
        touched = [
            c
            for c in changes(diff_states(now, after, excuse=_excuser(reader, after)))
            if c[:2] not in run_keys
        ]
        if touched:
            record.conflicts = len(touched)
            stop_reason = (
                f"the compensation for step {entry.step_id} also changed "
                f"{len(touched)} row(s) the run didn't write, or another user "
                "wrote while it ran"
            )
            break
        last = after

    try:
        final = reader.read(before=before)
    except StateUnreadable as exc:
        return finish(
            "not_rolled_back",
            f"the final read failed ({exc}); what was undone can't be proven",
            landed,
        )
    record.final_sha256 = snapshot_sha256(final)
    remaining = diff_states(before, final, excuse=_excuser(reader, final))
    if (
        attributor is not None
        and attributor.spec.kind == "audit_read"
        and not exclusive
    ):
        final_window = attributor.window()
        if final_window is None:
            return finish(
                "not_rolled_back",
                "the audit log couldn't be read after the rollback, so another "
                "user's write can't be ruled out; a person checks the record",
                landed,
            )
        # Another user's write to a run row while the compensations ran: it
        # may be gone with the row now. The diff shows the run's rows.
        late = [
            c
            for c in run_changes
            if attributor.others_touched(c, final_window)
            or attributor.others_on_record(c, final_window)
        ]
        if late:
            record.conflicts = len(late)
            return finish(
                "not_rolled_back",
                f"another user wrote {len(late)} row(s) the run touched (or their "
                "record) while the rollback ran; a person checks that their "
                "change isn't lost",
                landed,
            )
    residue: list[Change] = []
    kept = 0
    #: Void rows kept on purpose, per table and kind (added, removed, changed).
    explained: dict[str, list[int]] = {}
    for change in changes(remaining):
        table, key, old, new = change
        stop = at_stop.get((table, key))
        if (
            new is not None
            and stop is not None
            and stop != new
            and any(table == t and str(new.get(f)) == v for t, f, v in retained)
        ):
            # The run's record, kept and changed by its void compensation.
            kept += 1
            counts_ = explained.setdefault(table, [0, 0, 0])
            counts_[0 if old is None else 1 if new is None else 2] += 1
            continue
        residue.append(change)
    record.retained_rows = kept
    left = keep_rows(remaining, residue)
    unsettled = _unsettled(pending, record.steps)
    if not residue and unsettled:
        return finish(
            "not_rolled_back",
            "the read shows nothing left, but "
            + unsettled
            + (f" ({stop_reason})" if stop_reason else "")
            + "; the read may not see that write, so a person checks",
        )
    if not residue:
        # The verifier's own whole-database diff must agree: it may see no
        # more rows moved in a table than a void kept there. It decides when
        # the two differ.
        cross = getattr(reader, "verifier_diff", None)
        try:
            verdict = cross(before, final) if callable(cross) else None
        except Exception as exc:  # noqa: BLE001 - a failed check proves nothing
            record.cross_checked = False
            return finish(
                "not_rolled_back",
                f"the verifier's database diff failed ({type(exc).__name__}); "
                "what was undone can't be proven",
                remaining,
            )
        record.cross_checked = verdict is not None
        unexplained = sorted(
            t
            for t, d in (verdict or {}).items()
            if getattr(d, "unread", None)
            or any(
                n > m
                for n, m in zip(
                    (
                        getattr(d, "added", 1),
                        getattr(d, "removed", 1),
                        getattr(d, "changed", 1),
                    ),
                    explained.get(t, [0, 0, 0]),
                )
            )
        )
        if unexplained:
            return finish(
                "not_rolled_back",
                "the verifier's database diff still sees changes in "
                + ", ".join(unexplained)
                + "; a person checks the record",
                remaining,
            )
        return finish(
            "rolled_back",
            "every change the run made was compensated and the system of record "
            "matches the read from before the run"
            + (
                f", apart from {kept} voided record row(s) kept as the app requires"
                if kept
                else ""
            )
            + (
                "; a last-writer field can't show an edit another user made "
                "while a compensation ran"
                if attributor is not None
                and attributor.spec.kind == "record_field"
                and not exclusive
                else ""
            ),
        )
    reason = (
        stop_reason
        or "the compensations ran but the system of record doesn't match the "
        "read from before the run"
    )
    return finish(
        "not_rolled_back",
        f"{reason}. Still different: {left.describe()}",
        left,
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
    how to read the compensations' own collection, who changed a row, and
    whether a hard delete may run."""

    def __init__(
        self,
        reader: StateReader,
        *,
        read_records: Optional[RecordsReader] = None,
        attribution: Any = None,
        audit_read: Optional[RecordsReader] = None,
        allow_delete: bool = False,
        exclusive: bool = False,
    ) -> None:
        #: The store is a test copy only this run writes (``rollback qualify``,
        #: or a deployment that declares both a test copy and
        #: ``rollback.exclusive``).
        self.exclusive = exclusive
        if attribution is not None and attribution.kind == "audit_read":
            missing = [
                name
                for name, ok in (
                    ("record_key", attribution.record_key or attribution.record_keys),
                    ("event_field", attribution.event_field),
                    ("write_events", attribution.write_events),
                    ("insert_events", attribution.insert_events),
                    ("table_field", attribution.table_field),
                )
                if not ok
            ]
            if missing:
                # Without them an audit entry names no row, or a view or
                # another table's write excuses a change: attribution would
                # be silently off.
                raise ValueError(
                    "rollback with audit_read attribution needs "
                    + ", ".join(missing)
                    + " (docs/ROLLBACK.md, Other users)"
                )
        self.reader = reader
        self.read_records = read_records
        self.attribution = attribution
        self.audit_read = audit_read
        self.allow_delete = allow_delete
        self.attributor: Optional[Attributor] = None

    def arm(self, params: Optional[dict[str, Any]] = None) -> Optional[Snapshot]:
        """The read from before the run, or None when the store (or, with
        audit attribution, its audit log) can't be read.

        With attribution, the audit log is read here too; attribution whose
        own actors can't be resolved from ``params`` also returns None.
        """
        if self.attribution is not None:
            self.attributor = Attributor.from_spec(
                self.attribution, params or {}, self.audit_read
            )
            if self.attributor is None:
                # The run's own user can't be resolved: nothing could ever be
                # shown to be the run's, and undoable saves would run without
                # a person.
                return None
            if not self.attributor.start():
                # An unreadable audit log would attribute nothing: as unsafe
                # as an unreadable store.
                return None
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
            attributor=self.attributor,
            allow_delete=self.allow_delete,
            exclusive=self.exclusive,
        )
        return apply_rollback(report, workflow, record, run_dir)


def plan_from_deployment(cfg: Any, effect_verifier: Any) -> Optional[RollbackPlan]:
    """The deployment's rollback plan, or None when ``rollback`` is off."""
    from openadapt_flow.after_stop import records_reader
    from openadapt_flow.compiler.probing import probe_target_refusals
    from openadapt_flow.rollback_state import DatabaseReader, VerifierReader

    section = getattr(cfg, "rollback", None)
    if section is None or not section.enabled:
        return None
    reader: StateReader
    if section.database is not None:
        reader = DatabaseReader.from_frame_database(
            section.database,
            profile=section.profile,
            actor_names=section.actor_names,
        )
    elif effect_verifier is not None:
        reader = VerifierReader(effect_verifier)
    else:
        raise ValueError(
            "rollback needs rollback.database or an effects verifier to read "
            "the system of record"
        )
    audit_read = None
    if section.attribution is not None and section.attribution.read is not None:
        from openadapt_flow.deployment import EffectsConfig, build_effect_verifier

        audit_read = records_reader(
            build_effect_verifier(
                EffectsConfig.model_validate(section.attribution.read), {}
            )
        )
    return RollbackPlan(
        reader,
        read_records=records_reader(effect_verifier),
        attribution=section.attribution,
        audit_read=audit_read,
        allow_delete=section.allow_delete,
        # Only by declaration: a test copy (the gate rollback qualify uses)
        # that no one else writes, which a reset alone doesn't promise.
        exclusive=bool(section.exclusive) and not probe_target_refusals(cfg),
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

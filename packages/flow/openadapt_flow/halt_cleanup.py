"""Leave the app's screen clean after a halted item (docs/HALT_CLEANUP.md).

A halted item can leave a dialog open, a form half filled, or another page in
front. When the next item runs on the same session (a worklist pool worker),
it would start on that screen. This module gives a workflow a
:class:`~openadapt_flow.ir.HaltCleanup`:

- **Derive** (:func:`derive_cleanup`). The start state is the first anchored
  step's anchor: the screen every item starts on. The steps are Escape (close
  a dialog), then each cancel, close, discard or dismiss control the
  recordings clicked, guarded to run only when that control is on screen.
- **Qualify** (:func:`qualify_cleanup`, ``openadapt-flow cleanup qualify``)
  on a declared test copy, reset before each halt point. For each halt point
  (after every step but the last), one fresh session runs the steps up to
  the halt point, then the cleanup, then the start-state check. The cleanup is qualified only if every halt point
  reaches the start screen.
  Each halt point also runs once without the cleanup, and the test copy's
  records must be the same both ways: a "Close" that saves (autosave on
  leave) fails qualification.
- **Run** (:func:`run_cleanup`). After an item that halted before any write,
  when the session will take another item, the pool worker reads the
  records, runs the cleanup, returns to the entry, checks the start state and
  reads the records again. If the check fails, the session is dirty and the
  worker takes no more items. If the records changed (or can't be read), the
  item's write is uncertain: it is never reported as unwritten, and the run
  stops.

Navigation only. A record the halted item saved is the compensating
rollback's job (docs/ROLLBACK.md), not this one's.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from openadapt_flow.ir import (
    ActionKind,
    Guard,
    HaltCleanup,
    HaltCleanupQualification,
    Predicate,
    PredicateKind,
    RunReport,
    Step,
    Workflow,
)

#: Labels of controls that leave a dialog or form without saving. Not
#: "back" or "exit": those leave a page, and an app that saves on leave
#: (autosave) commits the form. "Close" can too; qualification and the
#: run-time read-back catch that.
CANCEL_WORDS = re.compile(
    r"\b(cancel|close|discard|dismiss|don'?t save|abandon)\b|^\s*[x×✕]\s*$",
    re.I,
)
#: Reads every record the system of record shows; None when unreadable.
RecordsRead = Callable[[], Optional[list[dict[str, Any]]]]
CHECK_STEP_ID = "cleanup.start_state"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def cleanup_sha256(cleanup: HaltCleanup) -> str:
    payload = cleanup.model_copy(update={"qualification": None}).model_dump(mode="json")
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


# ---------------------------------------------------------------------------
# Derive
# ---------------------------------------------------------------------------


def start_state_of(workflow: Workflow) -> Optional[Predicate]:
    """The screen every item starts on: the first anchored step's anchor."""
    from openadapt_flow.traversal import iter_workflow_steps

    first = next(
        (s for s in iter_workflow_steps(workflow) if s.anchor is not None), None
    )
    if first is None or first.anchor is None:
        return None
    return Predicate(
        kind=PredicateKind.ANCHOR_RESOLVES,
        anchor=first.anchor.model_copy(deep=True),
        intent=f"the start screen ({first.intent})",
    )


def _label(step: Step) -> str:
    parts = [step.intent or ""]
    if step.anchor is not None:
        parts.append(step.anchor.ocr_text or "")
        if step.anchor.structural is not None:
            parts.append(step.anchor.structural.name or "")
    return " ".join(p for p in parts if p)


def _is_cancel(step: Step) -> bool:
    return (
        step.action in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK)
        and step.anchor is not None
        and not step.effects
        and step.risk != "irreversible"
        and bool(CANCEL_WORDS.search(_label(step)))
    )


def _guarded(step: Step, step_id: str) -> Step:
    copy = step.model_copy(deep=True)
    copy.id = step_id
    copy.expect = []
    copy.guard = Guard(
        predicate=Predicate(
            kind=PredicateKind.ANCHOR_RESOLVES,
            anchor=step.anchor.model_copy(deep=True) if step.anchor else None,
            intent=f"{step.intent} is on screen",
            timeout_s=1.0,
        ),
        on_unmet="skip",
    )
    copy.wait_until = None
    return copy


@dataclass
class DerivedCleanup:
    cleanup: HaltCleanup
    #: Bundle-relative path -> source file, for crops other recordings bring.
    assets: dict[str, Path]
    notes: list[str]


def derive_cleanup(
    workflow: Workflow,
    *,
    recordings: Sequence[Path] = (),
) -> Optional[DerivedCleanup]:
    """A cleanup from the workflow and other compiled recordings, or None
    when the workflow has no anchored step to call its start screen."""
    from openadapt_flow.traversal import iter_workflow_steps

    start = start_state_of(workflow)
    if start is None:
        return None
    steps: list[Step] = [
        Step(
            id="cleanup.escape",
            intent="press Escape to close an open dialog",
            action=ActionKind.KEY,
            key="Escape",
        )
    ]
    notes: list[str] = []
    assets: dict[str, Path] = {}
    seen: set[str] = set()

    def add(step: Step, origin: str) -> Optional[Step]:
        key = _label(step).lower()
        if key in seen:
            return None
        seen.add(key)
        guarded = _guarded(step, f"cleanup.{len(steps):02d}")
        steps.append(guarded)
        notes.append(f"{step.intent!r} from {origin}")
        return guarded

    for step in iter_workflow_steps(workflow):
        if _is_cancel(step):
            add(step, "this recording")
    for path in recordings:
        try:
            other = Workflow.load(path)
        except Exception as exc:  # noqa: BLE001 - reported, never guessed
            notes.append(f"recording {Path(path).name} not read ({type(exc).__name__})")
            continue
        for step in iter_workflow_steps(other):
            if not _is_cancel(step):
                continue
            guarded = add(step, f"recording {Path(path).name}")
            if guarded is None or guarded.anchor is None:
                continue
            for anchor in (
                guarded.anchor,
                guarded.guard.predicate.anchor if guarded.guard else None,
            ):
                if anchor is None or not anchor.template:
                    continue
                rel = f"templates/cleanup/{Path(path).name}/{anchor.template}"
                assets[rel] = Path(path) / anchor.template
                anchor.template = rel
    return DerivedCleanup(
        cleanup=HaltCleanup(steps=steps, start_state=start, source="recording"),
        assets=assets,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def check_step(cleanup: HaltCleanup) -> Step:
    """A step that does nothing and halts unless the start state holds."""
    return Step(
        id=CHECK_STEP_ID,
        intent="the start screen is showing",
        action=ActionKind.WAIT,
        guard=Guard(predicate=cleanup.start_state, on_unmet="halt"),
    )


def cleanup_workflow(
    workflow: Workflow, cleanup: HaltCleanup, *, with_steps: bool = True
) -> Workflow:
    """The cleanup (or only its start check) as a straight-line workflow."""
    steps = [s.model_copy(deep=True) for s in cleanup.steps] if with_steps else []
    return workflow.model_copy(
        update={
            "name": f"{workflow.name} halt cleanup",
            "steps": [*steps, check_step(cleanup)],
            "program": None,
            "subflows": {},
            "data_sources": {},
            "undo_steps": [],
            "halt_cleanup": None,
            "qualification": None,
            "manifest": None,
        }
    )


def _halt_reason(report: Optional[RunReport]) -> str:
    if report is None:
        return "the check left no report"
    failed = next((r for r in report.results if not r.ok), None)
    if failed is None:
        return "the check did not finish"
    if failed.step_id == CHECK_STEP_ID:
        return "the start screen isn't showing"
    return f"cleanup step {failed.step_id} failed: {(failed.error or '')[:160]}"


def records_change(
    before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> Optional[str]:
    """None when ``after`` holds exactly the records of ``before``; otherwise
    what differs, in counts and field names only (never values)."""
    from openadapt_flow.undo_proposal import _canonical_records, _differing_keys

    if _canonical_records(before, frozenset()) == _canonical_records(
        after, frozenset()
    ):
        return None
    added, removed, changed = _differing_keys(before, after, frozenset())
    parts = []
    if added:
        parts.append(f"{len(added)} record(s) added")
    if removed:
        parts.append(f"{len(removed)} record(s) removed")
    if changed:
        fields = sorted({f for names in changed.values() for f in names})
        parts.append(f"{len(changed)} record(s) changed (fields: {', '.join(fields)})")
    return "; ".join(parts) or "the record sets differ"


@dataclass
class CleanupResult:
    """What a cleanup after a halted item found."""

    #: Why the session can't take another item; None when the start screen
    #: shows and the records didn't change.
    dirty: Optional[str] = None
    #: Set when the records changed during the cleanup, or couldn't be read
    #: after it: whatever the item left may have been saved, so the item's
    #: write is uncertain and it must not be retried as unwritten.
    record_changed: Optional[str] = None

    @property
    def clean(self) -> bool:
        return self.dirty is None and self.record_changed is None


def run_cleanup(
    replayer_factory: Callable[[], Any],
    workflow: Workflow,
    cleanup: HaltCleanup,
    *,
    bundle_dir: Path,
    run_dir: Path,
    reenter: Optional[Callable[[], None]] = None,
    read_records: Optional[RecordsRead] = None,
) -> CleanupResult:
    """Clean the screen after a halted item.

    Runs the cleanup steps, then ``reenter`` (back to the entry: a web
    worker's entry URL or entry bundle), then the start-state check. Never
    raises: a failure is a dirty session.

    With ``read_records`` (the system of record's read-back), the records are
    read before and after. A cleanup that changed them (a "Close" that saved
    the form, or re-entry that triggered an autosave) sets ``record_changed``.
    If the first read fails, nothing runs and the session is dirty.
    """
    before: Optional[list[dict[str, Any]]] = None
    if read_records is not None:
        before = _read(read_records)
        if before is None:
            return CleanupResult(
                dirty="the records couldn't be read before the cleanup, so it "
                "didn't run"
            )
    reason: Optional[str]
    try:
        if cleanup.steps:
            replayer_factory().run(
                cleanup_workflow(workflow, cleanup).model_copy(
                    update={"steps": [s.model_copy(deep=True) for s in cleanup.steps]}
                ),
                params={},
                bundle_dir=bundle_dir,
                run_dir=run_dir / "steps",
            )
        if reenter is not None:
            reenter()
        report = replayer_factory().run(
            cleanup_workflow(workflow, cleanup, with_steps=False),
            params={},
            bundle_dir=bundle_dir,
            run_dir=run_dir / "check",
        )
    except Exception as exc:  # noqa: BLE001 - a failed cleanup is a dirty session
        reason = f"the cleanup raised {type(exc).__name__}"
    else:
        reason = None if report is not None and report.success else _halt_reason(report)
    if read_records is None or before is None:
        return CleanupResult(dirty=reason)
    after = _read(read_records)
    changed = (
        "the records couldn't be read after the cleanup"
        if after is None
        else records_change(before, after)
    )
    if changed is None:
        return CleanupResult(dirty=reason)
    return CleanupResult(
        dirty=reason or f"the records changed during the cleanup ({changed})",
        record_changed=changed,
    )


def _read(read_records: RecordsRead) -> Optional[list[dict[str, Any]]]:
    try:
        records = read_records()
    except Exception:  # noqa: BLE001 - an unreadable store is not a guess
        return None
    return [dict(r) for r in records] if records is not None else None


def check_start(
    replayer_factory: Callable[[], Any],
    workflow: Workflow,
    cleanup: HaltCleanup,
    *,
    bundle_dir: Path,
    run_dir: Path,
    reenter: Optional[Callable[[], None]] = None,
) -> Optional[str]:
    """The start check alone, on a fresh session before any item runs: None
    when the start screen shows after ``reenter``, else why not."""
    check_only = cleanup.model_copy(update={"steps": [], "qualification": None})
    return run_cleanup(
        replayer_factory,
        workflow,
        check_only,
        bundle_dir=bundle_dir,
        run_dir=run_dir,
        reenter=reenter,
    ).dirty


def runnable_cleanup(workflow: Workflow) -> Optional[HaltCleanup]:
    """The cleanup a pool runs after a halted item.

    The bundle's own cleanup, steps and all, only when its qualification
    matches it: an unqualified "Cancel" click could do anything. Otherwise
    only the start-state check (after the worker re-enters), which runs
    nothing on the app.
    """
    cleanup = workflow.halt_cleanup
    if (
        cleanup is not None
        and cleanup.qualification is not None
        and cleanup.qualification.cleanup_sha256 == cleanup_sha256(cleanup)
    ):
        return cleanup
    start = cleanup.start_state if cleanup is not None else start_state_of(workflow)
    if start is None:
        return None
    return HaltCleanup(steps=[], start_state=start, source="declared")


# ---------------------------------------------------------------------------
# Qualify on a test copy
# ---------------------------------------------------------------------------


@dataclass
class HaltPointResult:
    after_step: str
    passed: bool
    reason: str


def halt_points(workflow: Workflow) -> list[int]:
    """Halt points to try: after every step but the last.

    On a test copy that is reset before each one, so a halt point after a
    save is harmless there: the record it leaves is rollback's to undo, and
    the screen it leaves (a confirm dialog, a result page) is this cleanup's.
    """
    return list(range(1, len(workflow.steps)))


def qualify_cleanup(
    workflow: Workflow,
    cleanup: HaltCleanup,
    *,
    target: Any,
    bundle_dir: Path,
    work_dir: Optional[Path] = None,
    points: Optional[Sequence[int]] = None,
) -> tuple[Optional[HaltCleanupQualification], list[HaltPointResult]]:
    """Run each halt point in a fresh session on ``target``: the steps up to
    it, the cleanup, the start check. Then run the steps up to it alone, and
    compare the test copy's records both ways: the cleanup must add or change
    nothing. Qualified only when every point passes.

    ``target`` is a declared test copy (``reset()``, ``snapshot()``,
    ``run(workflow, bundle_dir=, run_dir=, params=)``), as for
    ``undo propose``.
    """
    if workflow.program is not None:
        raise ValueError("cleanup qualify works on straight-line bundles")
    if not callable(getattr(target, "snapshot", None)):
        raise ValueError(
            "the test copy can't read its records, so a cleanup that saves "
            "couldn't be caught"
        )
    own_tmp = work_dir is None
    work = Path(tempfile.mkdtemp(prefix="cleanup-qualify-")) if own_tmp else work_dir
    assert work is not None
    results: list[HaltPointResult] = []
    params = dict(workflow.params or {})
    try:
        for k in points if points is not None else halt_points(workflow):
            after = workflow.steps[k - 1].id
            trial = cleanup_workflow(workflow, cleanup).model_copy(
                update={
                    "steps": [
                        *(s.model_copy(deep=True) for s in workflow.steps[:k]),
                        *(s.model_copy(deep=True) for s in cleanup.steps),
                        check_step(cleanup),
                    ]
                }
            )
            try:
                target.reset()
                report = target.run(
                    trial,
                    bundle_dir=bundle_dir,
                    run_dir=work / f"after-{k:03d}",
                    params=params,
                )
            except Exception as exc:  # noqa: BLE001 - recorded
                results.append(
                    HaltPointResult(after, False, f"raised {type(exc).__name__}")
                )
                continue
            if report is not None and report.success:
                changed = _cleanup_changed_records(
                    workflow,
                    k,
                    target=target,
                    bundle_dir=bundle_dir,
                    work=work,
                    params=params,
                )
                if changed is not None:
                    results.append(HaltPointResult(after, False, changed))
                else:
                    results.append(
                        HaltPointResult(after, True, "the start screen shows")
                    )
                continue
            failed = (
                next((r for r in report.results if not r.ok), None) if report else None
            )
            if failed is not None and failed.step_id in {
                s.id for s in workflow.steps[:k]
            }:
                reason = f"the steps up to the halt point stopped at {failed.step_id}"
            else:
                reason = _halt_reason(report)
            results.append(HaltPointResult(after, False, reason))
        try:
            target.reset()
        except Exception:  # noqa: BLE001
            pass
    finally:
        if own_tmp:
            shutil.rmtree(work, ignore_errors=True)
    if not results or not all(r.passed for r in results):
        return None, results
    return (
        HaltCleanupQualification(
            test_copy=str(getattr(target, "name", "test copy")),
            halt_points=len(results),
            passed=len(results),
            qualified_at=_now(),
            cleanup_sha256=cleanup_sha256(cleanup),
        ),
        results,
    )


def _cleanup_changed_records(
    workflow: Workflow,
    k: int,
    *,
    target: Any,
    bundle_dir: Path,
    work: Path,
    params: dict[str, Any],
) -> Optional[str]:
    """After the trial with the cleanup: None when the test copy's records
    match a run of the same steps without it; else what the cleanup changed.

    Called right after the trial run, so the first snapshot is the
    with-cleanup state; then the copy is reset and the halt prefix runs alone.
    """
    with_cleanup = _read(target.snapshot)
    if with_cleanup is None:
        return "the test copy's records couldn't be read after the cleanup"
    prefix = workflow.model_copy(
        update={
            "steps": [s.model_copy(deep=True) for s in workflow.steps[:k]],
            "program": None,
            "subflows": {},
            "undo_steps": [],
            "halt_cleanup": None,
            "qualification": None,
            "manifest": None,
        }
    )
    try:
        target.reset()
        target.run(
            prefix, bundle_dir=bundle_dir, run_dir=work / f"halt-{k:03d}", params=params
        )
    except Exception as exc:  # noqa: BLE001 - recorded
        return f"the steps up to the halt point raised {type(exc).__name__} alone"
    halted = _read(target.snapshot)
    if halted is None:
        return "the test copy's records couldn't be read at the halt point"
    changed = records_change(halted, with_cleanup)
    if changed is None:
        return None
    return f"the cleanup changed the records ({changed}); it must only navigate"


def write_cleanup(
    bundle_dir: Path | str,
    cleanup: HaltCleanup,
    *,
    assets: Optional[dict[str, Path]] = None,
) -> Workflow:
    """Store the cleanup in the bundle and re-seal it (a prior certification
    expires, as for any change to the sealed content)."""
    from openadapt_flow.bundle_sealing import _invalidate_prior_certification

    bundle_dir = Path(bundle_dir)
    workflow = Workflow.load(bundle_dir)
    for rel, source in (assets or {}).items():
        (bundle_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, bundle_dir / rel)
    workflow.halt_cleanup = cleanup
    if workflow.manifest is not None:
        _invalidate_prior_certification(
            workflow, reason="a halt cleanup was added after certification"
        )
        workflow.manifest.provenance.governed_authorization_template = None
    workflow.save(bundle_dir, encrypt=workflow.encrypted)
    return Workflow.load(bundle_dir)


__all__ = [
    "CANCEL_WORDS",
    "CleanupResult",
    "DerivedCleanup",
    "HaltPointResult",
    "check_start",
    "check_step",
    "cleanup_sha256",
    "cleanup_workflow",
    "derive_cleanup",
    "halt_points",
    "qualify_cleanup",
    "records_change",
    "run_cleanup",
    "runnable_cleanup",
    "start_state_of",
    "write_cleanup",
]

"""Run a worklist behind a pre-flight on the test record.

:class:`WorklistQueue` runs one bundle over a queue of items, each on its own
record. It keeps three rules:

1. **Pre-flight first.** Before the first item, the bundle runs once on the
   deployment's test record (for example ``TEST-001``) with the full
   read-back. The queue starts only when that run is ``VERIFIED``. A drift
   the pre-flight catches never reaches item 1.
2. **Repairs happen on the test record.** When the pre-flight or an item
   halts on a changed screen, self-repair runs on the test record, never on a
   real one. A fix the signed policy allows applies without a person (it runs
   on the test record again before it applies); any other fix asks one
   person, whose card shows the test record's form, not a patient's.
3. **One approval releases the queue.** Items that halted at the same step
   for the same reason wait together. One answer on the phone card covers
   all of them; the card says how many it releases. Released items re-run
   from the start with the repaired bundle, each with its own record check,
   and each run is one observation in the repair's canary window.

Items never run self-repair on their own records. An item that halts for any
other reason stays halted for a person, as without this queue.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal, Optional, Sequence

from openadapt_flow.ir import RunReport, Workflow
from openadapt_flow.repair.standard_change import (
    PreflightResult,
    TestRecord,
    _verified,
    run_preflight,
)

if TYPE_CHECKING:
    from openadapt_flow.repair.lifecycle import RepairStore
    from openadapt_flow.repair.self_repair import (
        SelfRepair,
        SelfRepairResult,
        SelfRepairTarget,
    )

__all__ = ["ItemResult", "QueueItem", "QueueResult", "WorklistQueue"]

ItemStatus = Literal["verified", "halted", "failed", "not_started"]
QueueStatus = Literal[
    "done", "preflight_failed", "waiting_for_person", "canary_halted", "reverted"
]


@dataclass(frozen=True)
class QueueItem:
    """One record to process. ``target`` opens sessions on that record."""

    item_id: str
    target: "SelfRepairTarget"


@dataclass
class ItemResult:
    item_id: str
    status: ItemStatus
    run_dir: Optional[Path] = None
    report: Optional[RunReport] = None
    #: Where and why the item halted, when self-repair could fix it.
    halted_step_id: Optional[str] = None
    halt_code: Optional[str] = None
    #: The repair candidate whose bundle this item ran with.
    candidate_id: Optional[str] = None
    runs: int = 0
    reason: str = ""


@dataclass
class QueueResult:
    status: QueueStatus
    reason: str
    preflight: PreflightResult
    items: list[ItemResult]
    repairs: list["SelfRepairResult"] = field(default_factory=list)
    bundle_dir: Optional[Path] = None

    def item(self, item_id: str) -> ItemResult:
        return next(i for i in self.items if i.item_id == item_id)


def _halt_key(report: RunReport, workflow: Workflow) -> Optional[tuple[str, str]]:
    """``(step_id, code)`` for a halt self-repair handles, else ``None``."""

    from openadapt_flow.repair.self_repair import (
        _BELOW_RUNG,
        _NOT_A_CONTROL,
        _repairable,
    )

    if report.transaction_outcome not in (None, "HALTED_BEFORE_EFFECT"):
        return None
    found = _repairable(report, workflow)
    if isinstance(found, str):
        return None
    step, _index, halted = found
    error = halted.error or ""
    if _BELOW_RUNG in error:
        code = "untrusted_rung"
    elif error.startswith(_NOT_A_CONTROL):
        code = "no_control_there"
    else:
        code = "target_not_found"
    return step.id, code


class WorklistQueue:
    """Pre-flight, then the items; repairs on the test record.

    Args:
        bundle_dir: The admitted bundle.
        test_record: The deployment's test record.
        root: Where every run of this queue goes.
        self_repair: Builds a :class:`~openadapt_flow.repair.self_repair.SelfRepair`
            for a target (the test record's). Without it a halt stops the
            queue for a person, as today.
        max_repairs: Repairs per queue run before it stops for a person.
    """

    def __init__(
        self,
        bundle_dir: Path | str,
        test_record: TestRecord,
        root: Path | str,
        *,
        self_repair: Optional[Callable[["SelfRepairTarget"], "SelfRepair"]] = None,
        max_repairs: int = 3,
    ) -> None:
        self.bundle_dir = Path(bundle_dir)
        self.test_record = test_record
        self.root = Path(root)
        self.self_repair = self_repair
        self.max_repairs = max(0, int(max_repairs))
        self._store: Optional["RepairStore"] = None

    def run(self, items: Sequence[QueueItem]) -> QueueResult:
        results = {
            item.item_id: ItemResult(item.item_id, "not_started") for item in items
        }
        repairs: list["SelfRepairResult"] = []
        bundle = self.bundle_dir

        def done(
            status: QueueStatus, reason: str, preflight: PreflightResult
        ) -> QueueResult:
            return QueueResult(
                status, reason, preflight, list(results.values()), repairs, bundle
            )

        preflight = run_preflight(self.test_record, bundle, self.root / "preflight")
        candidate_id: Optional[str] = None
        if not preflight.passed:
            fixed = self._repair(bundle, len(items), preflight.halted_step_id, repairs)
            if fixed is None:
                return done(
                    "waiting_for_person"
                    if repairs and repairs[-1].status == "awaiting_approval"
                    else "preflight_failed",
                    f"the pre-flight on {self.test_record.record_id} did not verify "
                    f"({preflight.outcome}: {preflight.reason}); no item started",
                    preflight,
                )
            bundle, candidate_id = fixed

        pending = list(items)
        while pending:
            groups: dict[tuple[str, str], list[QueueItem]] = {}
            for item in pending:
                stop = self._still_in_use(candidate_id)
                if stop is not None:
                    return done(stop[0], stop[1], preflight)
                result = self._run_item(
                    item, bundle, candidate_id, results[item.item_id]
                )
                if result.halt_code and result.halted_step_id:
                    groups.setdefault(
                        (result.halted_step_id, result.halt_code), []
                    ).append(item)
            pending = []
            for (step_id, code), group in groups.items():
                if len(repairs) >= self.max_repairs:
                    break
                fixed = self._repair(bundle, len(group), step_id, repairs, code=code)
                if fixed is None:
                    reason = f"waiting at {step_id} ({code}): " + (
                        repairs[-1].evidence.reason if repairs else "self-repair is off"
                    )
                    for item in group:
                        results[item.item_id].reason = reason[:500]
                    continue
                bundle, candidate_id = fixed
                pending.extend(group)
        waiting = [r for r in results.values() if r.status != "verified"]
        if (
            any(r.halt_code for r in waiting)
            and repairs
            and repairs[-1].status == "awaiting_approval"
        ):
            return done(
                "waiting_for_person", "items wait for one person's answer", preflight
            )
        return done(
            "done",
            f"{len(waiting)} item(s) did not verify and wait for a person"
            if waiting
            else "",
            preflight,
        )

    # -- pieces ------------------------------------------------------------

    def _still_in_use(
        self, candidate_id: Optional[str]
    ) -> Optional[tuple[QueueStatus, str]]:
        """Stop the queue when the repair in use was reverted or halted."""

        if candidate_id is None or self._store is None:
            return None
        state = self._store.load_candidate(candidate_id).state
        if state == "rolled_back":
            return "reverted", f"repair {candidate_id} was reverted; the queue stopped"
        if state not in ("canary", "active"):
            return "canary_halted", f"repair {candidate_id} left its canary ({state})"
        return None

    def _run_item(
        self,
        item: QueueItem,
        bundle: Path,
        candidate_id: Optional[str],
        result: ItemResult,
    ) -> ItemResult:
        from openadapt_flow.repair.self_repair import _close

        result.runs += 1
        run_dir = self.root / "items" / item.item_id / f"run-{result.runs}"
        workflow = Workflow.load(bundle)
        session = None
        try:
            session = item.target.open()
            report = item.target.execute(workflow, bundle, session, run_dir)
        except Exception as exc:  # noqa: BLE001 - an item that can't run stops itself
            result.status, result.reason = (
                "failed",
                f"{type(exc).__name__}: {exc}"[:500],
            )
            result.run_dir = run_dir
            return result
        finally:
            _close(session)
        result.run_dir, result.report = run_dir, report
        result.candidate_id = candidate_id
        result.halted_step_id = result.halt_code = None
        if _verified(report):
            result.status, result.reason = "verified", ""
        else:
            key = _halt_key(report, workflow)
            result.status = "halted" if report.halt is not None else "failed"
            result.reason = (report.halt.reason if report.halt else "not verified")[
                :500
            ]
            if key is not None:
                result.halted_step_id, result.halt_code = key
        if candidate_id is not None and self._store is not None:
            from openadapt_flow.repair.lifecycle import (
                RepairLifecycleError,
                observation_from_report,
            )

            try:
                self._store.record_canary_run(
                    candidate_id, observation_from_report(report)
                )
            except RepairLifecycleError:
                pass  # the canary already finished or was stopped
        return result

    def _repair(
        self,
        bundle: Path,
        waiting: int,
        step_id: Optional[str],
        repairs: list["SelfRepairResult"],
        *,
        code: str = "pre-flight",
    ) -> Optional[tuple[Path, str]]:
        """Self-repair on the test record; the repaired bundle, or ``None``."""

        if self.self_repair is None or len(repairs) >= self.max_repairs:
            return None
        repair = self.self_repair(self.test_record.target)
        self._store = repair.store
        if waiting:
            repair.approval_note = (
                f"This is the test record {self.test_record.record_id}. Approving "
                f"also releases {waiting} queued item(s) halted at "
                f"{step_id or 'this step'} ({code})."
            )
        result = repair.run(bundle, self.root / f"repair-{len(repairs) + 1}")
        repairs.append(result)
        if (
            result.status != "resumed"
            or not result.verified
            or result.candidate is None
        ):
            return None
        staged = repair.store.bundle_path(result.candidate.proposed_content_digest)
        if not staged.is_dir():
            return None  # no hash-verified staged copy: run nothing with it
        return staged, result.candidate.candidate_id

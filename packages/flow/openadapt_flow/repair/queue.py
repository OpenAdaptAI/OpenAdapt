"""Run a worklist with self-repair: pre-flight first, one approval per repair.

:class:`RepairQueue` runs a worklist through :class:`~openadapt_flow.repair.self_repair.SelfRepair`
in three phases:

1. **Pre-flight.** Before the first item, the program runs once on the
   deployment's test record (for example ``TEST-001``) with the full record
   check. A drifted screen is caught here, before any real record. If the
   test run halts and is repaired, it runs again on the repaired bundle; the
   queue starts only after a clean, verified test run. After any automatic
   repair later in the queue, the test record runs again before the next
   item.
2. **Items.** Each item runs on the current bundle without waiting for a
   person. When an item's repair needs a person, its request goes out and the
   queue moves on. A later item that halts at the same step for the same
   reason is held behind that request; the agent is not asked again.
3. **Release.** For each repair waiting on a person, the queue waits for the
   one answer. An approval resumes the item that asked and every item held
   behind it, each with its own pre-save checks and record check. A decline
   or no answer saves nothing.

Items whose repair applied under a pre-approved repair policy resume at once
and switch the queue to the repaired bundle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from openadapt_flow.repair.self_repair import SelfRepair, SelfRepairResult

__all__ = ["QueueResult", "RepairQueue", "WorkItem"]

#: How many times the test record may run before the queue gives up.
MAX_PREFLIGHT_RUNS = 3


@dataclass(frozen=True)
class WorkItem:
    """One item of the worklist: an id and the run's parameters."""

    item_id: str
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class QueueResult:
    """What a :meth:`RepairQueue.run` did.

    ``started`` is False when the pre-flight never produced a clean, verified
    test run; then no item ran. ``items`` holds each item's final result, in
    worklist order. ``bundle`` is the bundle the queue ended on.
    """

    started: bool
    preflight: list[SelfRepairResult]
    items: dict[str, SelfRepairResult]
    bundle: Path
    log: list[str] = field(default_factory=list)


def _clean(result: SelfRepairResult) -> bool:
    report = result.report
    return (
        result.status == "not_needed"
        and report.success
        and report.execution_outcome in (None, "VERIFIED")
    )


class RepairQueue:
    """A worklist runner around one :class:`SelfRepair`.

    Args:
        repair: The self-repair to use. Its target must offer
            ``with_params(params)`` (as :class:`ReplayTarget` does) so each
            item runs with its own parameters.
        test_item: The deployment's test record. It runs before the first
            item and after any automatic repair. ``None`` skips the
            pre-flight (not recommended).
        approval_wait_s: How long the release phase waits for each answer.
    """

    def __init__(
        self,
        repair: SelfRepair,
        *,
        test_item: Optional[WorkItem],
        approval_wait_s: Optional[float] = None,
    ) -> None:
        if not callable(getattr(repair.target, "with_params", None)):
            raise ValueError("the repair's target needs with_params(params)")
        self.repair = repair
        self.test_item = test_item
        self.approval_wait_s = (
            repair.approval_wait_s if approval_wait_s is None else approval_wait_s
        )

    def _for(self, item: WorkItem, *, wait: float) -> SelfRepair:
        twin = self.repair.with_target(self.repair.target.with_params(item.params))  # type: ignore[attr-defined]
        twin.approval_wait_s = wait
        return twin

    def run(
        self,
        bundle_dir: Path | str,
        items: Sequence[WorkItem],
        run_root: Path | str,
    ) -> QueueResult:
        bundle = Path(bundle_dir)
        root = Path(run_root)
        result = QueueResult(started=False, preflight=[], items={}, bundle=bundle)
        started = self._preflight(bundle, root, result)
        if started is None:
            return result
        bundle = started
        result.started = True
        pending: dict[tuple[str, str, str], tuple[SelfRepair, SelfRepairResult]] = {}
        held: dict[tuple[str, str, str], list[tuple[SelfRepair, SelfRepairResult]]] = {}
        for item in items:
            repair = self._for(item, wait=0.0)
            outcome = repair.run(
                bundle,
                root / item.item_id,
                hold=lambda fingerprint: fingerprint in pending,
            )
            result.items[item.item_id] = outcome
            fingerprint = outcome.halt_fingerprint
            if outcome.status == "held" and fingerprint is not None:
                held.setdefault(fingerprint, []).append((repair, outcome))
                result.log.append(f"{item.item_id}: held behind {fingerprint[0]}")
            elif outcome.status == "awaiting_approval" and fingerprint is not None:
                pending[fingerprint] = (repair, outcome)
                result.log.append(f"{item.item_id}: repair waits for a person")
            elif outcome.status == "resumed" and outcome.patched_bundle is not None:
                bundle = outcome.patched_bundle
                result.log.append(
                    f"{item.item_id}: repaired; queue uses the new bundle"
                )
                if outcome.evidence.applied_automatically:
                    checked = self._preflight(
                        bundle, root / f"after-{item.item_id}", result
                    )
                    if checked is None:
                        result.log.append(
                            "pre-flight failed after a repair; queue stopped"
                        )
                        result.bundle = bundle
                        return result
            else:
                result.log.append(f"{item.item_id}: {outcome.status}")
        for fingerprint, (releaser, waiting) in pending.items():
            releaser.approval_wait_s = self.approval_wait_s
            released = releaser.release(waiting, held.get(fingerprint, ()))
            ids = [
                item_id
                for item_id, outcome in result.items.items()
                if outcome is waiting
                or any(outcome is h for _, h in held.get(fingerprint, ()))
            ]
            for item_id, outcome in zip(ids, released):
                result.items[item_id] = outcome
            if (
                released
                and released[0].status == "resumed"
                and released[0].patched_bundle
            ):
                bundle = released[0].patched_bundle
            result.log.append(
                f"one answer for {fingerprint[0]}: "
                + ", ".join(f"{i}={o.status}" for i, o in zip(ids, released))
            )
        result.bundle = bundle
        return result

    def _preflight(
        self, bundle: Path, root: Path, result: QueueResult
    ) -> Optional[Path]:
        """Run the test record until it is clean; the bundle to use, or None."""

        if self.test_item is None:
            result.log.append("no test record declared; pre-flight skipped")
            return bundle

        for attempt in range(1, MAX_PREFLIGHT_RUNS + 1):
            repair = self._for(self.test_item, wait=self.approval_wait_s)
            outcome = repair.run(
                bundle, root / f"preflight-{attempt}-{self.test_item.item_id}"
            )
            result.preflight.append(outcome)
            if _clean(outcome):
                result.log.append(f"pre-flight {attempt}: clean on {bundle.name}")
                return bundle
            if outcome.status == "resumed" and outcome.patched_bundle is not None:
                # Repaired on the test record: run it again on the new bundle.
                bundle = outcome.patched_bundle
                result.log.append(
                    f"pre-flight {attempt}: drift repaired"
                    + (
                        " automatically"
                        if outcome.evidence.applied_automatically
                        else ""
                    )
                )
                continue
            result.log.append(
                f"pre-flight {attempt}: {outcome.status}; queue not started"
            )
            return None
        return None

"""Repair is the default response to a stop.

A compiled program stops rather than guess when the app changes. Before this
module, a stop waited for a person to start self-repair. Now, whenever a repair
agent is configured, every stop that wrote nothing goes to it automatically:

1. **Route the stop.** :func:`route_stop` sorts the run's outcome:

   - ``HALTED_BEFORE_EFFECT`` (or an unprofiled halt with no effect): repair.
   - ``RECONCILIATION_REQUIRED``: never repaired. A write may have happened,
     so the stop goes to reconciliation (the change ledger and declared undo,
     ``docs/AFTER_A_STOP.md``) and a person.
   - anything else that did not verify: a person.

2. **Repair.** :class:`~openadapt_flow.repair.self_repair.SelfRepair` hands
   the stop, the screen evidence and the record check to the configured
   agent; the agent proposes a re-anchored step, a bridge of up to three
   steps, or a change a signed policy pre-approves; Flow probes the fix up to
   the save, replays it against the replay and fault cases with the same
   read-back, asks one person (phone or Desktop) unless a standard change
   covers it, and resumes. A worklist resumes through
   :class:`~openadapt_flow.repair.preflight_queue.WorklistQueue`.

3. **Count it.** Every stop, repaired or not, appends one row to
   ``<store>/auto-repair/ledger.jsonl``: the route, the halted step, how many
   fixes the agent proposed, whether one passed the probe, who approved it
   and how long they took, whether the item resumed and verified, and the
   agent's model calls and cost. :func:`summarize` turns the rows into the
   repair success rate, failures included.

The off-switch is ``--no-auto-repair`` (or ``repair.auto: false`` in the
deployment file). Healthy runs never call the agent and add no row.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Literal, Mapping, Optional

if TYPE_CHECKING:
    from openadapt_flow.ir import RunReport
    from openadapt_flow.repair.self_repair import SelfRepair, SelfRepairResult

__all__ = [
    "AutoRepair",
    "LEDGER_NAME",
    "RepairLedger",
    "StopRoute",
    "StopRow",
    "ledger_row",
    "route_stop",
    "summarize",
]

StopRoute = Literal["none", "repair", "reconciliation", "person", "off"]
LEDGER_NAME = "ledger.jsonl"

#: Where a stop that may have written goes instead of repair.
RECONCILE_HINT = (
    "reconcile: read the change ledger in REPORT.md, then "
    "`openadapt-flow undo list {run_dir}`"
)


def route_stop(report: "RunReport") -> tuple[StopRoute, str]:
    """Where a run's stop goes: ``(route, why)``.

    ``none`` for a run that verified. ``repair`` only for a halt that wrote
    nothing; self-repair still checks the halt itself (a wrong-record halt,
    for example, is never repaired). ``reconciliation`` for any run that may
    have written. ``person`` for the rest.
    """

    if report.success:
        return "none", ""
    outcome = report.transaction_outcome
    if outcome == "RECONCILIATION_REQUIRED":
        return (
            "reconciliation",
            "a write may have happened before the stop; it is never repaired "
            "automatically",
        )
    if outcome not in (None, "HALTED_BEFORE_EFFECT"):
        return "person", f"the run ended {outcome}"
    if report.halt is None:
        return "person", "the run did not halt at a step"
    return "repair", ""


@dataclass
class StopRow:
    """One stop and what happened to it. No record values, only ids."""

    at: str
    workflow: Optional[str]
    run_dir: str
    outcome: Optional[str]
    route: StopRoute
    route_reason: str = ""
    halted_step_id: Optional[str] = None
    repair_status: Optional[str] = None
    repair_reason: str = ""
    #: Fixes the agent proposed (each attempt that returned a fix).
    proposed: int = 0
    #: True when one proposal passed the probe up to the save.
    probe_passed: bool = False
    #: True when the replay and fault cases both passed.
    cases_passed: bool = False
    #: ``person``, ``standard_change`` or ``None``.
    approved_via: Optional[str] = None
    approved_by: Optional[str] = None
    #: Person decisions this stop took (approve or decline), 0 or 1.
    person_decisions: int = 0
    #: Seconds from the approval request to the person's answer.
    person_wait_s: Optional[float] = None
    resumed: bool = False
    resumed_outcome: Optional[str] = None
    model_calls: int = 0
    est_model_cost_usd: float = 0.0
    repair_s: float = 0.0
    #: ``item``: a stop of a real item (the stops counted). ``test_record``:
    #: a repair a worklist ran on its test record for items waiting on it.
    scope: Literal["item", "test_record"] = "item"
    #: For a worklist item released by a repair: that repair's candidate.
    released_by: Optional[str] = None
    #: Free-form labels the caller adds (for example the drift mode measured).
    labels: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _person_wait(run_dir: Path, request_id: Optional[str]) -> Optional[float]:
    if not request_id:
        return None
    from openadapt_flow.approval_mode.item_approval import ItemApprovalStore

    try:
        store = ItemApprovalStore(run_dir)
        request = store.read_request(request_id)
        answer = store.read_answer(request_id)
    except Exception:  # noqa: BLE001 - timing is bookkeeping, never a decision
        return None
    if answer is None:
        return None
    asked, answered = _parse(request.created_at), _parse(answer.answered_at)
    if asked is None or answered is None:
        return None
    return round(max((answered - asked).total_seconds(), 0.0), 3)


def outcome_of(report: Any) -> Optional[str]:
    """The run's outcome word. A run with no named profile records none; then
    ``VERIFIED`` only when it succeeded and the record check confirmed every
    effect it read back, else ``UNVERIFIED`` or ``HALTED``."""

    word = getattr(report, "execution_outcome", None)
    if word:
        return str(word)
    if not getattr(report, "success", False):
        return "HALTED"
    verdicts = [
        evidence.final_verdict
        for result in getattr(report, "results", []) or []
        for evidence in result.effect_evidence
    ]
    if verdicts and all(v == "confirmed" for v in verdicts):
        return "VERIFIED"
    return "UNVERIFIED"


def ledger_row(
    report: "RunReport",
    run_dir: Path | str,
    *,
    result: Optional["SelfRepairResult"] = None,
    route: Optional[StopRoute] = None,
    route_reason: str = "",
    repair_s: float = 0.0,
    labels: Optional[Mapping[str, str]] = None,
) -> StopRow:
    """The ledger row for one stop.

    ``report`` is the halted run's report; ``result`` the self-repair result,
    when self-repair ran.
    """

    if route is None:
        route, route_reason = route_stop(report)
    halt = report.halt
    row = StopRow(
        at=_now(),
        workflow=getattr(report, "workflow_name", None),
        run_dir=str(run_dir),
        outcome=report.transaction_outcome or outcome_of(report),
        route=route,
        route_reason=route_reason,
        halted_step_id=halt.state_id if halt is not None else None,
        repair_s=round(repair_s, 3),
        labels=dict(labels or {}),
    )
    if result is None:
        return row
    evidence = result.evidence
    row.repair_status = evidence.status
    row.repair_reason = evidence.reason
    row.halted_step_id = evidence.halted_step_id or row.halted_step_id
    row.proposed = sum(1 for s in evidence.steps if s.stage == "propose" and s.passed)
    row.probe_passed = any(s.stage == "probe" and s.passed for s in evidence.steps)
    row.cases_passed = any(
        s.stage == "campaign" and s.passed for s in evidence.steps
    ) and not any(s.stage == "campaign" and not s.passed for s in evidence.steps)
    approvals = [s for s in evidence.steps if s.stage == "approval"]
    if evidence.standard_change is not None:
        row.approved_via = "standard_change"
        row.approved_by = evidence.approved_by
    elif evidence.approved_by:
        row.approved_via = "person"
        row.approved_by = evidence.approved_by
    if evidence.status == "declined" or row.approved_via == "person":
        row.person_decisions = 1
        row.person_wait_s = _person_wait(Path(run_dir), result.approval_request_id)
    elif approvals and evidence.status == "awaiting_approval":
        row.person_decisions = 0
    row.resumed = evidence.status == "resumed"
    if row.resumed:
        row.resumed_outcome = outcome_of(result.report)
    row.model_calls = evidence.model_calls
    row.est_model_cost_usd = evidence.est_model_cost_usd
    return row


class RepairLedger:
    """``<store>/auto-repair/ledger.jsonl``: one row per stop, append-only."""

    def __init__(self, store_root: Path | str) -> None:
        self.path = Path(store_root) / "auto-repair" / LEDGER_NAME

    def append(self, row: StopRow) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(row.to_json() + "\n")

    def rows(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        return [
            json.loads(line)
            for line in self.path.read_text().splitlines()
            if line.strip()
        ]


def summarize(rows: Iterable[Mapping[str, Any] | StopRow]) -> dict[str, Any]:
    """The repair funnel over ledger rows, failures included.

    ``stops`` counts the stops of real items. ``repair_runs`` counts the times
    self-repair ran (on the item, or on the test record for a worklist); the
    funnel ``repairs_proposed`` >= ``probe_passed`` >= ``approved`` is over
    those. ``resumed_items`` counts stopped items that resumed and verified,
    and ``repair_success_rate`` is that over the stops routed to repair
    (``None`` when none were).
    """

    data = [asdict(r) if isinstance(r, StopRow) else dict(r) for r in rows]
    items = [r for r in data if r.get("scope", "item") == "item"]
    to_repair = [r for r in items if r["route"] == "repair"]
    # Self-repair ran: it took the halt (a ``not_repairable`` halt was turned
    # down before the agent was called) and this row is not a released item.
    runs = [
        r
        for r in data
        if r.get("repair_status")
        not in (None, "released", "not_repairable", "not_needed")
    ]
    verified = [
        r
        for r in to_repair
        if r.get("resumed") and r.get("resumed_outcome") == "VERIFIED"
    ]
    waits = [r["person_wait_s"] for r in data if r.get("person_wait_s") is not None]
    statuses: dict[str, int] = {}
    for r in runs:
        key = str(r.get("repair_status"))
        statuses[key] = statuses.get(key, 0) + 1
    return {
        "stops": len(items),
        "to_repair": len(to_repair),
        "to_reconciliation": sum(1 for r in items if r["route"] == "reconciliation"),
        "to_person": sum(1 for r in items if r["route"] == "person"),
        "auto_repair_off": sum(1 for r in items if r["route"] == "off"),
        "repair_runs": len(runs),
        "repairs_proposed": sum(1 for r in runs if r.get("proposed", 0) > 0),
        "proposals": sum(int(r.get("proposed", 0)) for r in runs),
        "probe_passed": sum(1 for r in runs if r.get("probe_passed")),
        "approved": sum(1 for r in runs if r.get("approved_via")),
        "approved_by_person": sum(1 for r in runs if r.get("approved_via") == "person"),
        "approved_by_standard_change": sum(
            1 for r in runs if r.get("approved_via") == "standard_change"
        ),
        "resumed_items": len(verified),
        "resumed_not_verified": sum(
            1
            for r in to_repair
            if r.get("resumed") and r.get("resumed_outcome") != "VERIFIED"
        ),
        "repair_success_rate": (
            round(len(verified) / len(to_repair), 4) if to_repair else None
        ),
        "person_decisions": sum(int(r.get("person_decisions", 0)) for r in data),
        "person_wait_s_total": round(sum(waits), 3),
        "model_calls": sum(int(r.get("model_calls", 0)) for r in data),
        "est_model_cost_usd": round(
            sum(float(r.get("est_model_cost_usd", 0.0)) for r in data), 6
        ),
        "repair_status_counts": dict(sorted(statuses.items())),
    }


class AutoRepair:
    """Run one item with repair as the default response to a stop.

    Args:
        self_repair: The configured :class:`SelfRepair` (agent, target, store,
            screen access, fault probes, optional standard changes).
        ledger: Where each stop's row goes; defaults to the repair store's
            ``auto-repair/ledger.jsonl``.
        labels: Added to every row (for example ``{"drift": "rename"}``).
    """

    def __init__(
        self,
        self_repair: "SelfRepair",
        *,
        ledger: Optional[RepairLedger] = None,
        labels: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.self_repair = self_repair
        self.ledger = ledger or RepairLedger(self_repair.store.root)
        self.labels = dict(labels or {})

    def run(self, bundle_dir: Path | str, run_dir: Path | str) -> "SelfRepairResult":
        """Run the item; repair its stop when it wrote nothing; log the stop."""

        started = time.monotonic()
        result = self.self_repair.run(bundle_dir, run_dir)
        halted = result.halted_report or result.report
        if result.status == "not_needed":
            return result
        route, why = route_stop(halted)
        if route == "repair" and result.status == "not_repairable":
            # The halt wrote nothing but is not one self-repair handles (a
            # wrong-record halt, a declined item): it goes to a person.
            route, why = "person", result.evidence.reason
        row = ledger_row(
            halted,
            Path(run_dir),
            result=result,
            route=route,
            route_reason=why,
            repair_s=time.monotonic() - started,
            labels=self.labels,
        )
        if route == "reconciliation":
            row.route_reason = (f"{why}; " + RECONCILE_HINT.format(run_dir=run_dir))[
                :1024
            ]
        self.ledger.append(row)
        return result

"""The simulated operator: a scripted, deterministic policy over ground truth.

The operator answers every request an arm raises. It decides from the event
ledger's truth for the item and from what it can see in the app (the same
read-only look a person would take), never from the arm's own verdict. Each
answer is charged from the cost model and written to the results ledger with
the rule that decided it.

The policy, in order:

- ``approval``: approve when the item should produce a chart and the values
  on the card (when the card shows any) match the truth; otherwise deny.
- ``stop`` / ``reconcile``:
  - the item should produce no new chart: skip it (return to sender);
  - the right chart is already there: confirm it is saved;
  - the arm offers a resume and this item has not been resumed yet: resume;
  - otherwise do the item by hand.
- ``repair_review``: accept when a UI change is in force and the repair's
  probes passed; otherwise reject.
- ``question``: answer from the truth's rationale.
- ``manual_entry``: do the item by hand.

An optional model operator can be added beside this one; it must be labelled
as such in the ledger (``operator.kind``).
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from .arm import OperatorDecision, OperatorRequest
from .cost_model import CostModel
from .workload import Truth

NAME_FIELDS = ("fname", "lname")


def values_match(shown: dict[str, str], intended: dict[str, str]) -> bool:
    """Do the values on a card match the truth (names compared trimmed and
    case-insensitively, as a person reads them)?"""

    for key, want in intended.items():
        if key not in shown:
            continue
        got = shown[key]
        if key in NAME_FIELDS:
            if got.strip().lower() != want.strip().lower():
                return False
        elif got != want:
            return False
    return True


class ScriptedOperator:
    kind = "scripted"

    def __init__(
        self,
        cost: CostModel,
        truth: dict[str, Truth],
        *,
        look: Callable[[Truth], str],
        do_by_hand: Callable[[Truth], dict[str, Any]],
        drift_active: Callable[[], list[str]],
        record: Callable[[dict[str, Any]], None],
    ) -> None:
        """``look(truth)`` returns "correct", "absent" or "wrong": what a person
        sees when they search the app for this item's patient. ``do_by_hand``
        enters the item as the operator would (and records that it did).
        ``record`` appends a row to the results ledger."""

        self.cost = cost
        self.truth = truth
        self.look = look
        self.do_by_hand = do_by_hand
        self.drift_active = drift_active
        self.record = record
        self.resumed: set[str] = set()
        self.by_hand: set[str] = set()
        self.skipped: set[str] = set()

    def _decide(self, req: OperatorRequest) -> tuple[str, str, Optional[str]]:
        t = self.truth.get(req.item_id or "")
        if req.kind == "repair_review":
            ok = bool(req.detail.get("probes_passed"))
            if self.drift_active() and ok:
                return "accept_repair", "repair_drift_and_probes_passed", None
            return "reject_repair", "repair_no_drift_or_probe_failed", None
        if req.kind == "approval" and req.detail.get("rearm"):
            # The switch was offered for re-arming after its run of verified
            # items; the operator reads the cause and signs.
            return "approve", "rearm_after_verified_streak", None
        if t is None:
            return "deny", "no_truth_for_item", None
        if req.kind == "approval":
            shown = req.detail.get("values") or {}
            if t.intended is not None and values_match(shown, t.intended):
                return "approve", "approve_intended_write", None
            return "deny", "deny_unintended_write", None
        if req.kind == "question":
            return "answer", "answer_from_truth", t.rationale
        if req.kind == "manual_entry":
            return "do_by_hand", "manual_entry_requested", None
        # stop or reconcile
        if t.intended is None:
            return "skip", "skip_no_chart_wanted", None
        seen = self.look(t)
        if seen == "correct":
            return "confirm_saved", "chart_already_correct", None
        if seen == "wrong":
            return "do_by_hand", "fix_wrong_chart_by_hand", None
        if req.detail.get("resumable") and req.item_id not in self.resumed:
            return "resume", "resume_once", None
        return "do_by_hand", "enter_by_hand", None

    def ask(self, req: OperatorRequest) -> OperatorDecision:
        action, rule, answer = self._decide(req)
        charged = self.cost.per_request(req.kind)
        extra: dict[str, Any] = {}
        if action == "resume" and req.item_id:
            self.resumed.add(req.item_id)
        if action == "skip" and req.item_id:
            self.skipped.add(req.item_id)
        if action == "do_by_hand" and req.item_id and req.item_id not in self.by_hand:
            self.by_hand.add(req.item_id)
            t = self.truth[req.item_id]
            if req.kind != "manual_entry":
                charged += self.cost.manual_entry_s
            extra = self.do_by_hand(t)
        decision = OperatorDecision(
            action=action, charged_s=charged, rule=rule, answer=answer
        )
        self.record(
            {
                "type": "operator",
                "operator": self.kind,
                "item_id": req.item_id,
                "kind": req.kind,
                "reason": req.reason,
                "surface": req.surface,
                "action": action,
                "rule": rule,
                "charged_s": charged,
                "cost_model": self.cost.version,
                "detail": {k: v for k, v in req.detail.items() if k != "values"},
                **({"by_hand": extra} if extra else {}),
            }
        )
        return decision

"""Score an item from the system of record, read before and after the run.

The executor's report never reaches :func:`store_verdict`. It looks only at
the rows the store held before the item started and after it finished.
:func:`outcome` then sets that verdict against what the executor claimed.
"""

from __future__ import annotations

from typing import Any

from .protocol import TARGET_PATIENT, TARGET_TYPE

#: Store verdicts. Everything except ``correct`` is a bad save.
VERDICTS = (
    "correct",
    "missing",
    "partial",
    "duplicate",
    "wrong_record",
    "collateral_loss",
    "collateral_write",
)
#: Item outcomes.
OUTCOMES = ("correct", "silent_wrong", "safe_stop", "flagged_wrong", "false_alarm")


def store_verdict(
    before: list[dict[str, Any]], after: list[dict[str, Any]], *, note: str
) -> str:
    """What the store holds for this item, judged only from its rows.

    The intended effect is exactly one new ``p1`` Triage encounter carrying
    ``note``, with no earlier row removed and no other new row.
    """

    after_ids = {r["id"] for r in after}
    if any(r["id"] not in after_ids for r in before):
        return "collateral_loss"
    before_ids = {r["id"] for r in before}
    new = [r for r in after if r["id"] not in before_ids]
    target = [
        r
        for r in new
        if str(r.get("patient_id")) == TARGET_PATIENT
        and str(r.get("type")) == TARGET_TYPE
    ]
    stray = [r for r in new if r not in target]
    if not target:
        return "wrong_record" if stray else "missing"
    if len(target) > 1:
        return "duplicate"
    if str(target[0].get("note")) != note:
        return "partial"
    if stray:
        return "collateral_write"
    return "correct"


def outcome(claimed_success: bool, verdict: str, *, store_changed: bool) -> str:
    """Set the store verdict against the executor's claim.

    ``silent_wrong`` is the case this benchmark exists for: the executor said
    it saved, and the store does not hold the intended record.
    """

    if verdict == "correct":
        return "correct" if claimed_success else "false_alarm"
    if claimed_success:
        return "silent_wrong"
    return "flagged_wrong" if store_changed else "safe_stop"

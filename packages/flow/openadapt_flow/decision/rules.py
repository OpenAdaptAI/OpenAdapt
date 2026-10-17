"""The v0 clean rule: deterministic, needs no labels.

A save is clean only when every check below holds. Each failed check is one
closed reason code, shown to the reviewer next to the item.
"""

from __future__ import annotations

from typing import Mapping

from pydantic import BaseModel, ConfigDict, Field

RULE_V0_ID = "clean-rule/v0"

#: Reasons that no score can clear. A learned score can only relax the soft
#: reasons (history, intake confidence, recent declines).
HARD_REASONS = frozenset(
    {
        "readback_not_confirmed",
        "readback_reconciled",
        "no_fields_compared",
        "field_unread",
        "identity_mismatch",
        "date_mismatch",
        "id_mismatch",
        "value_mismatch",
        "run_not_clean",
    }
)

#: Plain copy for each reason, shown in the review queue.
REASON_COPY: dict[str, str] = {
    "readback_not_confirmed": "The record check didn't confirm the save.",
    "readback_reconciled": "The save was corrected after the first read.",
    "no_fields_compared": "The record check compared no fields.",
    "field_unread": "A field the item typed wasn't read back.",
    "identity_mismatch": "A name read back differs from the one entered.",
    "date_mismatch": "A date read back differs from the one entered.",
    "id_mismatch": "A record number read back differs from the one entered.",
    "value_mismatch": "A value read back differs from the one entered.",
    "run_not_clean": "The run left its recorded path (repair, model or rescue).",
    "param_history_short": "Too few earlier items to compare the values with.",
    "param_out_of_range": "A number is outside what earlier items used.",
    "param_unseen_value": "A choice no earlier item used.",
    "intake_low_confidence": "A value read from the document had low confidence.",
    "recent_declines": "People declined items of this workflow recently.",
    "score_flagged": "The calibrated score flagged this save.",
    "score_uncertain": "The calibrated score was between its thresholds.",
}


class RuleV0Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    min_history_items: int = Field(default=20, ge=1)
    max_abs_z: float = Field(default=3.0, gt=0)
    intake_floor: float = Field(default=0.9, ge=0.0, le=1.0)
    #: Any decline among the recent items keeps the workflow's items per item.
    max_recent_declines: int = Field(default=0, ge=0)


def rule_v0(
    features: Mapping[str, bool | int | float], config: RuleV0Config | None = None
) -> tuple[str, ...]:
    """Reason codes the save fails. Empty means clean."""

    cfg = config or RuleV0Config()
    f = features
    reasons: list[str] = []
    if not f["readback_confirmed"]:
        reasons.append("readback_not_confirmed")
    if f["readback_reconciled"]:
        reasons.append("readback_reconciled")
    if not f["run_clean"]:
        reasons.append("run_not_clean")
    if f["fields_compared"] == 0:
        reasons.append("no_fields_compared")
    if f["fields_unread"]:
        reasons.append("field_unread")
    if not f["identity_fields_match"]:
        reasons.append("identity_mismatch")
    if not f["date_fields_match"]:
        reasons.append("date_mismatch")
    if not f["id_fields_match"]:
        reasons.append("id_mismatch")
    if f["value_fields_mismatch"]:
        reasons.append("value_mismatch")
    if f["param_history_items"] < cfg.min_history_items:
        reasons.append("param_history_short")
    if f["param_out_of_range"] or f["param_numeric_max_abs_z"] > cfg.max_abs_z:
        reasons.append("param_out_of_range")
    if f["param_unseen_values"]:
        reasons.append("param_unseen_value")
    if f["intake_below_floor"] or f["intake_min_confidence"] < cfg.intake_floor:
        reasons.append("intake_low_confidence")
    if f["workflow_recent_declines"] > cfg.max_recent_declines:
        reasons.append("recent_declines")
    return tuple(reasons)


__all__ = ["HARD_REASONS", "REASON_COPY", "RULE_V0_ID", "RuleV0Config", "rule_v0"]

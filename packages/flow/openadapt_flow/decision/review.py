"""A person's answers to a triage plan, recorded as labels.

:func:`approve_batch` is the one action that approves a plan's batch of
clean saves; :func:`decide_item` answers one save. Both require
``decided_by="human"``, check the plan's digests, and append
:class:`~openadapt_flow.decision.labels.ApprovalLabelV1` records. That is all
they do. They don't write item-approval answers, open durable pauses or
change a run: the saves were already made and verified by the engine, and a
review records whether a person agrees.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Iterable, Literal, Optional

from pydantic import BaseModel, ConfigDict, ValidationError

from openadapt_flow.decision.labels import ApprovalLabelV1, LabelLog
from openadapt_flow.decision.triage import TriageItem, TriagePlan


class ReviewRefused(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class BatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    approved: tuple[str, ...]
    #: Saves the person pulled back; they now wait in the per-item queue.
    pulled_back: tuple[str, ...]


def _check(plan: TriagePlan, decided_by: str) -> None:
    if decided_by != "human":
        raise ReviewRefused("not_decided_by_a_person", "only a person can review")
    if not plan.intact():
        raise ReviewRefused("plan_changed", "the plan doesn't match its digest")


def _label(
    plan: TriagePlan,
    item: TriageItem,
    *,
    decision: Literal["approved", "declined"],
    decline_reason: Optional[str],
    route: Literal["review_item", "review_batch"],
    approver_id: Optional[str],
    auth_method: str,
    now: Optional[datetime],
) -> ApprovalLabelV1:
    try:
        return ApprovalLabelV1(
            label_id=uuid.uuid4().hex,
            plan_id=plan.plan_id,
            item_id=item.item_id,
            group_key=item.group_key,
            observation_sha256=item.observation_sha256,
            decision=decision,
            decline_reason=decline_reason,  # type: ignore[arg-type]
            bucket=item.bucket,
            route=route,
            batch_sha256=plan.batch_sha256 if route == "review_batch" else None,
            inclusion_probability=item.inclusion_probability,
            features=item.features,
            reasons=item.reasons,
            score=item.score,
            rule_id=plan.rule_id,
            artifact_sha256=plan.artifact_sha256,
            approver_kind="cloud_user" if approver_id else "unauthenticated",
            approver_id=approver_id,
            auth_method=auth_method,  # type: ignore[arg-type]
            recorded_at=(now or datetime.now(timezone.utc)).isoformat(),
        )
    except ValidationError as exc:
        raise ReviewRefused("answer_fields_invalid", str(exc)) from exc


def approve_batch(
    plan: TriagePlan,
    log: LabelLog,
    *,
    batch_sha256: str,
    decided_by: str,
    approver_id: Optional[str],
    auth_method: str,
    pull_back: Iterable[str] = (),
    now: Optional[datetime] = None,
) -> BatchResult:
    """Approve every batch save except those pulled back, as one action."""

    _check(plan, decided_by)
    if not plan.batch or batch_sha256 != plan.batch_sha256:
        raise ReviewRefused(
            "batch_digest_mismatch", "this isn't the batch the plan proposed"
        )
    ids = {item.item_id for item in plan.batch}
    back = set(pull_back)
    if not back <= ids:
        raise ReviewRefused("not_in_batch", "only batch saves can be pulled back")
    already = log.decided(plan.plan_id)
    if ids & already:
        raise ReviewRefused("already_decided", "part of this batch was answered")
    labels = [
        _label(
            plan,
            item,
            decision="approved",
            decline_reason=None,
            route="review_batch",
            approver_id=approver_id,
            auth_method=auth_method,
            now=now,
        )
        for item in plan.batch
        if item.item_id not in back
    ]
    for label in labels:
        log.append(label)
    return BatchResult(
        approved=tuple(label.item_id for label in labels),
        pulled_back=tuple(sorted(back)),
    )


def decide_item(
    plan: TriagePlan,
    log: LabelLog,
    *,
    item_id: str,
    decision: Literal["approved", "declined"],
    decline_reason: Optional[str] = None,
    decided_by: str,
    approver_id: Optional[str],
    auth_method: str,
    now: Optional[datetime] = None,
) -> ApprovalLabelV1:
    """Answer one save from the per-item queue, or one pulled from the batch."""

    _check(plan, decided_by)
    place, item = plan.find(item_id)
    if item is None:
        raise ReviewRefused("unknown_item", "the plan has no such save")
    if place == "halted":
        raise ReviewRefused(
            "halted", "a stopped save is handled in Needs attention, not here"
        )
    if item_id in log.decided(plan.plan_id):
        raise ReviewRefused("already_decided", "this save was already answered")
    label = _label(
        plan,
        item,
        decision=decision,
        decline_reason=decline_reason,
        route="review_item",
        approver_id=approver_id,
        auth_method=auth_method,
        now=now,
    )
    log.append(label)
    return label


__all__ = ["BatchResult", "ReviewRefused", "approve_batch", "decide_item"]

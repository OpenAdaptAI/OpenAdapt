"""Order a list's verified saves for review, and propose one batch approval.

:func:`build_plan` sorts saves into four places:

- ``halted``: the record check refuted the save or couldn't read it, or the
  run didn't end ``VERIFIED``. The engine already stopped; triage only
  reports it. No rule, score or batch changes that.
- the per-item queue: saves a person looks at one at a time. Flagged saves
  carry the reason codes that failed. A random audit sample of clean saves
  is mixed in, with nothing that marks it as a sample.
- the batch: the remaining clean saves, which a person can approve with one
  action, or pull any of them back to the per-item queue first.

A plan is advice to a person. Nothing in Flow reads it to save, approve,
resume or certify: the run path doesn't import this package, and approving a
batch only writes review labels (:mod:`openadapt_flow.decision.review`).

Triage applies only to ``problems_only``. Under ``each_item`` and
``not_gated``, and whenever the policy is ``off``, every save goes to the
per-item queue in the order given, as before.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.decision.calibration import ArtifactMismatch, CalibrationArtifact
from openadapt_flow.decision.feature_model import FeatureModelProvider
from openadapt_flow.decision.features import SaveObservation, extract_features
from openadapt_flow.decision.labels import Bucket
from openadapt_flow.decision.provider import DecisionProvider, DecisionRequest
from openadapt_flow.decision.rules import (
    HARD_REASONS,
    REASON_COPY,
    RULE_V0_ID,
    RuleV0Config,
    rule_v0,
)

PLAN_SCHEMA: Literal["openadapt.decision-triage-plan/v1"] = (
    "openadapt.decision-triage-plan/v1"
)
TriagePolicy = Literal["off", "rule_v0", "learned"]
AppliedPolicy = Literal["off", "rule_v0", "learned", "per_item_fallback"]
_HEX64 = r"^[0-9a-f]{64}$"


class TriageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: ``rule_v0`` needs no labels. ``learned`` needs an eligible, pinned
    #: calibration artifact for the save's group.
    policy: TriagePolicy = "rule_v0"
    rule: RuleV0Config = Field(default_factory=RuleV0Config)
    #: Share of clean saves sent to the per-item queue anyway. Can't be off.
    audit_rate: float = Field(default=0.1, ge=0.01, le=1.0)
    #: What ``learned`` does when the group isn't calibrated. ``per_item``
    #: (the default) sends every save to a person. It stays the default on
    #: purpose: someone who chose ``learned`` asked for a calibrated miss
    #: rate, and an uncalibrated group has none. Falling back to ``rule_v0``
    #: would quietly batch saves under a rule nobody measured on this
    #: workflow. Pick ``rule_v0`` explicitly to accept that.
    learned_fallback: Literal["per_item", "rule_v0"] = "per_item"
    pinned_artifact_sha256: Optional[str] = Field(default=None, pattern=_HEX64)


class TriageItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    item_id: str
    group_key: str
    observation_sha256: str
    bucket: Bucket
    reasons: tuple[str, ...] = ()
    #: ``(field_or_param_name, code)`` for the reviewer. Local.
    field_flags: tuple[tuple[str, str], ...] = ()
    score: Optional[float] = None
    inclusion_probability: float = 1.0
    features: dict[str, Any]


class TriagePlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.decision-triage-plan/v1"] = PLAN_SCHEMA
    plan_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    mode: str
    policy_requested: TriagePolicy
    policy_applied: AppliedPolicy
    fallback_reason: Optional[str] = None
    rule_id: Optional[str] = None
    artifact_sha256: Optional[str] = None
    audit_rate: float
    created_at: str
    halted: tuple[TriageItem, ...] = ()
    per_item: tuple[TriageItem, ...] = ()
    batch: tuple[TriageItem, ...] = ()
    batch_sha256: Optional[str] = None
    plan_sha256: str = "0" * 64

    def compute_batch_digest(self) -> Optional[str]:
        if not self.batch:
            return None
        rows = sorted((i.item_id, i.observation_sha256) for i in self.batch)
        text = json.dumps([self.plan_id, rows], separators=(",", ":"))
        return hashlib.sha256(text.encode()).hexdigest()

    def compute_digest(self) -> str:
        payload = self.model_dump(mode="json", exclude={"plan_sha256"})
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode()).hexdigest()

    def intact(self) -> bool:
        return (
            self.plan_sha256 == self.compute_digest()
            and self.batch_sha256 == self.compute_batch_digest()
        )

    def find(self, item_id: str) -> tuple[str, Optional[TriageItem]]:
        for place in ("halted", "per_item", "batch"):
            for item in getattr(self, place):
                if item.item_id == item_id:
                    return place, item
        return "missing", None


def _unit(plan_id: str, item_id: str, salt: str) -> float:
    digest = hashlib.sha256(f"{salt}\0{plan_id}\0{item_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def build_plan(
    observations: Sequence[SaveObservation],
    *,
    mode: str,
    config: Optional[TriageConfig] = None,
    artifacts: Optional[dict[str, CalibrationArtifact]] = None,
    provider: Optional[DecisionProvider] = None,
    plan_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> TriagePlan:
    """Triage ``observations`` into a plan. See the module docstring.

    ``artifacts`` maps a group key to its calibration artifact (``learned``
    only). ``provider`` overrides the local feature model; the artifact's
    thresholds still apply.
    """

    cfg = config or TriageConfig()
    pid = plan_id or uuid.uuid4().hex
    applied: AppliedPolicy = cfg.policy
    fallback: Optional[str] = None
    if mode != "problems_only":
        applied, fallback = "off", f"mode_{mode}"
    halted: list[TriageItem] = []
    per_item: list[TriageItem] = []
    batch: list[TriageItem] = []
    used_artifacts: set[str] = set()
    group_fallbacks: set[str] = set()

    for obs in observations:
        feats = extract_features(obs, intake_floor=cfg.rule.intake_floor)
        common: dict[str, Any] = dict(
            item_id=obs.item_id,
            group_key=obs.group_key,
            observation_sha256=obs.digest(),
            field_flags=feats.flags,
            features=feats.values,
        )
        reasons = rule_v0(feats.values, cfg.rule)
        if obs.halted():
            halted.append(
                TriageItem(
                    bucket="halted",
                    reasons=tuple(r for r in reasons if r in HARD_REASONS)
                    or ("readback_not_confirmed",),
                    **common,
                )
            )
            continue
        if applied == "off":
            per_item.append(TriageItem(bucket="unscored", reasons=reasons, **common))
            continue

        item_policy: str = applied
        score: Optional[float] = None
        bucket: Bucket
        if applied == "learned":
            artifact = (artifacts or {}).get(obs.group_key)
            reason = _artifact_problem(artifact, cfg)
            if reason is not None:
                group_fallbacks.add(reason)
                item_policy = cfg.learned_fallback
            else:
                assert artifact is not None
                used_artifacts.add(artifact.artifact_sha256)
                scorer = provider or FeatureModelProvider(
                    artifact, pinned_sha256=artifact.artifact_sha256
                )
                answer = scorer.answer(
                    DecisionRequest(group_key=obs.group_key, features=feats.values)
                )
                hard = tuple(r for r in reasons if r in HARD_REASONS)
                score = answer.p_bad
                if hard:
                    bucket, reasons = "flagged", hard
                elif answer.p_bad is None:
                    bucket, reasons = "abstain", ("score_uncertain",)
                elif answer.p_bad >= artifact.t_high:
                    bucket, reasons = "flagged", ("score_flagged",)
                elif answer.p_bad >= artifact.t_low:
                    bucket, reasons = "abstain", ("score_uncertain",)
                else:
                    bucket, reasons = "clean", ()
        if item_policy == "per_item":
            per_item.append(TriageItem(bucket="unscored", reasons=reasons, **common))
            continue
        if item_policy == "rule_v0":
            bucket = "flagged" if reasons else "clean"
        if bucket == "clean":
            if _unit(pid, obs.item_id, "audit") < cfg.audit_rate:
                per_item.append(
                    TriageItem(
                        bucket="audit",
                        score=score,
                        inclusion_probability=cfg.audit_rate,
                        **common,
                    )
                )
            else:
                batch.append(TriageItem(bucket="clean", score=score, **common))
        else:
            per_item.append(
                TriageItem(bucket=bucket, reasons=reasons, score=score, **common)
            )

    if applied != "off":
        per_item = _order(per_item, pid)
        if applied == "learned" and group_fallbacks:
            fallback = ",".join(sorted(group_fallbacks))
            if not used_artifacts and cfg.learned_fallback == "per_item":
                applied = "per_item_fallback"
    plan = TriagePlan(
        plan_id=pid,
        mode=mode,
        policy_requested=cfg.policy,
        policy_applied=applied,
        fallback_reason=fallback,
        rule_id=RULE_V0_ID if applied in ("rule_v0", "learned") else None,
        artifact_sha256=next(iter(used_artifacts))
        if len(used_artifacts) == 1
        else None,
        audit_rate=cfg.audit_rate,
        created_at=(now or datetime.now(timezone.utc)).isoformat(),
        halted=tuple(halted),
        per_item=tuple(per_item),
        batch=tuple(batch),
    )
    plan = plan.model_copy(update={"batch_sha256": plan.compute_batch_digest()})
    return plan.model_copy(update={"plan_sha256": plan.compute_digest()})


def _artifact_problem(
    artifact: Optional[CalibrationArtifact], cfg: TriageConfig
) -> Optional[str]:
    if artifact is None:
        return "no_artifact"
    if cfg.pinned_artifact_sha256 is None:
        return "artifact_not_pinned"
    try:
        artifact.verify(cfg.pinned_artifact_sha256)
    except ArtifactMismatch:
        return "artifact_mismatch"
    if not artifact.eligible:
        return artifact.ineligible_reason or "uncalibrated"
    return None


def _order(items: list[TriageItem], plan_id: str) -> list[TriageItem]:
    """Saves with a hard reason first; the rest, audit included, shuffled."""

    def key(item: TriageItem) -> tuple[int, float]:
        hard = any(r in HARD_REASONS for r in item.reasons)
        return (0 if hard else 1, _unit(plan_id, item.item_id, "order"))

    return sorted(items, key=key)


def reviewer_view(plan: TriagePlan) -> dict[str, Any]:
    """What the review screen shows. No bucket, score or audit marker."""

    def row(item: TriageItem) -> dict[str, Any]:
        return {
            "item_id": item.item_id,
            "reasons": [
                {"code": code, "text": REASON_COPY.get(code, code)}
                for code in item.reasons
            ],
            "fields": [{"name": name, "code": code} for name, code in item.field_flags],
        }

    return {
        "schema": "openadapt.decision-review-view/v1",
        "plan_id": plan.plan_id,
        "plan_sha256": plan.plan_sha256,
        "stopped": [row(item) for item in plan.halted],
        "review_each": [row(item) for item in plan.per_item],
        "batch": {
            "title": f"Approve {len(plan.batch)} clean items" if plan.batch else None,
            "batch_sha256": plan.batch_sha256,
            "items": [item.item_id for item in plan.batch],
        },
    }


__all__ = [
    "PLAN_SCHEMA",
    "TriageConfig",
    "TriageItem",
    "TriagePlan",
    "TriagePolicy",
    "build_plan",
    "reviewer_view",
]

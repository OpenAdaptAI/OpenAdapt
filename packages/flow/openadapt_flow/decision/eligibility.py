"""Whether one workflow has earned "only ask a person about problems".

This is the exceptions-only eligibility check the approval modes ask for. It
mirrors ``openadapt_types.AutonomyEligibilityV1`` field for field, so Flow
keeps working without openadapt-types installed.

A workflow is eligible only with both kinds of evidence:

* a sealed, pinned calibration artifact for its save step that is itself
  eligible (enough person labels, enough bad ones, a threshold found); and
* the audit sample: clean saves a person reviewed anyway. Every decline
  there is a miss. The exact one-sided (Clopper-Pearson) upper bound on the
  miss rate, at confidence ``1 - delta``, must be under the target.

Missing evidence raises :class:`EligibilityUnavailable` with the reason. It
never answers "eligible" by default, and it never approves a save: it only
says whether a person may switch the workflow's mode.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Literal, Mapping, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator

from openadapt_flow.decision.calibration import (
    DEFAULT_DELTA,
    DEFAULT_TARGET_MISS,
    ArtifactMismatch,
    CalibrationArtifact,
    binom_cdf,
)
from openadapt_flow.decision.labels import ApprovalLabelV1

AUTONOMY_ELIGIBILITY_SCHEMA: Literal["openadapt.autonomy-eligibility/v1"] = (
    "openadapt.autonomy-eligibility/v1"
)
#: The smallest share of clean saves still sent to a person for audit.
MIN_AUDIT_FRACTION = 0.01
DEFAULT_AUDIT_FRACTION = 0.10
_OPAQUE_ID = r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$"
_SHA256 = r"^sha256:[0-9a-f]{64}$"


class EligibilityUnavailable(LookupError):
    """There isn't enough evidence to decide. The workflow stays per item."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AutonomyEligibilityV1(BaseModel):
    """Mirror of ``openadapt_types.AutonomyEligibilityV1``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.autonomy-eligibility/v1"] = (
        AUTONOMY_ELIGIBILITY_SCHEMA
    )
    workflow_id: StrictStr = Field(pattern=_OPAQUE_ID)
    calibration_artifact_sha256: StrictStr = Field(pattern=_SHA256)
    verified_runs: int = Field(ge=0)
    audited_items: int = Field(ge=0)
    audit_misses: int = Field(ge=0)
    audit_miss_rate_upper_bound: float = Field(ge=0.0, le=1.0)
    target_miss_rate: float = Field(gt=0.0, le=1.0)
    audit_fraction: float = Field(ge=MIN_AUDIT_FRACTION, le=1.0)
    evaluated_at: StrictStr = Field(
        pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
    )

    @model_validator(mode="after")
    def _counts(self) -> "AutonomyEligibilityV1":
        if self.audit_misses > self.audited_items:
            raise ValueError("audit_misses cannot exceed audited_items")
        return self

    @property
    def eligible(self) -> bool:
        return (
            self.audited_items > 0
            and self.audit_miss_rate_upper_bound < self.target_miss_rate
        )


def miss_rate_upper_bound(misses: int, audited: int, *, delta: float) -> float:
    """Exact one-sided upper confidence bound on a miss rate.

    The Clopper-Pearson bound: the largest ``p`` with
    ``P(X <= misses | audited, p) >= delta``. Found by bisection, so no
    scipy. Zero audited items bound nothing and return 1.0.
    """

    if not 0.0 < delta < 1.0:
        raise ValueError("delta must be between 0 and 1")
    if audited <= 0 or misses >= audited:
        return 1.0
    low, high = misses / audited, 1.0
    for _ in range(100):
        mid = (low + high) / 2.0
        if binom_cdf(misses, audited, mid) > delta:
            low = mid
        else:
            high = mid
    return high


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class TriageEligibilityProvider:
    """``AutonomyEligibilityProvider`` backed by decision triage.

    ``artifacts`` maps a workflow id to its pinned calibration artifact and
    the digest it must have. ``labels`` are the person labels from the
    review queue's label log; only the ``audit`` bucket counts.
    """

    def __init__(
        self,
        *,
        artifacts: Mapping[str, tuple[CalibrationArtifact, str]],
        labels: Sequence[ApprovalLabelV1],
        verified_runs: Optional[Mapping[str, int]] = None,
        target_miss_rate: float = DEFAULT_TARGET_MISS,
        delta: float = DEFAULT_DELTA,
        audit_fraction: float = DEFAULT_AUDIT_FRACTION,
        clock: Optional[object] = None,
    ) -> None:
        if audit_fraction < MIN_AUDIT_FRACTION:
            raise ValueError("at least 1% of clean saves must still be audited")
        self._artifacts = dict(artifacts)
        self._labels = list(labels)
        self._verified_runs = dict(verified_runs or {})
        self._target = target_miss_rate
        self._delta = delta
        self._audit_fraction = audit_fraction
        self._clock = clock or _now

    def eligibility(self, workflow_id: str) -> AutonomyEligibilityV1:
        if not re.match(_OPAQUE_ID, workflow_id or ""):
            raise EligibilityUnavailable("workflow_id_invalid", "not a workflow id")
        pinned = self._artifacts.get(workflow_id)
        if pinned is None:
            raise EligibilityUnavailable(
                "no_calibration_artifact",
                f"no calibration artifact is pinned for {workflow_id}; "
                "keep it per item until triage has been calibrated on it",
            )
        artifact, pinned_sha256 = pinned
        try:
            artifact.verify(pinned_sha256)
        except ArtifactMismatch as exc:
            raise EligibilityUnavailable(
                "calibration_artifact_mismatch", str(exc)
            ) from exc
        if not artifact.eligible:
            raise EligibilityUnavailable(
                "calibration_not_eligible",
                f"the calibration artifact is not eligible "
                f"({artifact.ineligible_reason or 'unknown'})",
            )
        audited = [
            label
            for label in self._labels
            if label.group_key == artifact.group_key and label.bucket == "audit"
        ]
        if not audited:
            raise EligibilityUnavailable(
                "no_audit_labels",
                "no person has reviewed an audited clean save of this workflow yet",
            )
        misses = sum(1 for label in audited if label.decision == "declined")
        bound = miss_rate_upper_bound(misses, len(audited), delta=self._delta)
        return AutonomyEligibilityV1(
            workflow_id=workflow_id,
            calibration_artifact_sha256=f"sha256:{artifact.artifact_sha256}",
            verified_runs=int(self._verified_runs.get(workflow_id, 0)),
            audited_items=len(audited),
            audit_misses=misses,
            audit_miss_rate_upper_bound=bound,
            target_miss_rate=self._target,
            audit_fraction=self._audit_fraction,
            evaluated_at=self._clock(),  # type: ignore[operator]
        )


__all__ = [
    "AUTONOMY_ELIGIBILITY_SCHEMA",
    "AutonomyEligibilityV1",
    "EligibilityUnavailable",
    "MIN_AUDIT_FRACTION",
    "TriageEligibilityProvider",
    "miss_rate_upper_bound",
]

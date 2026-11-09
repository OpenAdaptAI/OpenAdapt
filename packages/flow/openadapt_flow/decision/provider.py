"""The interface a triage score comes from.

A :class:`DecisionProvider` answers one closed question about one save
("does this save look clean?") from minimized features. It returns a
probability or abstains. It can't approve, decline, resume or certify
anything: :class:`Answer` has no field for that, and the code that writes
approvals (``approval_mode.item_approval``, ``runtime.durable``) doesn't
import this package.

The only provider in Flow today is the local
:class:`~openadapt_flow.decision.feature_model.FeatureModelProvider`. A local
language model or a hosted judge could implement the same protocol later;
neither exists yet, and ``phi_boundary`` stays ``minimized_features_only``
for any of them.
"""

from __future__ import annotations

import math
from typing import Any, Literal, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openadapt_flow.decision.minimize import assert_minimized

REQUEST_SCHEMA: Literal["openadapt.decision-request/v1"] = (
    "openadapt.decision-request/v1"
)
ANSWER_SCHEMA: Literal["openadapt.decision-answer/v1"] = "openadapt.decision-answer/v1"
#: The only boundary a request may declare: features, never values.
PHI_BOUNDARY: Literal["minimized_features_only"] = "minimized_features_only"
_GROUP_KEY = r"^[0-9a-f]{64}$"


class Question(BaseModel):
    """The closed question. One code, no free text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: Literal["save_looks_clean"] = "save_looks_clean"


class DecisionRequest(BaseModel):
    """What a provider is shown about one save."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.decision-request/v1"] = REQUEST_SCHEMA
    question: Question = Field(default_factory=Question)
    #: Opaque digest of the calibration group (workflow and save step).
    group_key: str = Field(pattern=_GROUP_KEY)
    features: dict[str, Any]
    phi_boundary: Literal["minimized_features_only"] = PHI_BOUNDARY

    @field_validator("features")
    @classmethod
    def _minimized(cls, value: dict[str, Any]) -> dict[str, Any]:
        return assert_minimized(value)


class Answer(BaseModel):
    """A provider's answer: a probability that the save is wrong, or none."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.decision-answer/v1"] = ANSWER_SCHEMA
    provider_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._:/-]{0,127}$")
    #: Calibrated probability that a person would decline this save as
    #: wrong values or wrong record. ``None`` when the provider abstains.
    p_bad: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    abstain: bool = False
    abstain_reason: Optional[
        Literal["uncalibrated", "group_mismatch", "artifact_mismatch"]
    ] = None
    #: Digest of the calibration artifact the score came from.
    artifact_sha256: Optional[str] = Field(default=None, pattern=_GROUP_KEY)

    @model_validator(mode="after")
    def _consistent(self) -> "Answer":
        if self.abstain != (self.p_bad is None):
            raise ValueError("an answer has a score or abstains, not both")
        if self.abstain != (self.abstain_reason is not None):
            raise ValueError("an abstention needs exactly one closed reason")
        if self.p_bad is not None and not math.isfinite(self.p_bad):
            raise ValueError("a score must be finite")
        return self


@runtime_checkable
class DecisionProvider(Protocol):
    """Anything that scores a :class:`DecisionRequest`."""

    provider_id: str

    def answer(self, request: DecisionRequest) -> Answer: ...


__all__ = [
    "ANSWER_SCHEMA",
    "PHI_BOUNDARY",
    "REQUEST_SCHEMA",
    "Answer",
    "DecisionProvider",
    "DecisionRequest",
    "Question",
]

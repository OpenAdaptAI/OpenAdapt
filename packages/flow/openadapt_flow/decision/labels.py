"""One record per human review decision, kept as training and audit labels.

Each record holds the decision, the closed decline reason, the minimized
features the item was triaged on, which queue it was in and how likely it
was to be sampled for a one-at-a-time review. No value, screenshot or
record key is stored.

The records are not
:class:`~openadapt_flow.judgment_cases.JudgmentCaseV1` themselves: a
judgment case binds one business-decision state of a compiled graph, and a
review decision belongs to a save, not a graph state.
:func:`to_judgment_case` converts a record when a caller wants it in the
case-set format, under a fixed ``approval-review`` state.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openadapt_flow.approval_mode.approver import (
    ApproverIdentityV1,
    person_identity,
)
from openadapt_flow.approval_mode.model import (
    OPAQUE_ID_PATTERN,
    ApproverAuthMethod,
    DeclineReason,
)
from openadapt_flow.decision.minimize import (
    FEATURE_NAMES,
    FEATURE_TYPES,
    assert_minimized,
)
from openadapt_flow.judgment_cases import (
    JudgmentCaseProvenanceV1,
    JudgmentCaseV1,
    JudgmentDecisionBindingV1,
    JudgmentDisposition,
    JudgmentFactFieldV1,
    JudgmentFactSchemaV1,
    JudgmentFactType,
    LocalEvidenceRefV1,
)

LABEL_SCHEMA: Literal["openadapt.decision-label/v1"] = "openadapt.decision-label/v1"
#: Where an item sat in the plan. ``unscored`` is every item when triage is
#: off or has fallen back to per-item review.
Bucket = Literal["halted", "flagged", "abstain", "audit", "clean", "unscored"]
BAD_REASONS = frozenset({"wrong_values", "wrong_record"})
_HEX64 = r"^[0-9a-f]{64}$"
_CODE = r"^[a-z][a-z0-9_]{0,63}$"


class ApprovalLabelV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.decision-label/v1"] = LABEL_SCHEMA
    label_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    plan_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    item_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    group_key: str = Field(pattern=_HEX64)
    observation_sha256: str = Field(pattern=_HEX64)
    decision: Literal["approved", "declined"]
    decline_reason: Optional[DeclineReason] = None
    bucket: Bucket
    route: Literal["review_item", "review_batch"]
    batch_sha256: Optional[str] = Field(default=None, pattern=_HEX64)
    #: Chance the item was reviewed one at a time. ``1`` except for audit
    #: items, where it is the audit rate.
    inclusion_probability: float = Field(gt=0.0, le=1.0)
    features: dict[str, Any]
    reasons: tuple[str, ...] = ()
    score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    rule_id: Optional[str] = Field(default=None, max_length=64)
    artifact_sha256: Optional[str] = Field(default=None, pattern=_HEX64)
    decided_by: Literal["human"] = "human"
    approver_kind: Literal["cloud_user", "unauthenticated"]
    approver_id: Optional[str] = Field(default=None, pattern=OPAQUE_ID_PATTERN)
    auth_method: ApproverAuthMethod
    recorded_at: str

    @field_validator("features")
    @classmethod
    def _minimized(cls, value: dict[str, Any]) -> dict[str, Any]:
        return assert_minimized(value)

    @field_validator("reasons")
    @classmethod
    def _codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        import re

        if any(re.fullmatch(_CODE, code) is None for code in value):
            raise ValueError("reasons are closed codes")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "ApprovalLabelV1":
        if (self.decision == "declined") != (self.decline_reason is not None):
            raise ValueError("a decline needs exactly one closed reason")
        if self.route == "review_batch":
            if self.bucket != "clean" or self.decision != "approved":
                raise ValueError("a batch approves clean items only")
            if self.batch_sha256 is None:
                raise ValueError("a batch label names the batch it came from")
        if self.bucket == "halted":
            raise ValueError("a halted save is not reviewed here")
        if self.approver_kind == "cloud_user":
            if self.approver_id is None or self.auth_method == "none":
                raise ValueError("a Cloud approver needs an id and a sign-in method")
        elif self.approver_id is not None or self.auth_method != "none":
            raise ValueError("an unauthenticated decision names no approver")
        return self

    def approver_identity(self) -> ApproverIdentityV1:
        """The person who decided, as ``openadapt.approver-identity/v1``.

        A label is always a person's decision: an agent, rule or system
        decision is recorded on the run report and never becomes a label.
        """

        return person_identity(self.approver_id, self.auth_method)

    def is_bad(self) -> bool:
        """Declined as wrong values or the wrong record."""

        return self.decline_reason in BAD_REASONS

    def digest(self) -> str:
        text = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(text.encode()).hexdigest()


class LabelLog:
    """An append-only JSON-lines file of :class:`ApprovalLabelV1` (0600)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def append(self, label: ApprovalLabelV1) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = (label.model_dump_json() + "\n").encode()
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.path, flags, 0o600)
        try:
            os.write(descriptor, line)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def read(self) -> list[ApprovalLabelV1]:
        if not self.path.exists():
            return []
        out = []
        for number, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if not line.strip():
                continue
            try:
                out.append(ApprovalLabelV1.model_validate_json(line))
            except ValueError as exc:
                raise ValueError(f"label log line {number} is malformed") from exc
        return out

    def decided(self, plan_id: str) -> set[str]:
        return {label.item_id for label in self.read() if label.plan_id == plan_id}


_FACT_TYPES = {
    bool: JudgmentFactType.BOOLEAN,
    int: JudgmentFactType.INTEGER,
    float: JudgmentFactType.NUMBER,
}
REVIEW_STATE_ID = "approval-review"


def label_fact_schema() -> JudgmentFactSchemaV1:
    fields = {
        name: JudgmentFactFieldV1(type=_FACT_TYPES[FEATURE_TYPES[name]])
        for name in FEATURE_NAMES
    }
    fields["review_outcome"] = JudgmentFactFieldV1(
        type=JudgmentFactType.ENUM,
        allowed_values=("approved", "wrong_values", "wrong_record", "screen_problem"),
    )
    return JudgmentFactSchemaV1(fields=fields)


def to_judgment_case(
    label: ApprovalLabelV1,
    *,
    workflow_contract_sha256: str,
    decision_contract_sha256: str,
    evidence: LocalEvidenceRefV1,
    reviewer_principal_ref_sha256: str,
) -> JudgmentCaseV1:
    """The label as a human-node judgment case. It authorizes nothing."""

    schema = label_fact_schema()
    facts: dict[str, bool | int | float | str] = dict(label.features)
    facts["review_outcome"] = label.decline_reason or "approved"
    return JudgmentCaseV1(
        id=f"review-{label.label_id}",
        decision=JudgmentDecisionBindingV1(
            graph_id="__program__",
            state_id=REVIEW_STATE_ID,
            workflow_contract_sha256=workflow_contract_sha256,
            decision_contract_sha256=decision_contract_sha256,
        ),
        fact_schema_sha256=schema.contract_sha256(),
        facts=facts,
        local_evidence=(evidence,),
        provenance=JudgmentCaseProvenanceV1(
            source="policy_review",
            source_ref_sha256=label.digest(),
            reviewer_role="item_reviewer",
            reviewer_principal_ref_sha256=reviewer_principal_ref_sha256,
        ),
        disposition=JudgmentDisposition.HUMAN_NODE,
    )


__all__ = [
    "BAD_REASONS",
    "LABEL_SCHEMA",
    "REVIEW_STATE_ID",
    "ApprovalLabelV1",
    "Bucket",
    "LabelLog",
    "label_fact_schema",
    "to_judgment_case",
]

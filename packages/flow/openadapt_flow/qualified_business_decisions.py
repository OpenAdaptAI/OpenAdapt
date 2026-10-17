"""Qualification-owned delivery bindings for typed business decisions.

The executable decision stays in the workflow graph.  This module stores only
the reviewed, static mobile presentation and the exact authenticated delivery
boundary.  It contains no signing key, runner token, live record value, OCR
text, or screenshot.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:  # pragma: no cover
    from openadapt_flow.ir import BusinessDecisionSpec, Workflow


_ID = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$"
_SHA256 = r"^sha256:[0-9a-f]{64}$"
_CONTRACT_SHA256 = r"^[0-9a-f]{64}$"
_STATIC_TEXT = re.compile(r"^[^{}\r\n]+$")


class QualifiedBusinessDecisionContextCard(BaseModel):
    """One reviewed static context item for a mobile decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    context_id: str = Field(pattern=_ID)
    kind: Literal[
        "policy",
        "precedent",
        "relationship",
        "capacity",
        "timing",
        "risk",
        "other_reviewed",
    ]
    label: str = Field(min_length=1, max_length=120)
    value: str = Field(min_length=1, max_length=500)

    @field_validator("label", "value")
    @classmethod
    def _static_text(cls, value: str) -> str:
        if value.strip() != value or _STATIC_TEXT.fullmatch(value) is None:
            raise ValueError("mobile decision copy must be static and trimmed")
        return value


class QualifiedBusinessDecisionOptionCopy(BaseModel):
    """Optional reviewed detail for one already-declared finite option."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    detail: Optional[str] = Field(default=None, min_length=1, max_length=500)
    consequence: Optional[str] = Field(default=None, min_length=1, max_length=500)

    @field_validator("detail", "consequence")
    @classmethod
    def _static_text(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and (
            value.strip() != value or _STATIC_TEXT.fullmatch(value) is None
        ):
            raise ValueError("mobile decision copy must be static and trimmed")
        return value


class QualifiedBusinessDecisionDelivery(BaseModel):
    """Reviewed mobile-delivery contract for one exact graph decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.qualified-business-decision-delivery/v1"] = (
        "openadapt.qualified-business-decision-delivery/v1"
    )
    graph_id: str = Field(
        pattern=r"^(?:__program__|[A-Za-z0-9][A-Za-z0-9._:-]{0,127})$"
    )
    state_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    decision_contract_sha256: str = Field(pattern=_CONTRACT_SHA256)
    decision_contract_revision: int = Field(default=1, ge=1)

    presentation_ref: str = Field(pattern=_ID)
    presentation_revision: int = Field(default=1, ge=1)
    egress_review_digest: str = Field(pattern=_SHA256)
    review_contract_digest: str = Field(pattern=_SHA256)
    category: Optional[str] = Field(default=None, min_length=1, max_length=120)
    title: Optional[str] = Field(default=None, min_length=1, max_length=120)
    role_label: Optional[str] = Field(default=None, min_length=1, max_length=120)
    why_judgment_needed: Optional[str] = Field(
        default=None, min_length=1, max_length=500
    )
    context_cards: tuple[QualifiedBusinessDecisionContextCard, ...] = Field(
        default=(), max_length=16
    )
    option_copy: dict[str, QualifiedBusinessDecisionOptionCopy] = Field(
        default_factory=dict
    )
    reason_codes: tuple[
        Literal[
            "institutional_knowledge_required",
            "policy_exception",
            "competing_priorities",
            "relationship_context",
            "capacity_constraint",
            "temporal_context",
            "risk_acceptance",
            "other_reviewed",
        ],
        ...,
    ] = Field(default=(), max_length=8)

    policy_ref: str = Field(pattern=_ID)
    policy_revision: int = Field(default=1, ge=1)
    role_refs: dict[str, str] = Field(min_length=1, max_length=16)
    authorized_route_refs: tuple[str, ...] = Field(min_length=1, max_length=16)
    registration_route_ref: str = Field(pattern=_ID)
    answer_issuer_key_id: str = Field(pattern=_ID)
    required_authn: Literal["local_enterprise_identity", "aal2", "webauthn"]
    relay_capability_digest: str = Field(pattern=_SHA256)
    qualification_issuer_key_id: str = Field(pattern=_ID)
    task_issuer_key_id: str = Field(pattern=_ID)
    receipt_issuer_key_id: str = Field(pattern=_ID)

    @field_validator("category", "title", "role_label", "why_judgment_needed")
    @classmethod
    def _static_optional_text(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and (
            value.strip() != value or _STATIC_TEXT.fullmatch(value) is None
        ):
            raise ValueError("mobile decision copy must be static and trimmed")
        return value

    @model_validator(mode="after")
    def _closed_sets(self) -> "QualifiedBusinessDecisionDelivery":
        if self.egress_review_digest != self.review_contract_digest:
            raise ValueError(
                "the egress review must name the exact presentation review"
            )
        if len(set(self.role_refs.values())) != len(self.role_refs):
            raise ValueError("each local role must map to one unique remote role")
        for values, label in (
            (self.authorized_route_refs, "route references"),
            (self.reason_codes, "reason codes"),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"business decision {label} must be unique")
        if self.registration_route_ref not in self.authorized_route_refs:
            raise ValueError("business decision registration route must be authorized")
        context_ids = tuple(card.context_id for card in self.context_cards)
        if len(set(context_ids)) != len(context_ids):
            raise ValueError("business decision context ids must be unique")
        return self


def business_decision_delivery_review_digest(
    decision: "BusinessDecisionSpec", binding: QualifiedBusinessDecisionDelivery
) -> str:
    """Bind one qualification review to every byte of remote presentation copy."""

    payload = {
        "schema_version": "openadapt.business-decision-presentation-review/v1",
        "graph_id": binding.graph_id,
        "state_id": binding.state_id,
        "decision_contract_sha256": binding.decision_contract_sha256,
        "decision_contract_revision": binding.decision_contract_revision,
        "question": decision.question,
        "options": [
            {"option_id": option.id, "label": option.label}
            for option in decision.options
        ],
        "presentation_ref": binding.presentation_ref,
        "presentation_revision": binding.presentation_revision,
        "category": binding.category,
        "title": binding.title,
        "role_label": binding.role_label,
        "why_judgment_needed": binding.why_judgment_needed,
        "context_cards": [
            card.model_dump(mode="json") for card in binding.context_cards
        ],
        "option_copy": {
            key: value.model_dump(mode="json")
            for key, value in sorted(binding.option_copy.items())
        },
        "reason_codes": list(binding.reason_codes),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def resolve_qualified_business_decision(
    workflow: "Workflow", binding: QualifiedBusinessDecisionDelivery
) -> "BusinessDecisionSpec":
    """Return the exact bound decision, or refuse a stale qualification."""

    from openadapt_flow.ir import StateKind

    graph = (
        workflow.program
        if binding.graph_id == "__program__"
        else workflow.subflows.get(binding.graph_id)
    )
    if graph is None:
        raise ValueError("qualified business decision graph does not exist")
    state = graph.states.get(binding.state_id)
    if (
        state is None
        or state.kind is not StateKind.BUSINESS_DECISION
        or state.decision is None
    ):
        raise ValueError("qualified business decision state does not exist")
    decision = state.decision
    if decision.contract_sha256() != binding.decision_contract_sha256:
        raise ValueError("qualified business decision contract changed")
    if set(binding.role_refs) != set(decision.authorized_roles):
        raise ValueError("qualified remote role mapping differs from the decision")
    option_ids = {option.id for option in decision.options}
    if not set(binding.option_copy).issubset(option_ids):
        raise ValueError("qualified option copy names an unknown decision option")
    if any(option.required_evidence for option in decision.options):
        raise ValueError(
            "a remotely answerable business decision cannot require local evidence"
        )
    expected_review = business_decision_delivery_review_digest(decision, binding)
    if binding.review_contract_digest != expected_review:
        raise ValueError(
            "the mobile presentation differs from its exact qualification review"
        )
    return decision


def qualified_business_decision_delivery_errors(
    workflow: "Workflow",
) -> list[tuple[str, str]]:
    """Return stable paths and messages for stale delivery bindings."""

    project = workflow.qualification
    if project is None:
        return []
    errors: list[tuple[str, str]] = []
    for index, binding in enumerate(project.business_decision_deliveries):
        try:
            resolve_qualified_business_decision(workflow, binding)
        except ValueError as exc:
            errors.append(
                (f"qualification.business_decision_deliveries.{index}", str(exc))
            )
    return errors


__all__ = [
    "QualifiedBusinessDecisionContextCard",
    "QualifiedBusinessDecisionDelivery",
    "QualifiedBusinessDecisionOptionCopy",
    "business_decision_delivery_review_digest",
    "qualified_business_decision_delivery_errors",
    "resolve_qualified_business_decision",
]

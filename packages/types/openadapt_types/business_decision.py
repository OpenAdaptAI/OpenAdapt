"""Portable, privacy-safe contracts for finite business decisions.

This family is separate from :mod:`openadapt_types.human_decision`.
``HumanDecisionTaskV2`` resolves an operational runtime halt.  A
``BusinessDecisionTaskV1`` presents one finite branch that was declared and
qualified before the run started.  It never grants Continue, Skip, Teach, or
Reconcile authority.

The portable task is a presentation and authentication projection.  It is not
execution authority.  The customer runner must authenticate the task, submit
the selected option through Flow's business-decision store, reacquire the live
application state, and pass the successor action's normal identity and effect
contracts before it actuates.

The task and answer envelopes contain no question text, option label,
screenshot, OCR output, record value, or live identifier.  A separate
presentation artifact carries reviewed static copy.  Each text field is either
local-only or bound to a positive egress review.  A remote projection refuses
local-only or unreviewed copy.  A decision that needs protected context is
``local_answer_required`` and cannot be answered by a remote client.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)

BUSINESS_DECISION_TASK_SCHEMA = "openadapt.business-decision-task/v1"
BUSINESS_DECISION_ANSWER_SCHEMA = "openadapt.business-decision-answer/v1"
BUSINESS_DECISION_PRESENTATION_SCHEMA = "openadapt.business-decision-presentation/v1"
BUSINESS_DECISION_DELIVERY_POLICY_SCHEMA = (
    "openadapt.business-decision-delivery-policy/v1"
)
BUSINESS_DECISION_ANSWER_RECEIPT_SCHEMA = "openadapt.business-decision-answer-receipt/v1"

_TASK_DOMAIN = b"openadapt.business-decision-task/v1\x00"
_ANSWER_DOMAIN = b"openadapt.business-decision-answer/v1\x00"
_DELIVERY_POLICY_DOMAIN = b"openadapt.business-decision-delivery-policy/v1\x00"
_ANSWER_RECEIPT_DOMAIN = b"openadapt.business-decision-answer-receipt/v1\x00"
_PRESENTATION_TEXT_DOMAIN = b"openadapt.business-decision-presentation-text/v1\x00"
_OPAQUE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$"
_OPTION_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,199}$"
_SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"
_SIGNATURE_PATTERN = r"^hmac-sha256:[0-9a-f]{64}$"
_ROLE_MAPPING_DIGEST_PATTERN = _SIGNATURE_PATTERN
_TIMESTAMP_PATTERN = (
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$"
)
_OpaqueRoleRef = Annotated[StrictStr, Field(pattern=_OPAQUE_ID_PATTERN)]
_STATIC_PRESENTATION_TEXT_PATTERN = r"^[^\x00-\x1f\x7f]{1,500}$"


class _StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BusinessDecisionDeliveryMode(str, Enum):
    """Whether the decision can leave the customer-controlled context."""

    REMOTE_ANSWERABLE = "remote_answerable"
    LOCAL_ANSWER_REQUIRED = "local_answer_required"


class BusinessDecisionRequiredAuthn(str, Enum):
    """The exact authentication profile required by the qualified contract."""

    LOCAL_ENTERPRISE_IDENTITY = "local_enterprise_identity"
    AAL2 = "aal2"
    WEBAUTHN = "webauthn"


class BusinessDecisionOptionBindingV1(_StrictContract):
    """One finite option and its opaque compiled-successor commitment."""

    option_id: StrictStr = Field(pattern=_OPTION_ID_PATTERN)
    target_binding_digest: StrictStr = Field(pattern=_SHA256_PATTERN)


class BusinessDecisionPresentationClassification(str, Enum):
    """The reviewed egress class for one static presentation field."""

    LOCAL_ONLY = "local_only"
    REVIEWED_REMOTE_SAFE = "reviewed_remote_safe"


class BusinessDecisionContextKind(str, Enum):
    """Closed kinds for reviewed institutional context."""

    POLICY = "policy"
    PRECEDENT = "precedent"
    RELATIONSHIP = "relationship"
    CAPACITY = "capacity"
    TIMING = "timing"
    RISK = "risk"
    OTHER_REVIEWED = "other_reviewed"


class BusinessDecisionJudgmentReason(str, Enum):
    """Finite reasons why the qualified workflow retains human authority."""

    INSTITUTIONAL_KNOWLEDGE_REQUIRED = "institutional_knowledge_required"
    POLICY_EXCEPTION = "policy_exception"
    COMPETING_PRIORITIES = "competing_priorities"
    RELATIONSHIP_CONTEXT = "relationship_context"
    CAPACITY_CONSTRAINT = "capacity_constraint"
    TEMPORAL_CONTEXT = "temporal_context"
    RISK_ACCEPTANCE = "risk_acceptance"
    OTHER_REVIEWED = "other_reviewed"


class BusinessDecisionPresentationTextV1(_StrictContract):
    """One static text field and its positive egress-review binding."""

    text: StrictStr = Field(
        min_length=1,
        max_length=500,
        pattern=_STATIC_PRESENTATION_TEXT_PATTERN,
    )
    classification: BusinessDecisionPresentationClassification
    egress_review_digest: StrictStr | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
    )

    @model_validator(mode="after")
    def _validate_egress_review(self) -> BusinessDecisionPresentationTextV1:
        if self.text.strip() != self.text:
            raise ValueError("business decision presentation text must be trimmed")
        if (
            self.classification
            is BusinessDecisionPresentationClassification.REVIEWED_REMOTE_SAFE
        ) != (self.egress_review_digest is not None):
            raise ValueError(
                "reviewed remote-safe text requires one egress review digest; "
                "local-only text cannot carry one"
            )
        return self

    @property
    def content_digest(self) -> str:
        """Return the canonical digest for the exact reviewed copy."""

        return business_decision_presentation_text_digest(self.text)


class BusinessDecisionPresentationOptionV1(_StrictContract):
    """One exact option label from the reviewed workflow contract."""

    option_id: StrictStr = Field(pattern=_OPTION_ID_PATTERN)
    label: BusinessDecisionPresentationTextV1
    detail: BusinessDecisionPresentationTextV1 | None = None
    consequence: BusinessDecisionPresentationTextV1 | None = None


class BusinessDecisionContextCardV1(_StrictContract):
    """One reviewed static context item; no runtime observation belongs here."""

    context_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    kind: BusinessDecisionContextKind
    label: BusinessDecisionPresentationTextV1
    value: BusinessDecisionPresentationTextV1


class BusinessDecisionPresentationV1(_StrictContract):
    """Reviewed presentation artifact for one business decision.

    This artifact is separate from the Cloud-safe task.  The signed delivery
    policy binds its exact digest and the positive egress review before a
    remote route can show it.
    """

    schema_version: Literal["openadapt.business-decision-presentation/v1"] = (
        BUSINESS_DECISION_PRESENTATION_SCHEMA
    )
    presentation_ref: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    presentation_revision: StrictInt = Field(ge=1, le=2_147_483_647)
    decision_contract_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    decision_contract_revision: StrictInt = Field(ge=1, le=2_147_483_647)
    category: BusinessDecisionPresentationTextV1 | None = None
    title: BusinessDecisionPresentationTextV1 | None = None
    role_label: BusinessDecisionPresentationTextV1 | None = None
    question: BusinessDecisionPresentationTextV1
    why_judgment_needed: BusinessDecisionPresentationTextV1 | None = None
    context_cards: tuple[BusinessDecisionContextCardV1, ...] = Field(
        default=(),
        max_length=16,
    )
    options: tuple[BusinessDecisionPresentationOptionV1, ...] = Field(
        min_length=2,
        max_length=32,
    )
    reason_codes: tuple[BusinessDecisionJudgmentReason, ...] = Field(
        default=(),
        max_length=8,
    )
    review_contract_digest: StrictStr = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _validate_presentation(self) -> BusinessDecisionPresentationV1:
        option_ids = tuple(option.option_id for option in self.options)
        labels = tuple(option.label.text for option in self.options)
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("business decision presentation option ids must be unique")
        if len({label.casefold() for label in labels}) != len(labels):
            raise ValueError("business decision presentation labels must be unique")
        if any(len(label) > 120 for label in labels):
            raise ValueError(
                "business decision presentation option labels exceed 120 characters"
            )
        context_ids = tuple(card.context_id for card in self.context_cards)
        if len(context_ids) != len(set(context_ids)):
            raise ValueError("business decision context ids must be unique")
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("business decision reason codes must be unique")
        return self

    def text_fields(self) -> tuple[BusinessDecisionPresentationTextV1, ...]:
        """Return every classified text leaf in deterministic field order."""

        fields = [
            field
            for field in (
                self.category,
                self.title,
                self.role_label,
                self.question,
                self.why_judgment_needed,
            )
            if field is not None
        ]
        for card in self.context_cards:
            fields.extend((card.label, card.value))
        for option in self.options:
            fields.append(option.label)
            if option.detail is not None:
                fields.append(option.detail)
            if option.consequence is not None:
                fields.append(option.consequence)
        return tuple(fields)

    @property
    def egress_review_digests(self) -> frozenset[str]:
        """Return all positive review bindings in this presentation."""

        values = [field.egress_review_digest for field in self.text_fields()]
        return frozenset(value for value in values if value is not None)

    @property
    def remote_safe(self) -> bool:
        """Return true only when every text field has a positive egress review."""

        return all(
            field.classification
            is BusinessDecisionPresentationClassification.REVIEWED_REMOTE_SAFE
            and field.egress_review_digest is not None
            for field in self.text_fields()
        )

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.model_dump(mode="json"))

    @property
    def digest(self) -> str:
        return _digest(self.canonical_bytes())


class BusinessDecisionDeliveryPolicyV1(_StrictContract):
    """Signed qualification policy for local or remote decision delivery."""

    schema_version: Literal[
        "openadapt.business-decision-delivery-policy/v1"
    ] = BUSINESS_DECISION_DELIVERY_POLICY_SCHEMA
    policy_ref: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    policy_revision: StrictInt = Field(ge=1, le=2_147_483_647)
    decision_contract_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    decision_contract_revision: StrictInt = Field(ge=1, le=2_147_483_647)
    presentation_ref: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    presentation_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    presentation_egress_review_digest: StrictStr | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
    )
    authorized_role_refs: tuple[_OpaqueRoleRef, ...] = Field(
        min_length=1, max_length=16
    )
    authorized_route_refs: tuple[_OpaqueRoleRef, ...] = Field(
        min_length=1, max_length=16
    )
    authorized_answer_issuer_key_ids: tuple[_OpaqueRoleRef, ...] = Field(
        min_length=1, max_length=16
    )
    role_mapping_digest: StrictStr = Field(pattern=_ROLE_MAPPING_DIGEST_PATTERN)
    required_authn: BusinessDecisionRequiredAuthn
    delivery_mode: BusinessDecisionDeliveryMode
    relay_capability_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    created_at: StrictStr = Field(
        min_length=20, max_length=40, pattern=_TIMESTAMP_PATTERN
    )
    expires_at: StrictStr = Field(
        min_length=20, max_length=40, pattern=_TIMESTAMP_PATTERN
    )
    issuer_key_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    signature_algorithm: Literal["hmac-sha256"] = "hmac-sha256"
    signature: StrictStr = Field(pattern=_SIGNATURE_PATTERN)

    @model_validator(mode="after")
    def _validate_policy(self) -> BusinessDecisionDeliveryPolicyV1:
        if len(self.authorized_role_refs) != len(set(self.authorized_role_refs)):
            raise ValueError("business decision policy role refs must be unique")
        if len(self.authorized_route_refs) != len(set(self.authorized_route_refs)):
            raise ValueError("business decision policy route refs must be unique")
        if len(self.authorized_answer_issuer_key_ids) != len(
            set(self.authorized_answer_issuer_key_ids)
        ):
            raise ValueError("business decision policy answer key ids must be unique")
        if _parse_timestamp(self.expires_at, "expires_at") <= _parse_timestamp(
            self.created_at, "created_at"
        ):
            raise ValueError("expires_at must be after created_at")
        if (
            self.delivery_mode is BusinessDecisionDeliveryMode.REMOTE_ANSWERABLE
        ) != (self.presentation_egress_review_digest is not None):
            raise ValueError(
                "remote delivery requires one presentation egress review digest; "
                "local-only delivery cannot carry one"
            )
        return self

    def unsigned_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"signature"})

    def canonical_unsigned_bytes(self) -> bytes:
        return _canonical_json(self.unsigned_payload())

    @property
    def digest(self) -> str:
        return _digest(self.canonical_unsigned_bytes())

    def verify_hmac(self, key: bytes) -> bool:
        return _verify_hmac(
            key,
            self.unsigned_payload(),
            self.signature,
            _DELIVERY_POLICY_DOMAIN,
        )


class BusinessDecisionTaskV1(_StrictContract):
    """A signed remote-safe projection of one exact business-decision pause."""

    schema_version: Literal["openadapt.business-decision-task/v1"] = (
        BUSINESS_DECISION_TASK_SCHEMA
    )
    task_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    task_revision: StrictInt = Field(default=1, ge=1, le=2_147_483_647)
    tenant_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    runner_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    run_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    pause_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    pause_binding_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    request_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    request_revision: StrictInt = Field(default=1, ge=1, le=2_147_483_647)
    request_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    supersedes_request_digest: StrictStr | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    bundle_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    workflow_contract_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    governed_runtime_inputs_digest: StrictStr | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    decision_contract_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    decision_contract_revision: StrictInt = Field(ge=1, le=2_147_483_647)
    delivery_policy_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    program_scope_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    control_frames_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    presentation_ref: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    presentation_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    options: tuple[BusinessDecisionOptionBindingV1, ...] = Field(
        min_length=2,
        max_length=32,
    )
    authorized_role_refs: tuple[_OpaqueRoleRef, ...] = Field(
        min_length=1, max_length=16
    )
    authorized_route_refs: tuple[_OpaqueRoleRef, ...] = Field(
        min_length=1, max_length=16
    )
    authorized_answer_issuer_key_ids: tuple[_OpaqueRoleRef, ...] = Field(
        min_length=1, max_length=16
    )
    role_mapping_digest: StrictStr = Field(pattern=_ROLE_MAPPING_DIGEST_PATTERN)
    required_authn: BusinessDecisionRequiredAuthn
    delivery_mode: BusinessDecisionDeliveryMode
    local_evidence_required: StrictBool
    required_evidence_count: StrictInt = Field(default=0, ge=0, le=64)
    relay_capability_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    idempotency_scope_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    created_at: StrictStr = Field(
        min_length=20, max_length=40, pattern=_TIMESTAMP_PATTERN
    )
    expires_at: StrictStr = Field(
        min_length=20, max_length=40, pattern=_TIMESTAMP_PATTERN
    )
    issuer_key_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    signature_algorithm: Literal["hmac-sha256"] = "hmac-sha256"
    signature: StrictStr = Field(pattern=_SIGNATURE_PATTERN)

    @model_validator(mode="after")
    def _validate_task(self) -> BusinessDecisionTaskV1:
        option_ids = tuple(option.option_id for option in self.options)
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("business decision option ids must be unique")
        if len(self.authorized_role_refs) != len(set(self.authorized_role_refs)):
            raise ValueError("business decision role refs must be unique")
        if len(self.authorized_route_refs) != len(set(self.authorized_route_refs)):
            raise ValueError("business decision route refs must be unique")
        if len(self.authorized_answer_issuer_key_ids) != len(
            set(self.authorized_answer_issuer_key_ids)
        ):
            raise ValueError("business decision answer key ids must be unique")
        created_at = _parse_timestamp(self.created_at, "created_at")
        expires_at = _parse_timestamp(self.expires_at, "expires_at")
        if expires_at <= created_at:
            raise ValueError("expires_at must be after created_at")
        if self.required_evidence_count > 0 and not self.local_evidence_required:
            raise ValueError(
                "required evidence count requires local_evidence_required=true"
            )
        if (
            self.delivery_mode is BusinessDecisionDeliveryMode.REMOTE_ANSWERABLE
            and self.local_evidence_required
        ):
            raise ValueError(
                "a remote-answerable decision cannot require protected local evidence"
            )
        if self.request_revision == 1 and self.supersedes_request_digest is not None:
            raise ValueError("the first request revision cannot supersede a request")
        if self.request_revision > 1 and self.supersedes_request_digest is None:
            raise ValueError("a renewed request must bind its predecessor")
        return self

    def unsigned_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"signature"})

    def canonical_unsigned_bytes(self) -> bytes:
        return _canonical_json(self.unsigned_payload())

    @property
    def digest(self) -> str:
        return _digest(self.canonical_unsigned_bytes())

    def verify_hmac(self, key: bytes) -> bool:
        return _verify_hmac(key, self.unsigned_payload(), self.signature, _TASK_DOMAIN)


class BusinessDecisionAnswerV1(_StrictContract):
    """An authenticated route's signed relay of one finite mobile answer.

    The client chooses only ``option_id`` and supplies one idempotency key.  The
    authenticated route, not the client, must populate the principal, role, and
    authentication-context references before it signs this contract.
    """

    schema_version: Literal["openadapt.business-decision-answer/v1"] = (
        BUSINESS_DECISION_ANSWER_SCHEMA
    )
    task_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    task_revision: StrictInt = Field(ge=1, le=2_147_483_647)
    task_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    request_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    option_id: StrictStr = Field(pattern=_OPTION_ID_PATTERN)
    idempotency_key: StrictStr = Field(pattern=_IDEMPOTENCY_KEY_PATTERN)
    authenticated_principal_ref: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    authenticated_role_ref: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    authn_assurance: BusinessDecisionRequiredAuthn
    authenticated_route_ref: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    role_mapping_digest: StrictStr = Field(pattern=_ROLE_MAPPING_DIGEST_PATTERN)
    authentication_context_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    answered_at: StrictStr = Field(
        min_length=20, max_length=40, pattern=_TIMESTAMP_PATTERN
    )
    issuer_key_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    signature_algorithm: Literal["hmac-sha256"] = "hmac-sha256"
    signature: StrictStr = Field(pattern=_SIGNATURE_PATTERN)

    @model_validator(mode="after")
    def _validate_answered_at(self) -> BusinessDecisionAnswerV1:
        _parse_timestamp(self.answered_at, "answered_at")
        return self

    def unsigned_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"signature"})

    def canonical_unsigned_bytes(self) -> bytes:
        return _canonical_json(self.unsigned_payload())

    @property
    def digest(self) -> str:
        return _digest(self.canonical_unsigned_bytes())

    def verify_hmac(self, key: bytes) -> bool:
        return _verify_hmac(
            key, self.unsigned_payload(), self.signature, _ANSWER_DOMAIN
        )


class BusinessDecisionAnswerReceiptState(str, Enum):
    """A runner result for one answer.  No member claims a verified effect."""

    ANSWER_RECORDED = "answer_recorded"
    REFUSED = "refused"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    DELIVERY_UNCERTAIN = "delivery_uncertain"


class BusinessDecisionAnswerReceiptReason(str, Enum):
    RECORDED_PENDING_REVALIDATION = "recorded_pending_revalidation"
    AUTHORIZATION_REFUSED = "authorization_refused"
    OPTION_REFUSED = "option_refused"
    EVIDENCE_REFUSED = "evidence_refused"
    REVALIDATION_REFUSED = "revalidation_refused"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    DELIVERY_UNCERTAIN = "delivery_uncertain"


BUSINESS_DECISION_ANSWER_RECEIPT_REASONS: Mapping[
    BusinessDecisionAnswerReceiptState, frozenset[BusinessDecisionAnswerReceiptReason]
] = {
    BusinessDecisionAnswerReceiptState.ANSWER_RECORDED: frozenset(
        {BusinessDecisionAnswerReceiptReason.RECORDED_PENDING_REVALIDATION}
    ),
    BusinessDecisionAnswerReceiptState.REFUSED: frozenset(
        {
            BusinessDecisionAnswerReceiptReason.AUTHORIZATION_REFUSED,
            BusinessDecisionAnswerReceiptReason.OPTION_REFUSED,
            BusinessDecisionAnswerReceiptReason.EVIDENCE_REFUSED,
            BusinessDecisionAnswerReceiptReason.REVALIDATION_REFUSED,
        }
    ),
    BusinessDecisionAnswerReceiptState.EXPIRED: frozenset(
        {BusinessDecisionAnswerReceiptReason.EXPIRED}
    ),
    BusinessDecisionAnswerReceiptState.SUPERSEDED: frozenset(
        {BusinessDecisionAnswerReceiptReason.SUPERSEDED}
    ),
    BusinessDecisionAnswerReceiptState.DELIVERY_UNCERTAIN: frozenset(
        {BusinessDecisionAnswerReceiptReason.DELIVERY_UNCERTAIN}
    ),
}


class BusinessDecisionAnswerReceiptV1(_StrictContract):
    """A signed runner result that reports answer handling, not business success."""

    schema_version: Literal["openadapt.business-decision-answer-receipt/v1"] = (
        BUSINESS_DECISION_ANSWER_RECEIPT_SCHEMA
    )
    task_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    task_revision: StrictInt = Field(ge=1, le=2_147_483_647)
    task_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    request_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    answer_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    option_id: StrictStr = Field(pattern=_OPTION_ID_PATTERN)
    state: BusinessDecisionAnswerReceiptState
    reason_code: BusinessDecisionAnswerReceiptReason
    runner_decision_receipt_digest: StrictStr | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    decided_at: StrictStr = Field(
        min_length=20, max_length=40, pattern=_TIMESTAMP_PATTERN
    )
    issuer_key_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    signature_algorithm: Literal["hmac-sha256"] = "hmac-sha256"
    signature: StrictStr = Field(pattern=_SIGNATURE_PATTERN)

    @model_validator(mode="after")
    def _validate_receipt(self) -> BusinessDecisionAnswerReceiptV1:
        if self.reason_code not in BUSINESS_DECISION_ANSWER_RECEIPT_REASONS[self.state]:
            raise ValueError("business decision receipt state and reason disagree")
        has_local_receipt = (
            self.state is BusinessDecisionAnswerReceiptState.ANSWER_RECORDED
            or self.reason_code is BusinessDecisionAnswerReceiptReason.REVALIDATION_REFUSED
        )
        if has_local_receipt:
            if self.runner_decision_receipt_digest is None:
                raise ValueError(
                    "an answer-recorded receipt requires the runner receipt digest"
                )
        elif self.runner_decision_receipt_digest is not None:
            raise ValueError(
                "a non-recorded result cannot claim a runner decision receipt"
            )
        _parse_timestamp(self.decided_at, "decided_at")
        return self

    @property
    def succeeded(self) -> Literal[False]:
        """A business-answer receipt never proves the workflow's business effect."""

        return False

    def unsigned_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"signature"})

    def canonical_unsigned_bytes(self) -> bytes:
        return _canonical_json(self.unsigned_payload())

    @property
    def digest(self) -> str:
        return _digest(self.canonical_unsigned_bytes())

    def verify_hmac(self, key: bytes) -> bool:
        return _verify_hmac(
            key, self.unsigned_payload(), self.signature, _ANSWER_RECEIPT_DOMAIN
        )


def validate_business_decision_answer(
    task: BusinessDecisionTaskV1,
    answer: BusinessDecisionAnswerV1,
    *,
    task_signing_key: bytes,
    answer_signing_key: bytes,
    at: str,
) -> None:
    """Fail unless an authenticated answer matches one active remote task.

    This check does not authorize execution.  It is the portable admission
    check before the customer runner calls Flow's authoritative decision API.
    """

    if not task.verify_hmac(task_signing_key):
        raise ValueError("the business decision task signature is invalid")
    if not answer.verify_hmac(answer_signing_key):
        raise ValueError("the business decision answer signature is invalid")
    at_time = _parse_timestamp(at, "at")
    if task.delivery_mode is not BusinessDecisionDeliveryMode.REMOTE_ANSWERABLE:
        raise ValueError("this business decision requires a local answer")
    created_at = _parse_timestamp(task.created_at, "created_at")
    expires_at = _parse_timestamp(task.expires_at, "expires_at")
    if at_time < created_at:
        raise ValueError("the business decision task is not active yet")
    if at_time >= expires_at:
        raise ValueError("the business decision task expired")
    expected = {
        "task_id": (answer.task_id, task.task_id),
        "task_revision": (answer.task_revision, task.task_revision),
        "task_digest": (answer.task_digest, task.digest),
        "request_digest": (answer.request_digest, task.request_digest),
        "role_mapping_digest": (
            answer.role_mapping_digest,
            task.role_mapping_digest,
        ),
        "authn_assurance": (answer.authn_assurance, task.required_authn),
    }
    for name, (actual, required) in expected.items():
        if actual != required:
            raise ValueError(f"business decision {name} does not match the task")
    if answer.authenticated_role_ref not in set(task.authorized_role_refs):
        raise ValueError("the authenticated role is not authorized for the decision")
    if answer.authenticated_route_ref not in set(task.authorized_route_refs):
        raise ValueError("the authenticated route is not authorized for the decision")
    if answer.issuer_key_id not in set(task.authorized_answer_issuer_key_ids):
        raise ValueError("the answer signing key is not authorized for the decision")
    if answer.option_id not in {option.option_id for option in task.options}:
        raise ValueError("the business decision option is not permitted")
    answered_at = _parse_timestamp(answer.answered_at, "answered_at")
    if answered_at < created_at:
        raise ValueError("the business decision answer predates the task")
    if answered_at >= expires_at:
        raise ValueError("the business decision answer was created after expiry")
    if at_time < answered_at:
        raise ValueError("the business decision answer is from the future")


def validate_business_decision_delivery(
    task: BusinessDecisionTaskV1,
    presentation: BusinessDecisionPresentationV1,
    policy: BusinessDecisionDeliveryPolicyV1,
    *,
    task_signing_key: bytes,
    qualification_signing_key: bytes,
    at: str,
) -> None:
    """Authenticate one task and its exact reviewed presentation manifest.

    This check authenticates only the portable delivery bindings.  A remote
    consumer can render only copy that has the exact positive egress-review
    binding in the signed policy.  This check does not authorize execution and
    does not prove a business effect.
    """

    if not task.verify_hmac(task_signing_key):
        raise ValueError("the business decision task signature is invalid")
    if not policy.verify_hmac(qualification_signing_key):
        raise ValueError("the business decision delivery policy signature is invalid")

    at_time = _parse_timestamp(at, "at")
    policy_created_at = _parse_timestamp(policy.created_at, "policy.created_at")
    policy_expires_at = _parse_timestamp(policy.expires_at, "policy.expires_at")
    task_created_at = _parse_timestamp(task.created_at, "task.created_at")
    task_expires_at = _parse_timestamp(task.expires_at, "task.expires_at")
    if at_time < policy_created_at or at_time >= policy_expires_at:
        raise ValueError("the business decision delivery policy is not active")
    if at_time < task_created_at or at_time >= task_expires_at:
        raise ValueError("the business decision task is not active")
    if task_created_at < policy_created_at or task_expires_at > policy_expires_at:
        raise ValueError("the business decision task exceeds the delivery policy")
    if policy.delivery_mode is BusinessDecisionDeliveryMode.REMOTE_ANSWERABLE:
        if not presentation.remote_safe:
            raise ValueError(
                "remote business decision presentation contains local-only text"
            )
        if presentation.egress_review_digests != {
            policy.presentation_egress_review_digest
        }:
            raise ValueError(
                "remote business decision presentation review binding does not match"
            )

    expected = {
        "delivery_policy_digest": (task.delivery_policy_digest, policy.digest),
        "presentation_ref": (task.presentation_ref, presentation.presentation_ref),
        "presentation_digest": (task.presentation_digest, presentation.digest),
        "policy.presentation_ref": (
            policy.presentation_ref,
            presentation.presentation_ref,
        ),
        "policy.presentation_digest": (
            policy.presentation_digest,
            presentation.digest,
        ),
        "decision_contract_digest": (
            task.decision_contract_digest,
            presentation.decision_contract_digest,
        ),
        "decision_contract_revision": (
            task.decision_contract_revision,
            presentation.decision_contract_revision,
        ),
        "policy.decision_contract_digest": (
            policy.decision_contract_digest,
            presentation.decision_contract_digest,
        ),
        "policy.decision_contract_revision": (
            policy.decision_contract_revision,
            presentation.decision_contract_revision,
        ),
        "authorized_role_refs": (
            task.authorized_role_refs,
            policy.authorized_role_refs,
        ),
        "authorized_route_refs": (
            task.authorized_route_refs,
            policy.authorized_route_refs,
        ),
        "authorized_answer_issuer_key_ids": (
            task.authorized_answer_issuer_key_ids,
            policy.authorized_answer_issuer_key_ids,
        ),
        "role_mapping_digest": (
            task.role_mapping_digest,
            policy.role_mapping_digest,
        ),
        "required_authn": (task.required_authn, policy.required_authn),
        "delivery_mode": (task.delivery_mode, policy.delivery_mode),
        "relay_capability_digest": (
            task.relay_capability_digest,
            policy.relay_capability_digest,
        ),
    }
    for name, (actual, required) in expected.items():
        if actual != required:
            raise ValueError(f"business decision {name} does not match")

    task_option_ids = tuple(option.option_id for option in task.options)
    presentation_option_ids = tuple(option.option_id for option in presentation.options)
    if task_option_ids != presentation_option_ids:
        raise ValueError("business decision presentation options do not match the task")


def business_decision_presentation_text_digest(text: str) -> str:
    """Return a domain-separated digest for exact static presentation copy."""

    if not isinstance(text, str):
        raise TypeError("business decision presentation text must be a string")
    if not 1 <= len(text) <= 500 or text.strip() != text:
        raise ValueError(
            "business decision presentation text must be trimmed and 1-500 characters"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in text):
        raise ValueError(
            "business decision presentation text contains a control character"
        )
    return "sha256:" + hashlib.sha256(
        _PRESENTATION_TEXT_DOMAIN + text.encode("utf-8")
    ).hexdigest()


def sign_business_decision_task_hmac(
    *, key: bytes, fields: Mapping[str, Any]
) -> BusinessDecisionTaskV1:
    return _sign(BusinessDecisionTaskV1, key, fields, _TASK_DOMAIN)


def sign_business_decision_answer_hmac(
    *, key: bytes, fields: Mapping[str, Any]
) -> BusinessDecisionAnswerV1:
    return _sign(BusinessDecisionAnswerV1, key, fields, _ANSWER_DOMAIN)


def sign_business_decision_delivery_policy_hmac(
    *, key: bytes, fields: Mapping[str, Any]
) -> BusinessDecisionDeliveryPolicyV1:
    return _sign(
        BusinessDecisionDeliveryPolicyV1,
        key,
        fields,
        _DELIVERY_POLICY_DOMAIN,
    )


def sign_business_decision_answer_receipt_hmac(
    *, key: bytes, fields: Mapping[str, Any]
) -> BusinessDecisionAnswerReceiptV1:
    return _sign(BusinessDecisionAnswerReceiptV1, key, fields, _ANSWER_RECEIPT_DOMAIN)


def _sign(
    model: type[
        BusinessDecisionTaskV1
        | BusinessDecisionAnswerV1
        | BusinessDecisionDeliveryPolicyV1
        | BusinessDecisionAnswerReceiptV1
    ],
    key: bytes,
    fields: Mapping[str, Any],
    domain: bytes,
) -> (
    BusinessDecisionTaskV1
    | BusinessDecisionAnswerV1
    | BusinessDecisionDeliveryPolicyV1
    | BusinessDecisionAnswerReceiptV1
):
    _validate_hmac_key(key)
    if "signature" in fields:
        raise ValueError("fields must not contain a signature")
    validated = model.model_validate(
        {
            **dict(fields),
            "signature_algorithm": "hmac-sha256",
            "signature": "hmac-sha256:" + "0" * 64,
        }
    )
    signature = _hmac_signature(key, validated.unsigned_payload(), domain)
    return validated.model_copy(update={"signature": signature})


def _validate_hmac_key(key: bytes) -> None:
    if not isinstance(key, bytes) or len(key) < 32:
        raise ValueError("business decision HMAC key must contain at least 32 bytes")


def _parse_timestamp(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _hmac_signature(key: bytes, payload: Mapping[str, Any], domain: bytes) -> str:
    return (
        "hmac-sha256:"
        + hmac.new(key, domain + _canonical_json(payload), hashlib.sha256).hexdigest()
    )


def _verify_hmac(
    key: bytes,
    payload: Mapping[str, Any],
    signature: str,
    domain: bytes,
) -> bool:
    _validate_hmac_key(key)
    return hmac.compare_digest(signature, _hmac_signature(key, payload, domain))

"""Versioned public contracts for asynchronous OpenAdapt Execute requests.

This module defines the portable trust boundary.  It does not implement
tenanting, permit issuance, dispatch, webhook delivery, or any application
connector.  Those production systems stay outside this MIT package.

The contract intentionally separates a lifecycle state from a terminal
outcome.  A run can wait for reconciliation without pretending that
reconciliation is already an outcome.  A completed compensation is reported
as ``rolled_back_verified`` only after an independent effect check proves it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from enum import Enum
from typing import Any, Literal, Mapping, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)

from openadapt_types.human_decision import HumanDecisionTaskV1

EXECUTE_REQUEST_SCHEMA = "openadapt.execute-request/v1"
EXECUTE_ACCEPTED_SCHEMA = "openadapt.execute-accepted/v1"
EXECUTE_STATUS_SCHEMA = "openadapt.execute-status/v1"
EXECUTE_EVIDENCE_RECEIPT_SCHEMA = "openadapt.execute-evidence-receipt/v1"
EXECUTE_WEBHOOK_SCHEMA = "openadapt.execute-webhook/v1"
EXECUTE_OPENAPI_SCHEMA = "openadapt.execute-openapi/v1"

_OPAQUE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$"
_SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"
_SIGNATURE_PATTERN = r"^hmac-sha256:[0-9a-f]{64}$"
_TIMESTAMP_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$"
_WEBHOOK_SIGNING_DOMAIN = b"openadapt.execute-webhook/v1\x00"


class _StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EffectStrengthV1(str, Enum):
    """The required strength of proof for a consequential effect.

    The members are named so a future protocol version can add a strength
    without changing the meaning of a numeric value.
    """

    INDEPENDENT_SYSTEM_OF_RECORD = "independent_system_of_record"
    INDEPENDENT_SESSION = "independent_session"
    PERSISTED_STATE_REACQUISITION = "persisted_state_reacquisition"
    IMMEDIATE_SCREEN_CONFIRMATION = "immediate_screen_confirmation"


_EFFECT_STRENGTH_RANK = {
    EffectStrengthV1.IMMEDIATE_SCREEN_CONFIRMATION: 1,
    EffectStrengthV1.PERSISTED_STATE_REACQUISITION: 2,
    EffectStrengthV1.INDEPENDENT_SESSION: 3,
    EffectStrengthV1.INDEPENDENT_SYSTEM_OF_RECORD: 4,
}


class ExecuteLifecycleStateV1(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DECISION_REQUIRED = "decision_required"
    WAITING_FOR_RECONCILIATION = "waiting_for_reconciliation"
    TERMINAL = "terminal"


class ExecuteTerminalOutcomeV1(str, Enum):
    VERIFIED = "verified"
    HALTED_BEFORE_EFFECT = "halted_before_effect"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    REJECTED_POLICY = "rejected_policy"
    FAILED_PLATFORM = "failed_platform"
    ROLLED_BACK_VERIFIED = "rolled_back_verified"


class ExecuteWebhookEventTypeV1(str, Enum):
    EXECUTION_STATE_CHANGED = "execution.state_changed"
    EXECUTION_TERMINAL = "execution.terminal"
    DECISION_REQUIRED = "execution.decision_required"


class ExecuteAuthorizationContextV1(_StrictContract):
    """Opaque authorization references from the caller's own system."""

    actor_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    authorization_reference: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)


class ExecuteRequestV1(_StrictContract):
    schema_version: Literal[EXECUTE_REQUEST_SCHEMA] = EXECUTE_REQUEST_SCHEMA
    qualification_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    workflow_version: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    workflow_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    environment_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    idempotency_key: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    authorization_context: ExecuteAuthorizationContextV1
    effect_strength_schema_version: Literal["1"] = "1"
    minimum_effect_strength: EffectStrengthV1


class ExecuteAcceptedV1(_StrictContract):
    schema_version: Literal[EXECUTE_ACCEPTED_SCHEMA] = EXECUTE_ACCEPTED_SCHEMA
    execution_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    state: Literal[ExecuteLifecycleStateV1.QUEUED] = ExecuteLifecycleStateV1.QUEUED


class ExecuteStatusV1(_StrictContract):
    """The current public lifecycle view of an execution resource."""

    schema_version: Literal[EXECUTE_STATUS_SCHEMA] = EXECUTE_STATUS_SCHEMA
    execution_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    state: ExecuteLifecycleStateV1
    terminal_outcome: ExecuteTerminalOutcomeV1 | None = None
    evidence_receipt_id: StrictStr | None = Field(default=None, pattern=_OPAQUE_ID_PATTERN)
    updated_at: StrictStr = Field(pattern=_TIMESTAMP_PATTERN)

    @model_validator(mode="after")
    def _validate_lifecycle(self) -> "ExecuteStatusV1":
        if self.state is ExecuteLifecycleStateV1.TERMINAL:
            if self.terminal_outcome is None or self.evidence_receipt_id is None:
                raise ValueError("a terminal state requires outcome and evidence_receipt_id")
        elif self.terminal_outcome is not None or self.evidence_receipt_id is not None:
            raise ValueError("only a terminal state can carry outcome or evidence_receipt_id")
        return self


class ExecuteEvidenceContractV1(_StrictContract):
    """Boolean contract results. Evidence bytes remain in the declared boundary."""

    authorization_passed: StrictBool
    identity_passed: StrictBool
    postcondition_passed: StrictBool
    effect_passed: StrictBool
    minimum_effect_strength: EffectStrengthV1
    observed_effect_strength: EffectStrengthV1 | None = None
    model_used: StrictBool
    external_network_used: StrictBool


class ExecuteEvidenceReceiptV1(_StrictContract):
    """A portable receipt that identifies evidence without carrying it."""

    schema_version: Literal[EXECUTE_EVIDENCE_RECEIPT_SCHEMA] = EXECUTE_EVIDENCE_RECEIPT_SCHEMA
    receipt_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    execution_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    workflow_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    outcome: ExecuteTerminalOutcomeV1
    contracts: ExecuteEvidenceContractV1
    delivery_uncertain: StrictBool
    compensation_effect_verified: StrictBool = False
    evidence_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    issued_at: StrictStr = Field(pattern=_TIMESTAMP_PATTERN)

    @model_validator(mode="after")
    def _validate_proof(self) -> "ExecuteEvidenceReceiptV1":
        verified = self.outcome in {
            ExecuteTerminalOutcomeV1.VERIFIED,
            ExecuteTerminalOutcomeV1.ROLLED_BACK_VERIFIED,
        }
        if verified:
            if not all(
                (
                    self.contracts.authorization_passed,
                    self.contracts.identity_passed,
                    self.contracts.postcondition_passed,
                    self.contracts.effect_passed,
                )
            ):
                raise ValueError("a verified outcome requires every required contract")
            if self.contracts.observed_effect_strength is None:
                raise ValueError("a verified outcome requires observed_effect_strength")
            if (
                _EFFECT_STRENGTH_RANK[self.contracts.observed_effect_strength]
                < _EFFECT_STRENGTH_RANK[self.contracts.minimum_effect_strength]
            ):
                raise ValueError("observed effect strength is below the required strength")
        if self.outcome is ExecuteTerminalOutcomeV1.ROLLED_BACK_VERIFIED:
            if not self.compensation_effect_verified:
                raise ValueError("rolled_back_verified requires independently verified compensation")
        elif self.compensation_effect_verified:
            raise ValueError("only rolled_back_verified can claim compensation_effect_verified")
        if self.outcome is ExecuteTerminalOutcomeV1.RECONCILIATION_REQUIRED:
            if not self.delivery_uncertain:
                raise ValueError("reconciliation_required requires an uncertain effect or delivery")
        return self


class _SignedExecuteWebhookV1(_StrictContract):
    schema_version: Literal[EXECUTE_WEBHOOK_SCHEMA] = EXECUTE_WEBHOOK_SCHEMA
    event_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    delivery_attempt: StrictInt = Field(ge=1, le=100)
    issued_at: StrictStr = Field(pattern=_TIMESTAMP_PATTERN)
    issuer_key_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    signature: StrictStr = Field(pattern=_SIGNATURE_PATTERN)

    def unsigned_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"signature"})

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.unsigned_payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_bytes()).hexdigest()

    def verify_hmac(self, key: bytes) -> bool:
        expected = "hmac-sha256:" + hmac.new(
            key,
            _WEBHOOK_SIGNING_DOMAIN + self.canonical_bytes(),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(self.signature, expected)


class ExecuteStateChangedWebhookV1(_SignedExecuteWebhookV1):
    event_type: Literal[ExecuteWebhookEventTypeV1.EXECUTION_STATE_CHANGED] = (
        ExecuteWebhookEventTypeV1.EXECUTION_STATE_CHANGED
    )
    execution: ExecuteStatusV1


class ExecuteTerminalWebhookV1(_SignedExecuteWebhookV1):
    event_type: Literal[ExecuteWebhookEventTypeV1.EXECUTION_TERMINAL] = (
        ExecuteWebhookEventTypeV1.EXECUTION_TERMINAL
    )
    receipt: ExecuteEvidenceReceiptV1


class ExecuteDecisionRequiredWebhookV1(_SignedExecuteWebhookV1):
    event_type: Literal[ExecuteWebhookEventTypeV1.DECISION_REQUIRED] = (
        ExecuteWebhookEventTypeV1.DECISION_REQUIRED
    )
    execution_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    decision: HumanDecisionTaskV1


ExecuteWebhookV1: TypeAlias = (
    ExecuteStateChangedWebhookV1
    | ExecuteTerminalWebhookV1
    | ExecuteDecisionRequiredWebhookV1
)


def sign_execute_webhook_hmac(
    *, key: bytes, fields: Mapping[str, Any]
) -> ExecuteWebhookV1:
    """Build one signed webhook envelope from a closed event type."""

    event_type = fields.get("event_type")
    model_by_event = {
        ExecuteWebhookEventTypeV1.EXECUTION_STATE_CHANGED: ExecuteStateChangedWebhookV1,
        ExecuteWebhookEventTypeV1.EXECUTION_TERMINAL: ExecuteTerminalWebhookV1,
        ExecuteWebhookEventTypeV1.DECISION_REQUIRED: ExecuteDecisionRequiredWebhookV1,
    }
    try:
        model = model_by_event[ExecuteWebhookEventTypeV1(event_type)]
    except (KeyError, ValueError) as exc:
        raise ValueError("event_type must select a supported Execute webhook") from exc
    unsigned = model.model_validate({**fields, "signature": "hmac-sha256:" + "0" * 64})
    signature = "hmac-sha256:" + hmac.new(
        key,
        _WEBHOOK_SIGNING_DOMAIN + unsigned.canonical_bytes(),
        hashlib.sha256,
    ).hexdigest()
    return model.model_validate({**fields, "signature": signature})

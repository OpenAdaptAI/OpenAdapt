"""Privacy-safe, signed tasks for attended human decisions.

The task is a portable projection of an existing runtime pause. It is not an
execution capability: a consumer must still present the runtime's separately
issued capability, satisfy its authentication policy, and pass fresh
revalidation before actuation. Free-form text, screenshots, identifiers, and
observed values deliberately do not belong in this contract.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime
from enum import Enum
from typing import Any, Literal, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)

HUMAN_DECISION_TASK_SCHEMA = "openadapt.human-decision-task/v1"
_SIGNING_DOMAIN = b"openadapt.human-decision-task/v1\x00"
_OPAQUE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$"
_SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"
_SIGNATURE_PATTERN = r"^hmac-sha256:[0-9a-f]{64}$"


class _StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HumanDecisionTaskKind(str, Enum):
    IDENTITY = "identity"
    EFFECT = "effect"
    AMBIGUITY = "ambiguity"
    HUMAN_STEP = "human_step"
    DELIVERY_UNCERTAIN = "delivery_uncertain"
    HALT = "halt"
    OPERATOR_REVIEW = "operator_review"


class HumanDecisionQuestionTemplate(str, Enum):
    CONFIRM_IDENTITY = "confirm_identity"
    CONFIRM_PERSISTED_EFFECT = "confirm_persisted_effect"
    RESOLVE_AMBIGUITY = "resolve_ambiguity"
    COMPLETE_HUMAN_STEP = "complete_human_step"
    REVIEW_UNCERTAIN_DELIVERY = "review_uncertain_delivery"
    REVIEW_HALT = "review_halt"


class HumanDecisionAction(str, Enum):
    """Portable action names; the runtime remains authoritative.

    ``verify_and_resume`` maps to Flow's attended ``continue`` operation. It
    means that the operator prepared the live state for fresh revalidation; it
    never authorizes blind repetition of a prior action.
    """

    VERIFY_AND_RESUME = "verify_and_resume"
    SKIP = "skip"
    TEACH = "teach"
    ESCALATE = "escalate"


class HumanDecisionDeliveryState(str, Enum):
    NOT_DELIVERED = "not_delivered"
    DELIVERED = "delivered"
    UNKNOWN = "unknown"


class HumanDecisionRiskClass(str, Enum):
    READ_ONLY = "read_only"
    STATE_CHANGING = "state_changing"
    CONSEQUENTIAL = "consequential"
    IRREVERSIBLE = "irreversible"
    UNKNOWN = "unknown"


class HumanDecisionSubstrate(str, Enum):
    BROWSER = "browser"
    WINDOWS = "windows"
    MACOS = "macos"
    LINUX = "linux"
    RDP = "rdp"
    CITRIX = "citrix"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class HumanDecisionRequiredAuthn(str, Enum):
    LOCAL_SESSION = "local_session"
    AAL2 = "aal2"
    WEBAUTHN = "webauthn"


class HumanDecisionSafeSlotsV1(_StrictContract):
    """Bounded numeric context safe to relay outside the evidence boundary."""

    candidate_count: StrictInt | None = Field(default=None, ge=0, le=1000)
    required_signal_count: StrictInt | None = Field(default=None, ge=0, le=1000)
    confirmed_signal_count: StrictInt | None = Field(default=None, ge=0, le=1000)

    @model_validator(mode="after")
    def _validate_counts(self) -> "HumanDecisionSafeSlotsV1":
        if (
            self.required_signal_count is not None
            and self.confirmed_signal_count is not None
            and self.confirmed_signal_count > self.required_signal_count
        ):
            raise ValueError("confirmed_signal_count cannot exceed required_signal_count")
        return self


class HumanDecisionQuestionV1(_StrictContract):
    template: HumanDecisionQuestionTemplate
    safe_slots: HumanDecisionSafeSlotsV1 = Field(
        default_factory=HumanDecisionSafeSlotsV1
    )


class HumanDecisionEvidenceSummaryV1(_StrictContract):
    identity_required_count: StrictInt | None = Field(default=None, ge=0, le=1000)
    identity_confirmed_count: StrictInt | None = Field(default=None, ge=0, le=1000)
    effect_required_count: StrictInt | None = Field(default=None, ge=0, le=1000)
    effect_confirmed_count: StrictInt | None = Field(default=None, ge=0, le=1000)
    minimum_effect_tier: StrictInt | None = Field(default=None, ge=1, le=4)
    observed_effect_tier: StrictInt | None = Field(default=None, ge=1, le=4)
    frame_available_locally: StrictBool = False
    sensitive_evidence_local_only: Literal[True] = True

    @model_validator(mode="after")
    def _validate_coverage(self) -> "HumanDecisionEvidenceSummaryV1":
        pairs = (
            (self.identity_required_count, self.identity_confirmed_count),
            (self.effect_required_count, self.effect_confirmed_count),
        )
        if any(
            required is not None
            and confirmed is not None
            and confirmed > required
            for required, confirmed in pairs
        ):
            raise ValueError("confirmed evidence cannot exceed required evidence")
        return self


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


class HumanDecisionTaskV1(_StrictContract):
    """A signed, PHI-free projection of one exact attended runtime pause."""

    schema_version: Literal["openadapt.human-decision-task/v1"] = (
        HUMAN_DECISION_TASK_SCHEMA
    )
    task_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    task_revision: StrictInt = Field(default=1, ge=1)
    tenant_id: StrictStr | None = Field(default=None, pattern=_OPAQUE_ID_PATTERN)
    runner_id: StrictStr | None = Field(default=None, pattern=_OPAQUE_ID_PATTERN)
    run_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    pause_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    capability_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    bundle_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    task_kind: HumanDecisionTaskKind
    delivery_state: HumanDecisionDeliveryState
    risk_class: HumanDecisionRiskClass = HumanDecisionRiskClass.UNKNOWN
    substrate: HumanDecisionSubstrate = HumanDecisionSubstrate.UNKNOWN
    question: HumanDecisionQuestionV1
    evidence: HumanDecisionEvidenceSummaryV1
    allowed_actions: tuple[HumanDecisionAction, ...] = Field(min_length=1, max_length=4)
    required_authn: HumanDecisionRequiredAuthn
    created_at: StrictStr = Field(min_length=20, max_length=40)
    expires_at: StrictStr = Field(min_length=20, max_length=40)
    nonce: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    issuer_key_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    signature_algorithm: Literal["hmac-sha256"] = "hmac-sha256"
    signature: StrictStr = Field(pattern=_SIGNATURE_PATTERN)

    @model_validator(mode="after")
    def _validate_envelope(self) -> "HumanDecisionTaskV1":
        if len(set(self.allowed_actions)) != len(self.allowed_actions):
            raise ValueError("allowed_actions must be unique")
        created_at = _parse_timestamp(self.created_at, "created_at")
        expires_at = _parse_timestamp(self.expires_at, "expires_at")
        if expires_at <= created_at:
            raise ValueError("expires_at must be after created_at")
        return self

    def unsigned_payload(self) -> dict[str, Any]:
        """Return the deterministic language-agnostic signed payload."""

        return self.model_dump(mode="json", exclude={"signature"})

    def canonical_unsigned_bytes(self) -> bytes:
        return _canonical_json(self.unsigned_payload())

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_unsigned_bytes()).hexdigest()

    def verify_hmac(self, key: bytes) -> bool:
        """Verify the task signature without granting execution authority."""

        _validate_hmac_key(key)
        expected = _hmac_signature(key, self.unsigned_payload())
        return hmac.compare_digest(expected, self.signature)


def _validate_hmac_key(key: bytes) -> None:
    if not isinstance(key, bytes) or len(key) < 32:
        raise ValueError("human decision HMAC key must contain at least 32 bytes")


def _hmac_signature(key: bytes, payload: Mapping[str, Any]) -> str:
    digest = hmac.new(key, _SIGNING_DOMAIN + _canonical_json(payload), hashlib.sha256)
    return "hmac-sha256:" + digest.hexdigest()


def sign_human_decision_task_hmac(
    *, key: bytes, fields: Mapping[str, Any]
) -> HumanDecisionTaskV1:
    """Validate and sign a task using the local-v1 HMAC profile."""

    _validate_hmac_key(key)
    if "signature" in fields:
        raise ValueError("fields must not contain a signature")
    unsigned = dict(fields)
    unsigned.setdefault("schema_version", HUMAN_DECISION_TASK_SCHEMA)
    unsigned.setdefault("signature_algorithm", "hmac-sha256")
    validated = HumanDecisionTaskV1.model_validate(
        {**unsigned, "signature": "hmac-sha256:" + "0" * 64}
    )
    canonical_unsigned = validated.unsigned_payload()
    signature = _hmac_signature(key, canonical_unsigned)
    return validated.model_copy(update={"signature": signature})

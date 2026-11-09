"""Value-free contracts for attended or brokered authentication.

Authentication remains a human decision task.  These contracts add the
session, principal-class, capture-exclusion, and freshness evidence that the
general attended-task schema does not carry.  They never carry a credential,
account label, session token, or provider item name.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from openadapt_types.human_decision import HumanDecisionSubstrate

AUTHENTICATION_CAPTURE_POLICY_SCHEMA: Literal[
    "openadapt.authentication-capture-policy/v1"
] = "openadapt.authentication-capture-policy/v1"
AUTHENTICATION_TASK_SCHEMA: Literal["openadapt.authentication-task/v1"] = (
    "openadapt.authentication-task/v1"
)
AUTHENTICATION_RECEIPT_SCHEMA: Literal["openadapt.authentication-receipt/v1"] = (
    "openadapt.authentication-receipt/v1"
)

_OPAQUE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$"
_SHA256_PATTERN = r"^sha256:[a-f0-9]{64}$"
_HMAC_SHA256_PATTERN = r"^hmac-sha256:[a-f0-9]{64}$"
_TIMESTAMP_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"


class _StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AuthenticationContractError(ValueError):
    """The authentication receipt does not fit the live task and evidence."""


class AuthenticationMethod(str, Enum):
    EXISTING_SESSION = "existing_session"
    SAVED_ACCOUNT_SELECTION = "saved_account_selection"
    PASSWORD_MANAGER_AUTOFILL = "password_manager_autofill"
    PASSKEY = "passkey"
    FEDERATED_IDENTITY = "federated_identity"
    SMART_CARD = "smart_card"
    DEVICE_BROKER = "device_broker"
    MANUAL_SECRET_ENTRY = "manual_secret_entry"


class AuthenticationPrincipalClass(str, Enum):
    NAMED_USER = "named_user"
    SHARED_SERVICE_ACCOUNT = "shared_service_account"
    WORKFORCE_IDENTITY = "workforce_identity"
    DEVICE_IDENTITY = "device_identity"


class AuthenticationVerifierKind(str, Enum):
    AUTHENTICATED_SESSION_PROBE = "authenticated_session_probe"
    POST_AUTH_APPLICATION_STATE = "post_auth_application_state"
    IDENTITY_PROVIDER_INTROSPECTION = "identity_provider_introspection"


class AuthenticationMfaPolicy(str, Enum):
    REQUIRED = "required"
    IF_CHALLENGED = "if_challenged"
    NOT_REQUIRED = "not_required"


class AuthenticationMfaOutcome(str, Enum):
    VERIFIED = "verified"
    NOT_CHALLENGED = "not_challenged"
    NOT_REQUIRED = "not_required"


class AuthenticationOutcome(str, Enum):
    VERIFIED = "verified"
    REFUTED = "refuted"
    INDETERMINATE = "indeterminate"


def _canonical_json(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value)).hexdigest()


def _parse_utc(value: str, field_name: str) -> datetime:
    if re.fullmatch(_TIMESTAMP_PATTERN, value) is None:
        raise ValueError(f"{field_name} must be a canonical UTC timestamp")
    return datetime.fromisoformat(value[:-1] + "+00:00").astimezone(timezone.utc)


class AuthenticationCapturePolicyV1(_StrictContract):
    """The source-time exclusion policy for one protected interval."""

    schema_version: Literal["openadapt.authentication-capture-policy/v1"] = (
        AUTHENTICATION_CAPTURE_POLICY_SCHEMA
    )
    input_values: Literal["withheld_at_source"] = "withheld_at_source"
    input_targets: Literal["withheld_at_source"] = "withheld_at_source"
    media_frames: Literal["withheld_at_source"] = "withheld_at_source"
    structural_values: Literal["withheld_at_source"] = "withheld_at_source"
    account_labels: Literal["withheld_at_source"] = "withheld_at_source"
    timeline: Literal["protected_marker_only"] = "protected_marker_only"


class AuthenticationTaskContractV1(_StrictContract):
    """One value-free authentication requirement bound to an attended task."""

    schema_version: Literal["openadapt.authentication-task/v1"] = (
        AUTHENTICATION_TASK_SCHEMA
    )
    task_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    human_decision_task_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    attended_task_kind: Literal["human_step"] = "human_step"
    question_template: Literal["complete_human_step"] = "complete_human_step"
    substrate: HumanDecisionSubstrate
    allowed_methods: tuple[AuthenticationMethod, ...] = Field(
        min_length=1,
        max_length=len(AuthenticationMethod),
        json_schema_extra={"uniqueItems": True},
    )
    principal_class: AuthenticationPrincipalClass
    requires_user_presence: StrictBool = True
    mfa_policy: AuthenticationMfaPolicy = AuthenticationMfaPolicy.IF_CHALLENGED
    max_session_age_seconds: StrictInt = Field(default=900, ge=30, le=86_400)
    verifier_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    verifier_kind: AuthenticationVerifierKind
    verifier_contract_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    principal_binding_contract_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    application_contract_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    environment_contract_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    broker_attestation: Literal["required", "optional", "not_applicable"] = "optional"
    credential_provider_boundary: Literal["external_adapter_only"] = (
        "external_adapter_only"
    )
    capture: AuthenticationCapturePolicyV1 = Field(
        default_factory=AuthenticationCapturePolicyV1
    )

    @field_validator("allowed_methods")
    @classmethod
    def _ordered_methods(
        cls, value: tuple[AuthenticationMethod, ...]
    ) -> tuple[AuthenticationMethod, ...]:
        ordered = tuple(sorted(value, key=lambda item: item.value))
        if value != ordered or len(value) != len(set(value)):
            raise ValueError("authentication methods must be unique and ordered")
        return value

    @property
    def digest(self) -> str:
        return _sha256(self)


class AuthenticationRunBindingV1(_StrictContract):
    """Trusted local evidence that can accept one authentication receipt."""

    app_version_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    process_execution_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    step_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    challenge_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    principal_binding_hmac: StrictStr = Field(pattern=_HMAC_SHA256_PATTERN)
    session_binding_hmac: StrictStr = Field(pattern=_HMAC_SHA256_PATTERN)
    operator_authority_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    verifier_evidence_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    capture_exclusion_receipt_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    broker_binding_digest: StrictStr | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
    )


class AuthenticationReceiptPayloadV1(_StrictContract):
    """Value-free evidence for one authentication verification attempt."""

    schema_version: Literal["openadapt.authentication-receipt/v1"] = (
        AUTHENTICATION_RECEIPT_SCHEMA
    )
    task_contract_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    task_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    human_decision_task_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    app_version_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    process_execution_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    step_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    challenge_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    method: AuthenticationMethod
    substrate: HumanDecisionSubstrate
    principal_class: AuthenticationPrincipalClass
    principal_binding_contract_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    principal_binding_hmac: StrictStr = Field(pattern=_HMAC_SHA256_PATTERN)
    session_binding_hmac: StrictStr = Field(pattern=_HMAC_SHA256_PATTERN)
    operator_authority_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    authenticated_at: StrictStr = Field(pattern=_TIMESTAMP_PATTERN)
    verified_at: StrictStr = Field(pattern=_TIMESTAMP_PATTERN)
    fresh_until: StrictStr = Field(pattern=_TIMESTAMP_PATTERN)
    user_presence_outcome: Literal["verified", "not_required"]
    mfa_outcome: AuthenticationMfaOutcome
    verifier_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    verifier_contract_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    verifier_evidence_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    capture_exclusion_receipt_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    broker_binding_digest: StrictStr | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
    )
    credential_value_retained: Literal[False] = False
    account_label_retained: Literal[False] = False
    outcome: AuthenticationOutcome

    @field_validator("authenticated_at", "verified_at", "fresh_until")
    @classmethod
    def _canonical_time(cls, value: str, info: Any) -> str:
        _parse_utc(value, str(info.field_name))
        return value


class AuthenticationReceiptV1(AuthenticationReceiptPayloadV1):
    """A self-digesting attempt receipt anchored by the process ledger."""

    receipt_digest: StrictStr = Field(pattern=_SHA256_PATTERN)

    def computed_digest(self) -> str:
        return _sha256(self.model_dump(mode="json", exclude={"receipt_digest"}))

    @model_validator(mode="after")
    def _exact_digest(self) -> AuthenticationReceiptV1:
        if self.receipt_digest != self.computed_digest():
            raise ValueError("authentication receipt digest does not match")
        return self


def issue_authentication_receipt(
    payload: AuthenticationReceiptPayloadV1,
) -> AuthenticationReceiptV1:
    """Create a content-bound receipt for one verification attempt."""

    data = payload.model_dump(mode="json")
    return AuthenticationReceiptV1(**data, receipt_digest=_sha256(data))


def validate_authentication_receipt(
    contract: AuthenticationTaskContractV1,
    binding: AuthenticationRunBindingV1,
    receipt: AuthenticationReceiptV1,
    *,
    now: datetime | None = None,
) -> AuthenticationReceiptV1:
    """Return a verified receipt only when every live binding agrees.

    This function does not resume a run.  The existing attended runtime must
    still reacquire the application and repeat its live checks.
    """

    errors: list[str] = []
    expected = {
        "task_contract_digest": contract.digest,
        "task_id": contract.task_id,
        "human_decision_task_digest": contract.human_decision_task_digest,
        "app_version_digest": binding.app_version_digest,
        "process_execution_id": binding.process_execution_id,
        "step_id": binding.step_id,
        "challenge_digest": binding.challenge_digest,
        "principal_binding_hmac": binding.principal_binding_hmac,
        "session_binding_hmac": binding.session_binding_hmac,
        "operator_authority_digest": binding.operator_authority_digest,
        "verifier_evidence_digest": binding.verifier_evidence_digest,
        "capture_exclusion_receipt_digest": (binding.capture_exclusion_receipt_digest),
        "broker_binding_digest": binding.broker_binding_digest,
        "substrate": contract.substrate,
        "principal_class": contract.principal_class,
        "principal_binding_contract_digest": (
            contract.principal_binding_contract_digest
        ),
        "verifier_id": contract.verifier_id,
        "verifier_contract_digest": contract.verifier_contract_digest,
    }
    for field_name, expected_value in expected.items():
        if getattr(receipt, field_name) != expected_value:
            errors.append(f"{field_name} differs")
    if receipt.method not in contract.allowed_methods:
        errors.append("authentication method is not admitted")
    if contract.broker_attestation == "required" and not receipt.broker_binding_digest:
        errors.append("the required broker binding is absent")
    if contract.requires_user_presence and receipt.user_presence_outcome != "verified":
        errors.append("the required user presence is not verified")
    if (
        contract.mfa_policy is AuthenticationMfaPolicy.REQUIRED
        and receipt.mfa_outcome is not AuthenticationMfaOutcome.VERIFIED
    ):
        errors.append("the required MFA result is not verified")
    if (
        contract.mfa_policy is AuthenticationMfaPolicy.IF_CHALLENGED
        and receipt.mfa_outcome is AuthenticationMfaOutcome.NOT_REQUIRED
    ):
        errors.append("the conditional MFA result is not declared")

    authenticated = _parse_utc(receipt.authenticated_at, "authenticated_at")
    verified = _parse_utc(receipt.verified_at, "verified_at")
    fresh_until = _parse_utc(receipt.fresh_until, "fresh_until")
    maximum = authenticated + timedelta(seconds=contract.max_session_age_seconds)
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if verified < authenticated:
        errors.append("verification precedes authentication")
    if fresh_until < authenticated:
        errors.append("session freshness ends before authentication")
    if fresh_until > maximum:
        errors.append("session freshness exceeds the admitted maximum")
    if verified > fresh_until:
        errors.append("the session was stale when verification ran")
    if verified > clock:
        errors.append("authentication verification is in the future")
    if clock > fresh_until:
        errors.append("the authentication receipt is stale")
    if receipt.outcome is not AuthenticationOutcome.VERIFIED:
        errors.append("the authentication verifier did not confirm the session")
    if errors:
        raise AuthenticationContractError(
            "authentication receipt refused: " + "; ".join(errors)
        )
    return receipt

"""Signed non-production authority for one qualification campaign trial.

This contract is intentionally disjoint from production qualification
admission. A qualification campaign permit authorizes only one exact trial in
one exact non-production environment. It cannot be converted into production
authority and cannot represent production success.
"""

from __future__ import annotations

import hashlib
import re
from base64 import b64decode, b64encode
from datetime import datetime, timedelta, timezone
from typing import Any, Final, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import Field, StrictInt, field_validator, model_validator

from openadapt_flow.qualification_admission_v2 import (
    JS_MAX_SAFE_INTEGER,
    AdmittedRuntimeBuildIdentity,
    ClosedSignedModel,
    QualificationIssuer,
    QualificationSignerRegistry,
    canonical_json,
)

SCHEMA: Final[Literal["openadapt.qualification-campaign-permit/v1"]] = (
    "openadapt.qualification-campaign-permit/v1"
)
SIGNATURE_DOMAIN: Final[bytes] = b"openadapt-qualification-campaign-permit-v1\0"
POLICY_DOMAIN: Final[bytes] = b"openadapt-qualification-campaign-execution-policy-v1\0"
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+\-]{0,199}$")
_UTC_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_KEY_ID_RE = re.compile(r"^qa-ed25519-[a-f0-9]{16}$")
MAX_CAMPAIGN_PERMIT_LIFETIME: Final[timedelta] = timedelta(hours=24)

QUALIFICATION_CAMPAIGN_EXECUTION_POLICY: Final[dict[str, Any]] = {
    "schema_version": "openadapt.qualification-campaign-execution-policy/v1",
    "permit_schema": SCHEMA,
    "signature_domain": "openadapt-qualification-campaign-permit-v1\\0",
    "issuer_workflow": (
        "OpenAdaptAI/openadapt-internal/.github/workflows/"
        "issue-qualification-campaign-permit.yml"
    ),
    "issuer_ref_prefix": "refs/heads/main@",
    "maximum_lifetime_seconds": 24 * 60 * 60,
    "minimum_trials_per_condition": 3,
    "production_data_prohibited": True,
    "production_authority_prohibited": True,
    "production_success_prohibited": True,
}
QUALIFICATION_CAMPAIGN_EXECUTION_POLICY_SHA256: Final[str] = hashlib.sha256(
    POLICY_DOMAIN + canonical_json(QUALIFICATION_CAMPAIGN_EXECUTION_POLICY)
).hexdigest()


class QualificationCampaignPermitError(ValueError):
    """The qualification-only trial authority is invalid or stale."""


def _parse_utc(value: str, *, field: str) -> datetime:
    if not isinstance(value, str) or _UTC_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a canonical UTC timestamp")
    return datetime.fromisoformat(value[:-1] + "+00:00").astimezone(timezone.utc)


class QualificationCampaignTrialBinding(ClosedSignedModel):
    campaign_id: str = Field(pattern=_UUID_RE)
    campaign_contract_sha256: str = Field(pattern=_SHA256_RE)
    trial_id: str = Field(pattern=_UUID_RE)
    qualification_run_id: str = Field(pattern=_UUID_RE)
    task: str = Field(pattern=_ID_RE)
    condition: str = Field(pattern=_ID_RE)
    trial_ordinal: StrictInt = Field(ge=1, le=10_000)
    required_trials: StrictInt = Field(ge=3, le=10_000)
    input_digest: str = Field(pattern=_SHA256_RE)
    oracle_contract_sha256: str = Field(pattern=_SHA256_RE)
    evidence_sink_sha256: str = Field(pattern=_SHA256_RE)

    @model_validator(mode="after")
    def _valid_trial(self) -> "QualificationCampaignTrialBinding":
        if self.trial_ordinal > self.required_trials:
            raise ValueError("qualification trial ordinal exceeds its plan")
        return self


class QualificationCampaignPermitPayload(ClosedSignedModel):
    schema_version: Literal["openadapt.qualification-campaign-permit/v1"] = SCHEMA
    execution_purpose: Literal["qualification_campaign"] = "qualification_campaign"
    environment_class: Literal["non_production"] = "non_production"
    production_data_prohibited: Literal[True] = True
    production_authority_prohibited: Literal[True] = True
    production_success_prohibited: Literal[True] = True
    permit_id: str = Field(pattern=_UUID_RE)
    tenant_id: str = Field(pattern=_UUID_RE)
    workflow_id: str = Field(pattern=_UUID_RE)
    workflow_version_id: str = Field(pattern=_UUID_RE)
    bundle_version_id: str = Field(pattern=_UUID_RE)
    bundle_artifact_sha256: str = Field(pattern=_SHA256_RE)
    bundle_content_digest: str = Field(pattern=_SHA256_RE)
    environment_digest: str = Field(pattern=_SHA256_RE)
    environment_contract_sha256: str = Field(pattern=_SHA256_RE)
    runtime_environment_sha256: str = Field(pattern=_SHA256_RE)
    runtime_build_identity: AdmittedRuntimeBuildIdentity
    runtime_build_identity_sha256: str = Field(pattern=_SHA256_RE)
    qualification_signer_registry_sha256: str = Field(pattern=_SHA256_RE)
    qualification_signer_registry_revision: StrictInt = Field(
        ge=1, le=JS_MAX_SAFE_INTEGER
    )
    trial: QualificationCampaignTrialBinding
    policy_sha256: str = Field(pattern=_SHA256_RE)
    issuer: QualificationIssuer
    issued_at: str
    not_before: str
    expires_at: str

    @model_validator(mode="after")
    def _closed_nonproduction_authority(self) -> "QualificationCampaignPermitPayload":
        if self.workflow_version_id != self.bundle_version_id:
            raise ValueError("qualification workflow and bundle versions must match")
        if (
            self.runtime_build_identity_sha256
            != self.runtime_build_identity.artifact_sha256()
        ):
            raise ValueError("qualification runtime build digest is invalid")
        if self.policy_sha256 != QUALIFICATION_CAMPAIGN_EXECUTION_POLICY_SHA256:
            raise ValueError("qualification campaign execution policy is invalid")
        if (
            self.issuer.workflow
            != QUALIFICATION_CAMPAIGN_EXECUTION_POLICY["issuer_workflow"]
        ):
            raise ValueError("qualification campaign issuer workflow is invalid")
        expected_prefix = QUALIFICATION_CAMPAIGN_EXECUTION_POLICY["issuer_ref_prefix"]
        if (
            re.fullmatch(re.escape(expected_prefix) + r"[a-f0-9]{40}", self.issuer.ref)
            is None
        ):
            raise ValueError("qualification campaign issuer ref is invalid")
        issued = _parse_utc(self.issued_at, field="campaign permit issued_at")
        not_before = _parse_utc(self.not_before, field="campaign permit not_before")
        expires = _parse_utc(self.expires_at, field="campaign permit expires_at")
        if (
            not issued - timedelta(minutes=5)
            <= not_before
            <= issued + timedelta(minutes=5)
        ):
            raise ValueError("qualification campaign permit start is invalid")
        if not not_before < expires <= issued + MAX_CAMPAIGN_PERMIT_LIFETIME:
            raise ValueError("qualification campaign permit lifetime is invalid")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json(self)


class QualificationCampaignPermitEnvelope(ClosedSignedModel):
    payload: QualificationCampaignPermitPayload
    algorithm: Literal["ed25519"] = "ed25519"
    signature: str = Field(min_length=88, max_length=88)

    @field_validator("signature")
    @classmethod
    def _signature(cls, value: str) -> str:
        try:
            decoded = b64decode(value, validate=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("qualification campaign signature is invalid") from exc
        if len(decoded) != 64 or b64encode(decoded).decode("ascii") != value:
            raise ValueError("qualification campaign signature is invalid")
        return value

    def artifact_sha256(self) -> str:
        return hashlib.sha256(canonical_json(self)).hexdigest()


class QualificationCampaignPermitExpected(ClosedSignedModel):
    tenant_id: str = Field(pattern=_UUID_RE)
    workflow_id: str = Field(pattern=_UUID_RE)
    workflow_version_id: str = Field(pattern=_UUID_RE)
    bundle_version_id: str = Field(pattern=_UUID_RE)
    bundle_artifact_sha256: str = Field(pattern=_SHA256_RE)
    bundle_content_digest: str = Field(pattern=_SHA256_RE)
    environment_digest: str = Field(pattern=_SHA256_RE)
    environment_contract_sha256: str = Field(pattern=_SHA256_RE)
    runtime_environment_sha256: str = Field(pattern=_SHA256_RE)
    runtime_build_identity: AdmittedRuntimeBuildIdentity
    trial: QualificationCampaignTrialBinding


_EXPECTED_FIELDS: Final[tuple[str, ...]] = tuple(
    QualificationCampaignPermitExpected.model_fields
)


def sign_qualification_campaign_permit(
    payload: QualificationCampaignPermitPayload,
    private_key: Ed25519PrivateKey,
) -> QualificationCampaignPermitEnvelope:
    signature = private_key.sign(SIGNATURE_DOMAIN + payload.canonical_bytes())
    return QualificationCampaignPermitEnvelope(
        payload=payload,
        signature=b64encode(signature).decode("ascii"),
    )


def verify_qualification_campaign_permit(
    envelope: QualificationCampaignPermitEnvelope,
    *,
    registry: QualificationSignerRegistry,
    expected: QualificationCampaignPermitExpected,
    consumed_permit_ids: set[str] | frozenset[str] = frozenset(),
    now: datetime | None = None,
) -> str:
    """Verify one non-production trial permit against live independent state."""

    payload = envelope.payload
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    generated = _parse_utc(registry.generated_at, field="registry generated_at")
    registry_expires = _parse_utc(registry.expires_at, field="registry expires_at")
    if generated > current + timedelta(minutes=5) or current >= registry_expires:
        raise QualificationCampaignPermitError(
            "qualification signer registry is not current"
        )
    if (
        payload.qualification_signer_registry_sha256 != registry.artifact_sha256()
        or payload.qualification_signer_registry_revision != registry.revision
    ):
        raise QualificationCampaignPermitError(
            "qualification campaign registry does not match"
        )
    signer = next(
        (item for item in registry.signers if item.key_id == payload.issuer.key_id),
        None,
    )
    if signer is None or signer.status != "active":
        raise QualificationCampaignPermitError(
            "qualification campaign signer is not active"
        )
    if payload.issuer.workflow not in signer.allowed_workflows or not any(
        payload.issuer.ref.startswith(prefix) for prefix in signer.allowed_ref_prefixes
    ):
        raise QualificationCampaignPermitError(
            "qualification campaign signer scope is invalid"
        )
    try:
        Ed25519PublicKey.from_public_bytes(
            b64decode(signer.public_key, validate=True)
        ).verify(
            b64decode(envelope.signature, validate=True),
            SIGNATURE_DOMAIN + payload.canonical_bytes(),
        )
    except (InvalidSignature, ValueError) as exc:
        raise QualificationCampaignPermitError(
            "qualification campaign signature is invalid"
        ) from exc
    if current < _parse_utc(payload.not_before, field="campaign permit not_before"):
        raise QualificationCampaignPermitError(
            "qualification campaign permit is not active"
        )
    if current >= _parse_utc(payload.expires_at, field="campaign permit expires_at"):
        raise QualificationCampaignPermitError(
            "qualification campaign permit has expired"
        )
    if payload.permit_id in consumed_permit_ids:
        raise QualificationCampaignPermitError(
            "qualification campaign permit was already consumed"
        )
    for field in _EXPECTED_FIELDS:
        if getattr(payload, field) != getattr(expected, field):
            raise QualificationCampaignPermitError(
                f"qualification campaign {field} does not match live state"
            )
    return envelope.artifact_sha256()

"""Signed, expiring admission for one exact qualified workflow version.

Qualification evidence is an input to this contract.  It is not a prose label
and it is not a substitute for the live run gates.  The qualification authority
signs one closed payload after it verifies the counted campaign.  A runtime
then verifies the signature, validity interval, revocation state, and every
exact deployment binding before it can use the admission.

The payload contains only identifiers, bounded public contract names, counts,
and SHA-256 digests.  Customer values and evidence bodies stay inside their
declared boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
from base64 import b64decode, b64encode
from datetime import datetime, timedelta, timezone
from typing import Any, Final, Literal, Mapping
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA: Final[Literal["openadapt.qualification-admission/v1"]] = (
    "openadapt.qualification-admission/v1"
)
SIGNATURE_DOMAIN: Final[bytes] = b"openadapt-qualification-admission-v1\0"
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}$")
_UTC_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_KEY_ID_RE = re.compile(r"^qa-ed25519-[a-f0-9]{16}$")
MAX_ADMISSION_LIFETIME: Final[timedelta] = timedelta(days=30)


class QualificationAdmissionError(ValueError):
    """A qualification admission is invalid or does not fit the live run."""


def canonical_json(value: Any) -> bytes:
    """Return the cross-language canonical JSON used by Flow and Cloud."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def contract_sha256(value: Any) -> str:
    """Return the unprefixed digest for one canonical admission sub-contract."""

    return hashlib.sha256(canonical_json(value)).hexdigest()


def qualification_signer_key_id(public_key: bytes) -> str:
    """Return the stable identifier for one raw Ed25519 public key."""

    if len(public_key) != 32:
        raise ValueError("qualification signer public key is invalid")
    return f"qa-ed25519-{hashlib.sha256(public_key).hexdigest()[:16]}"


def _parse_utc(value: str, *, field: str) -> datetime:
    if not _UTC_RE.fullmatch(value):
        raise ValueError(f"{field} must be a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:  # pragma: no cover - guarded by the expression
        raise ValueError(f"{field} is invalid") from exc
    return parsed.astimezone(timezone.utc)


def _uuid(value: str, *, field: str) -> str:
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise ValueError(f"{field} must be a canonical UUID")
    return value


class QualificationCondition(BaseModel):
    """One task and condition with the required counted trial result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task: str = Field(min_length=1, max_length=128, pattern=_ID_RE)
    condition: str = Field(min_length=1, max_length=128, pattern=_ID_RE)
    required_trials: int = Field(ge=3, le=10_000)
    observed_trials: int = Field(ge=3, le=10_000)

    @model_validator(mode="after")
    def _complete_count(self) -> "QualificationCondition":
        if self.observed_trials < self.required_trials:
            raise ValueError("observed trials do not satisfy the required count")
        return self


class QualificationCampaignBinding(BaseModel):
    """The exact counted campaign accepted by the qualification authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_sha256: str = Field(pattern=_SHA256_RE)
    contract_sha256: str = Field(pattern=_SHA256_RE)
    outcomes_sha256: str = Field(pattern=_SHA256_RE)
    oracle_id: str = Field(min_length=1, max_length=200, pattern=_ID_RE)
    oracle_contract_sha256: str = Field(pattern=_SHA256_RE)
    tasks: tuple[QualificationCondition, ...] = Field(min_length=1, max_length=1_000)
    failure_taxonomy: tuple[str, ...] = Field(min_length=2, max_length=128)
    decision: Literal["admitted"] = "admitted"

    @model_validator(mode="after")
    def _closed_campaign(self) -> "QualificationCampaignBinding":
        task_keys = tuple((item.task, item.condition) for item in self.tasks)
        if task_keys != tuple(sorted(task_keys)) or len(task_keys) != len(
            set(task_keys)
        ):
            raise ValueError("qualification task conditions must be unique and ordered")
        if (
            self.failure_taxonomy != tuple(sorted(self.failure_taxonomy))
            or len(self.failure_taxonomy) != len(set(self.failure_taxonomy))
            or any(_ID_RE.fullmatch(item) is None for item in self.failure_taxonomy)
        ):
            raise ValueError(
                "qualification failure taxonomy must be unique and ordered"
            )
        required = {"over_halt", "silent_incorrect_success"}
        if not required.issubset(self.failure_taxonomy):
            raise ValueError(
                "qualification failure taxonomy must report silent incorrect "
                "success and over-halt"
            )
        return self


class QualificationIssuer(BaseModel):
    """Review workflow identity recorded by the qualification signer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key_id: str = Field(min_length=1, max_length=128, pattern=_ID_RE)
    workflow: str = Field(min_length=1, max_length=200, pattern=_ID_RE)
    ref: str = Field(min_length=1, max_length=200, pattern=_ID_RE)


class QualificationAdmissionPayload(BaseModel):
    """Closed production admission for one exact workflow version."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.qualification-admission/v1"] = SCHEMA
    admission_id: str
    tenant_id: str
    workflow_id: str
    workflow_version_id: str
    bundle_version_id: str
    runtime_validation_id: str
    bundle_artifact_sha256: str = Field(pattern=_SHA256_RE)
    bundle_content_digest: str = Field(pattern=_SHA256_RE)
    governed_authorization_template_sha256: str = Field(pattern=_SHA256_RE)
    application_contract_sha256: str = Field(pattern=_SHA256_RE)
    substrate_contract_sha256: str = Field(pattern=_SHA256_RE)
    environment_contract_sha256: str = Field(pattern=_SHA256_RE)
    runtime_environment_sha256: str = Field(pattern=_SHA256_RE)
    runtime_contract_sha256: str = Field(pattern=_SHA256_RE)
    input_policy_sha256: str = Field(pattern=_SHA256_RE)
    action_policy_sha256: str = Field(pattern=_SHA256_RE)
    network_policy_sha256: str = Field(pattern=_SHA256_RE)
    identity_contract_sha256: str = Field(pattern=_SHA256_RE)
    effect_contract_sha256: str = Field(pattern=_SHA256_RE)
    operator_contract_sha256: str = Field(pattern=_SHA256_RE)
    campaign: QualificationCampaignBinding
    issuer: QualificationIssuer
    issued_at: str
    not_before: str
    expires_at: str

    @field_validator(
        "admission_id",
        "tenant_id",
        "workflow_id",
        "workflow_version_id",
        "bundle_version_id",
        "runtime_validation_id",
    )
    @classmethod
    def _canonical_uuid(cls, value: str, info: Any) -> str:
        return _uuid(value, field=info.field_name)

    @model_validator(mode="after")
    def _validity_interval(self) -> "QualificationAdmissionPayload":
        if self.admission_id == self.runtime_validation_id:
            raise ValueError(
                "qualification admission identity must be independent from "
                "runtime validation identity"
            )
        issued = _parse_utc(self.issued_at, field="issued_at")
        not_before = _parse_utc(self.not_before, field="not_before")
        expires = _parse_utc(self.expires_at, field="expires_at")
        if (
            not issued - timedelta(minutes=5)
            <= not_before
            <= issued + timedelta(minutes=5)
        ):
            raise ValueError("qualification admission start is outside issue skew")
        if expires <= not_before:
            raise ValueError("qualification admission expiry must follow its start")
        if expires > issued + MAX_ADMISSION_LIFETIME:
            raise ValueError("qualification admission lifetime exceeds 30 days")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.model_dump(mode="json"))


class QualificationAdmissionEnvelope(BaseModel):
    """Signed admission artifact retained by Cloud and verified by Flow."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    payload: QualificationAdmissionPayload
    algorithm: Literal["ed25519"] = "ed25519"
    signature: str = Field(min_length=88, max_length=88)

    @field_validator("signature")
    @classmethod
    def _canonical_signature(cls, value: str) -> str:
        try:
            decoded = b64decode(value, validate=True)
        except ValueError as exc:
            raise ValueError(
                "qualification admission signature must be base64"
            ) from exc
        if len(decoded) != 64 or b64encode(decoded).decode("ascii") != value:
            raise ValueError("qualification admission signature is invalid")
        return value

    def artifact_sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.model_dump(mode="json"))).hexdigest()


class QualificationSignerTrust(BaseModel):
    """Locally configured authority rule for one qualification signer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    public_key: str
    allowed_workflows: tuple[str, ...] = Field(min_length=1, max_length=128)
    allowed_ref_prefixes: tuple[str, ...] = Field(min_length=1, max_length=128)

    @field_validator("public_key")
    @classmethod
    def _public_key(cls, value: str) -> str:
        try:
            decoded = b64decode(value, validate=True)
        except ValueError as exc:
            raise ValueError("qualification signer public key must be base64") from exc
        if len(decoded) != 32 or b64encode(decoded).decode("ascii") != value:
            raise ValueError("qualification signer public key is invalid")
        return value

    @model_validator(mode="after")
    def _ordered_scope(self) -> "QualificationSignerTrust":
        for label, values in (
            ("workflow", self.allowed_workflows),
            ("ref prefix", self.allowed_ref_prefixes),
        ):
            if values != tuple(sorted(values)) or len(values) != len(set(values)):
                raise ValueError(f"qualification signer {label}s must be ordered")
            if any(not value or len(value) > 200 for value in values):
                raise ValueError(f"qualification signer {label} is invalid")
        return self


class QualificationAdmissionExpected(BaseModel):
    """Exact live bindings the signed admission must match."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str
    workflow_id: str
    workflow_version_id: str
    bundle_version_id: str
    runtime_validation_id: str
    bundle_artifact_sha256: str = Field(pattern=_SHA256_RE)
    bundle_content_digest: str = Field(pattern=_SHA256_RE)
    governed_authorization_template_sha256: str = Field(pattern=_SHA256_RE)
    application_contract_sha256: str = Field(pattern=_SHA256_RE)
    substrate_contract_sha256: str = Field(pattern=_SHA256_RE)
    environment_contract_sha256: str = Field(pattern=_SHA256_RE)
    runtime_environment_sha256: str = Field(pattern=_SHA256_RE)
    runtime_contract_sha256: str = Field(pattern=_SHA256_RE)
    input_policy_sha256: str = Field(pattern=_SHA256_RE)
    action_policy_sha256: str = Field(pattern=_SHA256_RE)
    network_policy_sha256: str = Field(pattern=_SHA256_RE)
    identity_contract_sha256: str = Field(pattern=_SHA256_RE)
    effect_contract_sha256: str = Field(pattern=_SHA256_RE)
    operator_contract_sha256: str = Field(pattern=_SHA256_RE)

    @field_validator(
        "tenant_id",
        "workflow_id",
        "workflow_version_id",
        "bundle_version_id",
        "runtime_validation_id",
    )
    @classmethod
    def _canonical_uuid(cls, value: str, info: Any) -> str:
        return _uuid(value, field=info.field_name)


_EXPECTED_FIELDS: Final[tuple[str, ...]] = tuple(
    QualificationAdmissionExpected.model_fields
)


def expected_from_payload(
    payload: QualificationAdmissionPayload,
) -> QualificationAdmissionExpected:
    """Project the exact fields a separate live observer must reproduce."""

    return QualificationAdmissionExpected.model_validate(
        {field: getattr(payload, field) for field in _EXPECTED_FIELDS}
    )


def load_qualification_signer_trust(
    value: str | bytes | Mapping[str, Any],
) -> dict[str, QualificationSignerTrust]:
    """Strictly load the locally configured qualification signer registry."""

    try:
        raw = json.loads(value) if isinstance(value, (str, bytes)) else dict(value)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise QualificationAdmissionError(
            "qualification signer trust registry is invalid"
        ) from exc
    if not isinstance(raw, dict) or not raw:
        raise QualificationAdmissionError(
            "qualification signer trust registry is unavailable"
        )
    parsed: dict[str, QualificationSignerTrust] = {}
    try:
        for key_id, trust in raw.items():
            if not isinstance(key_id, str) or _KEY_ID_RE.fullmatch(key_id) is None:
                raise ValueError("qualification signer key id is invalid")
            parsed_trust = QualificationSignerTrust.model_validate(trust)
            raw_public_key = b64decode(parsed_trust.public_key, validate=True)
            if qualification_signer_key_id(raw_public_key) != key_id:
                raise ValueError("qualification signer key id does not match its key")
            parsed[key_id] = parsed_trust
    except ValueError as exc:
        raise QualificationAdmissionError(
            "qualification signer trust registry is invalid"
        ) from exc
    from openadapt_flow.qualification_dev_signer import (
        reject_local_dev_in_production_trust,
    )

    reject_local_dev_in_production_trust(parsed)
    return parsed


def sign_qualification_admission(
    payload: QualificationAdmissionPayload,
    private_key: Ed25519PrivateKey,
) -> QualificationAdmissionEnvelope:
    """Sign one already-reviewed qualification decision."""

    signature = private_key.sign(SIGNATURE_DOMAIN + payload.canonical_bytes())
    return QualificationAdmissionEnvelope(
        payload=payload,
        signature=b64encode(signature).decode("ascii"),
    )


def verify_qualification_admission(
    envelope: QualificationAdmissionEnvelope,
    *,
    trusted_signers: Mapping[str, QualificationSignerTrust],
    expected: QualificationAdmissionExpected,
    revoked_admission_ids: set[str] | frozenset[str] = frozenset(),
    now: datetime | None = None,
) -> str:
    """Verify the production admission and return its canonical artifact digest.

    The caller supplies the live values.  The function does not infer a missing
    identity, policy, runtime, or environment value from the signed record.
    """

    from openadapt_flow.qualification_dev_signer import (
        is_local_dev_issuer_workflow,
        is_local_dev_key_id,
        reject_local_dev_in_production_trust,
    )

    reject_local_dev_in_production_trust(trusted_signers)
    payload = envelope.payload
    if is_local_dev_key_id(payload.issuer.key_id) or is_local_dev_issuer_workflow(
        payload.issuer.workflow
    ):
        raise QualificationAdmissionError(
            "local-dev qualification signer cannot enter a production trust map"
        )
    trust = trusted_signers.get(payload.issuer.key_id)
    if trust is None:
        raise QualificationAdmissionError("qualification signer is not trusted")
    if (
        qualification_signer_key_id(b64decode(trust.public_key, validate=True))
        != payload.issuer.key_id
    ):
        raise QualificationAdmissionError("qualification signer key id is invalid")
    if payload.issuer.workflow not in trust.allowed_workflows:
        raise QualificationAdmissionError(
            "qualification issuer workflow is not allowed"
        )
    if not any(
        payload.issuer.ref.startswith(prefix) for prefix in trust.allowed_ref_prefixes
    ):
        raise QualificationAdmissionError("qualification issuer ref is not allowed")
    try:
        public_key = Ed25519PublicKey.from_public_bytes(
            b64decode(trust.public_key, validate=True)
        )
        public_key.verify(
            b64decode(envelope.signature, validate=True),
            SIGNATURE_DOMAIN + payload.canonical_bytes(),
        )
    except (InvalidSignature, ValueError) as exc:
        raise QualificationAdmissionError(
            "qualification admission signature is invalid"
        ) from exc

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    issued = _parse_utc(payload.issued_at, field="issued_at")
    not_before = _parse_utc(payload.not_before, field="not_before")
    expires = _parse_utc(payload.expires_at, field="expires_at")
    if issued > current + timedelta(minutes=5):
        raise QualificationAdmissionError("qualification admission is future-issued")
    if current < not_before:
        raise QualificationAdmissionError("qualification admission is not active")
    if current >= expires:
        raise QualificationAdmissionError("qualification admission has expired")
    if payload.admission_id in revoked_admission_ids:
        raise QualificationAdmissionError("qualification admission is revoked")

    for field in _EXPECTED_FIELDS:
        if getattr(payload, field) != getattr(expected, field):
            raise QualificationAdmissionError(
                f"qualification admission {field} does not match the live run"
            )
    return envelope.artifact_sha256()

"""Verification of the signed seven-target Product release admission."""

from __future__ import annotations

import hashlib
import json
import re
from base64 import b64decode, urlsafe_b64decode, urlsafe_b64encode
from datetime import datetime, timezone
from typing import Literal, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DOMAIN = b"openadapt.product-release-admission-payload.v1\0"
TARGETS = ("agent", "capture", "cloud", "desktop", "docs", "flow", "openadapt")
_HEX64 = r"^[a-f0-9]{64}$"
_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_KEY_ID = r"^release-admission-ed25519-[a-f0-9]{16}$"
_SAFE_ID = r"^[A-Za-z0-9][A-Za-z0-9._:@/+\-]{0,199}$"
_UTC = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")


class ProductReleaseAdmissionError(ValueError):
    """The aggregate admission is invalid or inactive."""


class _Closed(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


def _canonical_json(value: object) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _utc(value: str, *, field: str) -> datetime:
    if _UTC.fullmatch(value) is None:
        raise ValueError(f"{field} is not canonical UTC seconds")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{field} is not UTC seconds") from exc
    if parsed.tzinfo is None or parsed.microsecond != 0:
        raise ValueError(f"{field} is not UTC seconds")
    return parsed.astimezone(timezone.utc)


class ProductReleaseTarget(_Closed):
    target: Literal["agent", "capture", "cloud", "desktop", "docs", "flow", "openadapt"]
    admission_id: str = Field(pattern=_UUID)
    admission_sha256: str = Field(pattern=_HEX64)
    release_id: str = Field(pattern=_SAFE_ID)
    release_artifact_sha256: str = Field(pattern=_HEX64)
    admission_issued_at: str
    admission_expires_at: str
    revoked_at: str | None
    artifact_authority_sha256: str = Field(pattern=_HEX64)
    artifact_authority_state: Literal["active", "revoked", "expired", "unavailable"]
    artifact_authority_checked_at: str
    artifact_authority_expires_at: str

    @model_validator(mode="after")
    def _chronology(self) -> "ProductReleaseTarget":
        issued = _utc(self.admission_issued_at, field="target admission_issued_at")
        expires = _utc(self.admission_expires_at, field="target admission_expires_at")
        checked = _utc(
            self.artifact_authority_checked_at,
            field="target artifact_authority_checked_at",
        )
        authority_expires = _utc(
            self.artifact_authority_expires_at,
            field="target artifact_authority_expires_at",
        )
        if issued >= expires or checked >= authority_expires:
            raise ValueError("product release target chronology is invalid")
        if self.revoked_at is not None:
            _utc(self.revoked_at, field="target revoked_at")
        return self


class ProductReleaseAdmissionPayload(_Closed):
    schema_version: Literal["openadapt.product-release-admission-payload/v1"]
    set_id: str = Field(pattern=_UUID)
    sequence: int = Field(gt=0, le=9_007_199_254_740_991)
    policy_sha256: str = Field(pattern=_HEX64)
    issued_at: str
    expires_at: str
    targets: tuple[ProductReleaseTarget, ...] = Field(min_length=7, max_length=7)

    @model_validator(mode="after")
    def _closed_set(self) -> "ProductReleaseAdmissionPayload":
        issued = _utc(self.issued_at, field="product admission issued_at")
        expires = _utc(self.expires_at, field="product admission expires_at")
        if issued >= expires:
            raise ValueError("product release admission chronology is invalid")
        if tuple(item.target for item in self.targets) != TARGETS:
            raise ValueError(
                "product release admission targets are not exact and ordered"
            )
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self)

    def payload_sha256_value(self) -> str:
        return hashlib.sha256(DOMAIN + self.canonical_bytes()).hexdigest()


class ProductReleaseSigner(_Closed):
    algorithm: Literal["ed25519"]
    key_id: str = Field(pattern=_KEY_ID)
    public_key: str

    @field_validator("public_key")
    @classmethod
    def _key(cls, value: str) -> str:
        try:
            raw = b64decode(value, validate=True)
        except ValueError as exc:
            raise ValueError("product release signer key is invalid") from exc
        if len(raw) != 32:
            raise ValueError("product release signer key is invalid")
        return value

    @model_validator(mode="after")
    def _key_id_matches(self) -> "ProductReleaseSigner":
        raw = b64decode(self.public_key, validate=True)
        expected = "release-admission-ed25519-" + hashlib.sha256(raw).hexdigest()[:16]
        if self.key_id != expected:
            raise ValueError("product release signer key id is invalid")
        return self


class ProductReleaseAdmissionArtifact(_Closed):
    schema_version: Literal["openadapt.product-release-admission-artifact/v1"]
    payload: ProductReleaseAdmissionPayload
    payload_sha256: str = Field(pattern=_HEX64)
    signer: ProductReleaseSigner
    signature: str = Field(min_length=86, max_length=86)

    @model_validator(mode="after")
    def _self_consistent(self) -> "ProductReleaseAdmissionArtifact":
        if self.payload_sha256 != self.payload.payload_sha256_value():
            raise ValueError("product release admission payload digest is invalid")
        try:
            signature = urlsafe_b64decode(self.signature + "==")
        except ValueError as exc:
            raise ValueError("product release admission signature is invalid") from exc
        if (
            len(signature) != 64
            or urlsafe_b64encode(signature).decode("ascii").rstrip("=")
            != self.signature
        ):
            raise ValueError("product release admission signature is invalid")
        try:
            Ed25519PublicKey.from_public_bytes(
                b64decode(self.signer.public_key, validate=True)
            ).verify(signature, DOMAIN + self.payload.canonical_bytes())
        except (InvalidSignature, ValueError) as exc:
            raise ValueError("product release admission signature is invalid") from exc
        return self

    def artifact_sha256(self) -> str:
        return hashlib.sha256(_canonical_json(self)).hexdigest()


class ProductReleaseSignerTrust(_Closed):
    public_key: str
    status: Literal["active", "revoked"]
    revoked_at: str | None

    @model_validator(mode="after")
    def _state(self) -> "ProductReleaseSignerTrust":
        if self.status == "active" and self.revoked_at is not None:
            raise ValueError("active product release signer has a revocation time")
        if self.status == "revoked" and self.revoked_at is None:
            raise ValueError("revoked product release signer lacks a revocation time")
        if self.revoked_at is not None:
            _utc(self.revoked_at, field="product release signer revoked_at")
        return self


def verify_product_release_admission(
    artifact: ProductReleaseAdmissionArtifact,
    *,
    trusted_signers: Mapping[str, ProductReleaseSignerTrust],
    newest_sequence: int,
    revoked_set_ids: set[str] | frozenset[str] = frozenset(),
    now: datetime | None = None,
) -> ProductReleaseAdmissionPayload:
    """Verify signature, authority state, time, revocation, and newest sequence."""

    try:
        artifact = ProductReleaseAdmissionArtifact.model_validate_json(
            _canonical_json(artifact)
        )
    except ValueError as exc:
        raise ProductReleaseAdmissionError(str(exc)) from exc
    trust = trusted_signers.get(artifact.signer.key_id)
    if trust is None or trust.public_key != artifact.signer.public_key:
        raise ProductReleaseAdmissionError("product release signer is not trusted")
    if trust.status != "active":
        raise ProductReleaseAdmissionError("product release signer is revoked")
    payload = artifact.payload
    if payload.set_id in revoked_set_ids:
        raise ProductReleaseAdmissionError("product release admission is revoked")
    if payload.sequence != newest_sequence:
        raise ProductReleaseAdmissionError("product release admission is superseded")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if (
        not _utc(payload.issued_at, field="issued_at")
        <= current
        < _utc(payload.expires_at, field="expires_at")
    ):
        raise ProductReleaseAdmissionError("product release admission is not active")
    for target in payload.targets:
        if target.revoked_at is not None:
            raise ProductReleaseAdmissionError(
                f"product release target {target.target} is revoked"
            )
        if target.artifact_authority_state != "active":
            raise ProductReleaseAdmissionError(
                f"product release target {target.target} authority is not active"
            )
        if (
            not _utc(target.admission_issued_at, field="target issued_at")
            <= current
            < _utc(target.admission_expires_at, field="target expires_at")
        ):
            raise ProductReleaseAdmissionError(
                f"product release target {target.target} admission is not active"
            )
        if (
            not _utc(target.artifact_authority_checked_at, field="authority checked_at")
            <= current
            < _utc(target.artifact_authority_expires_at, field="authority expires_at")
        ):
            raise ProductReleaseAdmissionError(
                f"product release target {target.target} authority is stale"
            )
    return payload


def load_product_release_signer_trust(
    raw: object,
) -> dict[str, ProductReleaseSignerTrust]:
    if not isinstance(raw, dict) or not raw:
        raise ProductReleaseAdmissionError(
            "product release signer trust is unavailable"
        )
    try:
        parsed = {
            str(key): ProductReleaseSignerTrust.model_validate(value)
            for key, value in raw.items()
        }
    except ValueError as exc:
        raise ProductReleaseAdmissionError(
            "product release signer trust is invalid"
        ) from exc
    if any(re.fullmatch(_KEY_ID, key) is None for key in parsed):
        raise ProductReleaseAdmissionError("product release signer key id is invalid")
    return parsed

"""The signature chain behind a fleet patch.

Two links, both Ed25519 over a domain-separated canonical JSON payload (the
same construction the qualification permits use):

1. A **fleet root** signs a :class:`SignerCertificate` for one tenant's patch
   signing key. The certificate names the tenant only by an opaque pseudonym,
   bounds the key to a validity window, and lists the application families the
   key may sign patches for.
2. That tenant key signs the patch payload.

A receiving tenant pins the root public keys it trusts in a
:class:`FleetTrust`. It never trusts a key because a patch or a feed carries
it. Revoked signer keys are refused by key id.
"""

from __future__ import annotations

import hashlib
from base64 import b64decode, b64encode
from datetime import datetime, timezone
from typing import Final, Literal, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator

from openadapt_flow.fleet.fingerprint import APP_FAMILY_PATTERN
from openadapt_flow.qualification_admission_v2 import canonical_json

CERTIFICATE_DOMAIN: Final[bytes] = b"openadapt.fleet.signer-certificate/v1\x00"
PATCH_DOMAIN: Final[bytes] = b"openadapt.fleet.structural-patch/v1\x00"
PSEUDONYM_PATTERN: Final[str] = r"^[a-z0-9-]{8,64}$"


class PatchSignatureError(ValueError):
    """A certificate or patch signature failed. The patch is refused."""


def public_key_b64(
    key: Ed25519PrivateKey | Ed25519PublicKey,  # gitleaks:allow (type annotation)
) -> str:
    public = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    raw = public.public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return b64encode(raw).decode("ascii")


def key_id(public_b64: str) -> str:
    return hashlib.sha256(b64decode(public_b64, validate=True)).hexdigest()[:32]


def _load_public(value: str) -> Ed25519PublicKey:
    try:
        raw = b64decode(value, validate=True)
    except ValueError as exc:
        raise PatchSignatureError("public key is not base64") from exc
    if len(raw) != 32:
        raise PatchSignatureError("public key is not an Ed25519 key")
    return Ed25519PublicKey.from_public_bytes(raw)


def _utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PatchSignatureError("certificate time is not ISO 8601") from exc
    if parsed.tzinfo is None:
        raise PatchSignatureError("certificate time has no timezone")
    return parsed.astimezone(timezone.utc)


def _signature(value: str) -> bytes:
    try:
        raw = b64decode(value, validate=True)
    except ValueError as exc:
        raise PatchSignatureError("signature is not base64") from exc
    if len(raw) != 64:
        raise PatchSignatureError("signature has the wrong length")
    return raw


class SignerCertificatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.fleet.signer-certificate/v1"] = (
        "openadapt.fleet.signer-certificate/v1"
    )
    tenant_pseudonym: str = Field(pattern=PSEUDONYM_PATTERN)
    signer_public_key: str = Field(min_length=44, max_length=44)
    app_families: tuple[str, ...] = Field(min_length=1, max_length=64)
    not_before: str = Field(min_length=20, max_length=64)
    not_after: str = Field(min_length=20, max_length=64)
    root_key_id: str = Field(pattern=r"^[a-f0-9]{32}$")

    @field_validator("app_families")
    @classmethod
    def _families(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        import re

        if list(value) != sorted(set(value)):
            raise ValueError("certificate app families must be sorted and unique")
        for family in value:
            if not re.match(APP_FAMILY_PATTERN, family):
                raise ValueError(f"invalid app family {family!r}")
        return value


class SignerCertificate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    payload: SignerCertificatePayload
    signature: str = Field(min_length=1, max_length=128)

    @property
    def signer_key_id(self) -> str:
        return key_id(self.payload.signer_public_key)


def issue_certificate(
    root_key: Ed25519PrivateKey,
    *,
    tenant_pseudonym: str,
    signer_public_key: str,
    app_families: tuple[str, ...],
    not_before: datetime,
    not_after: datetime,
) -> SignerCertificate:
    payload = SignerCertificatePayload(
        tenant_pseudonym=tenant_pseudonym,
        signer_public_key=signer_public_key,
        app_families=tuple(sorted(set(app_families))),
        not_before=not_before.astimezone(timezone.utc).isoformat(),
        not_after=not_after.astimezone(timezone.utc).isoformat(),
        root_key_id=key_id(public_key_b64(root_key)),
    )
    signature = root_key.sign(CERTIFICATE_DOMAIN + canonical_json(payload))
    return SignerCertificate(
        payload=payload, signature=b64encode(signature).decode("ascii")
    )


class FleetTrust(BaseModel):
    """What one tenant trusts. Configured locally; never read from a feed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: key id -> base64 Ed25519 public key of each trusted fleet root.
    roots: dict[str, str] = Field(min_length=1)
    revoked_signer_key_ids: frozenset[str] = frozenset()

    @classmethod
    def pin(cls, *roots: Ed25519PublicKey | Ed25519PrivateKey) -> "FleetTrust":
        keys = {key_id(public_key_b64(r)): public_key_b64(r) for r in roots}
        return cls(roots=keys)


def verify_certificate(
    certificate: SignerCertificate,
    trust: FleetTrust,
    *,
    app_family: str,
    now: Optional[datetime] = None,
) -> None:
    """Refuse unless a pinned root signed this certificate for ``app_family``."""
    payload = certificate.payload
    root = trust.roots.get(payload.root_key_id)
    if root is None or key_id(root) != payload.root_key_id:
        raise PatchSignatureError("certificate is not signed by a trusted fleet root")
    try:
        _load_public(root).verify(
            _signature(certificate.signature),
            CERTIFICATE_DOMAIN + canonical_json(payload),
        )
    except InvalidSignature as exc:
        raise PatchSignatureError("certificate signature is invalid") from exc
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not (_utc(payload.not_before) <= current < _utc(payload.not_after)):
        raise PatchSignatureError("certificate is not valid at this time")
    if certificate.signer_key_id in trust.revoked_signer_key_ids:
        raise PatchSignatureError("signer key is revoked")
    if app_family not in payload.app_families:
        raise PatchSignatureError("certificate does not cover this application family")


def sign_bytes(key: Ed25519PrivateKey, message: bytes) -> str:
    return b64encode(key.sign(PATCH_DOMAIN + message)).decode("ascii")


def verify_bytes(public_b64: str, message: bytes, signature: str) -> None:
    try:
        _load_public(public_b64).verify(_signature(signature), PATCH_DOMAIN + message)
    except InvalidSignature as exc:
        raise PatchSignatureError("patch signature is invalid") from exc


__all__ = [
    "FleetTrust",
    "PatchSignatureError",
    "SignerCertificate",
    "SignerCertificatePayload",
    "issue_certificate",
    "key_id",
    "public_key_b64",
    "sign_bytes",
    "verify_bytes",
    "verify_certificate",
]

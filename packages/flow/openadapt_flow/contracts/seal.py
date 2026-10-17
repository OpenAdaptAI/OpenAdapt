"""Cloud issuer signatures on Seals (``openadapt.seal-signature/v1``).

A Seal is an :class:`~openadapt_flow.contracts.execute.ExecuteEvidenceReceiptV1`. Cloud
may attach ``seal_signature``: an Ed25519 signature by a published OpenAdapt
Cloud issuer key over the receipt's canonical sha256 (the receipt without its
``seal_signature``). Byte-compatible with Cloud ``src/lib/sealSignature.ts``.

Verification is fail closed: every problem is a refusal with a closed reason,
and an unsigned receipt is never "sealed". Verifying needs ``cryptography``
(install ``cryptography`` alongside); without it verification refuses.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, StrictStr

SEAL_SIGNATURE_SCHEMA = "openadapt.seal-signature/v1"
SEAL_ISSUER = "openadapt-cloud"
_DOMAIN = (SEAL_SIGNATURE_SCHEMA + "\0").encode("utf-8")
_SHA256_HEX = r"^[a-f0-9]{64}$"
_KEY_ID = r"^seal-ed25519-[a-f0-9]{16}$"
_ISO_SECONDS = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
_B64 = r"^[A-Za-z0-9+/]+={0,2}$"

SealRefusal = Literal[
    "unsigned",
    "malformed",
    "receipt_mismatch",
    "unknown_key",
    "revoked_key",
    "bad_signature",
    "verifier_unavailable",
]


class SealSignatureV1(BaseModel):
    """Issuer signature over one receipt hash."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_: Literal["openadapt.seal-signature/v1"] = Field(
        default=SEAL_SIGNATURE_SCHEMA, alias="schema"
    )
    issuer: Literal["openadapt-cloud"] = SEAL_ISSUER
    algorithm: Literal["ed25519"] = "ed25519"
    key_id: StrictStr = Field(pattern=_KEY_ID)
    receipt_sha256: StrictStr = Field(pattern=_SHA256_HEX)
    run_id: StrictStr = Field(min_length=1, max_length=128)
    issued_at: StrictStr = Field(pattern=_ISO_SECONDS)
    signature: StrictStr = Field(pattern=_B64)

    def wire(self) -> dict[str, str]:
        return self.model_dump(mode="json", by_alias=True)


@dataclass(frozen=True)
class SealPublicKey:
    key_id: str
    public_key: str  # base64 raw 32 bytes
    status: Literal["active", "revoked"]
    revoked_at: str | None = None


@dataclass(frozen=True)
class SealVerification:
    ok: bool
    key_id: str | None = None
    reason: SealRefusal | None = None


def canonical_json(value: Any) -> str:
    """Sorted-key compact JSON, matching Cloud's canonical form."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def receipt_sha256(receipt: Mapping[str, Any]) -> str:
    """Hash of a receipt's JSON without its ``seal_signature``."""
    body = {k: v for k, v in receipt.items() if k != "seal_signature"}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def seal_key_id(public_key_raw: bytes) -> str:
    return "seal-ed25519-" + hashlib.sha256(public_key_raw).hexdigest()[:16]


def signing_payload(signature: Mapping[str, Any]) -> bytes:
    unsigned = {k: v for k, v in signature.items() if k != "signature"}
    return _DOMAIN + canonical_json(unsigned).encode("utf-8")


def parse_key_registry(entries: Sequence[Mapping[str, Any]]) -> list[SealPublicKey]:
    keys: list[SealPublicKey] = []
    for entry in entries:
        raw = base64.b64decode(str(entry.get("public_key", "")), validate=True)
        if len(raw) != 32:
            raise ValueError("seal public key must be 32 raw bytes")
        if entry.get("key_id") != seal_key_id(raw):
            raise ValueError("seal key_id does not match its public key")
        status = entry.get("status")
        if status not in ("active", "revoked"):
            raise ValueError("seal key status must be active or revoked")
        revoked_at = entry.get("revoked_at")
        if status == "revoked" and not (
            isinstance(revoked_at, str) and re.match(_ISO_SECONDS, revoked_at)
        ):
            raise ValueError("a revoked seal key names revoked_at")
        keys.append(
            SealPublicKey(
                key_id=str(entry["key_id"]),
                public_key=base64.b64encode(raw).decode(),
                status=status,
                revoked_at=revoked_at if status == "revoked" else None,
            )
        )
    return keys


def verify_seal_signature(
    signature: Any,
    expected_receipt_sha256: str,
    registry: Sequence[SealPublicKey],
) -> SealVerification:
    """Verify a Seal signature against a pinned issuer key registry."""
    if signature is None:
        return SealVerification(False, reason="unsigned")
    try:
        parsed = (
            signature
            if isinstance(signature, SealSignatureV1)
            else SealSignatureV1.model_validate(signature)
        )
    except Exception:
        return SealVerification(False, reason="malformed")
    if parsed.receipt_sha256 != expected_receipt_sha256:
        return SealVerification(False, reason="receipt_mismatch")
    key = next((k for k in registry if k.key_id == parsed.key_id), None)
    if key is None:
        return SealVerification(False, reason="unknown_key")
    if key.status == "revoked" and (
        key.revoked_at is None or parsed.issued_at >= key.revoked_at
    ):
        return SealVerification(False, reason="revoked_key")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return SealVerification(False, reason="verifier_unavailable")
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(key.public_key)).verify(
            base64.b64decode(parsed.signature), signing_payload(parsed.wire())
        )
    except (InvalidSignature, ValueError):
        return SealVerification(False, reason="bad_signature")
    return SealVerification(True, key_id=key.key_id)


def verify_sealed_receipt(
    receipt: Mapping[str, Any], registry: Sequence[SealPublicKey]
) -> SealVerification:
    """Verify an Execute receipt (wire JSON) carries a valid issuer Seal."""
    return verify_seal_signature(
        receipt.get("seal_signature"), receipt_sha256(receipt), registry
    )

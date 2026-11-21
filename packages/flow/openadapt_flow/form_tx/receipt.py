"""Cryptographic Ed25519 value-scrubbed receipt issuance and verification for ``form_tx``.

Ensures raw PII and sensitive field values never enter ``FormTxReceiptPayload``:
only deterministic digests (``dossier_digest``, ``pack_digest``,
``human_resolution_digests``, and ``salted_output_digests``) are recorded and
signed over canonical JSON bytes.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import os
import secrets
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from openadapt_flow.form_tx.types import (
    FormPack,
    FormTxOutcome,
    FormTxReceiptPayload,
    HumanResolutionRecord,
    InputDossier,
    SignedFormTxReceipt,
    SummaryVerdict,
    canonical_json_bytes,
    canonical_sha256,
    salted_sha256,
)

__all__ = [
    "ed25519_public_key_hex",
    "generate_ed25519_keypair",
    "form_tx_receipt_format",
    "issue_form_tx_receipt",
    "load_or_create_ed25519_key",
    "verify_form_tx_receipt",
    "verify_salted_output_digest",
]


def generate_ed25519_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    """Generate a fresh in-memory Ed25519 ``(private_key, public_key)`` pair."""
    private_key = Ed25519PrivateKey.generate()
    return private_key, private_key.public_key()


def ed25519_public_key_hex(
    key: Ed25519PrivateKey | Ed25519PublicKey,
) -> str:
    """Return the raw 32-byte Ed25519 public key encoded as 64 lowercase hex chars."""
    pub = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    return pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()


def load_or_create_ed25519_key(path: Path | str) -> Ed25519PrivateKey:
    """Load an Ed25519 private key from ``path``, creating one with ``0o600`` mode if absent."""
    key_path = Path(path)
    if key_path.exists():
        raw = key_path.read_bytes()
        try:
            loaded = serialization.load_pem_private_key(raw, password=None)
        except ValueError:
            # Support raw 32-byte or 64-char hex seed files if provisioned externally.
            stripped = raw.strip()
            if len(stripped) == 64:
                try:
                    loaded = Ed25519PrivateKey.from_private_bytes(
                        bytes.fromhex(stripped.decode("ascii"))
                    )
                except (UnicodeDecodeError, ValueError) as exc:
                    raise ValueError(
                        f"Invalid Ed25519 key material at {key_path}"
                    ) from exc
            elif len(raw) == 32:
                loaded = Ed25519PrivateKey.from_private_bytes(raw)
            else:
                raise
        if not isinstance(loaded, Ed25519PrivateKey):
            raise ValueError(f"Key at {key_path} is not an Ed25519 private key")
        return loaded

    key_path.parent.mkdir(parents=True, exist_ok=True)
    private_key = Ed25519PrivateKey.generate()
    pem_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    fd, tmp_name = tempfile.mkstemp(
        dir=str(key_path.parent),
        prefix=f".{key_path.name}.",
    )
    try:
        os.chmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(pem_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, key_path)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
    return private_key


def _coerce_private_key(
    signing_key: Ed25519PrivateKey | tuple[Ed25519PrivateKey, Ed25519PublicKey],
) -> Ed25519PrivateKey:
    if isinstance(signing_key, tuple) and len(signing_key) == 2:
        signing_key = signing_key[0]
    if not isinstance(signing_key, Ed25519PrivateKey):
        raise TypeError("signing_key must be an Ed25519PrivateKey")
    return signing_key


def issue_form_tx_receipt(
    *,
    run_id: str,
    signing_key: Ed25519PrivateKey | tuple[Ed25519PrivateKey, Ed25519PublicKey],
    pack: Optional[FormPack] = None,
    dossier: Optional[InputDossier] = None,
    request_id: Optional[str] = None,
    pack_id: Optional[str] = None,
    pack_digest: Optional[str] = None,
    dossier_digest: Optional[str] = None,
    outcome: FormTxOutcome = FormTxOutcome.VERIFIED_DONE,
    fields_filled_auto: int = 0,
    human_resolutions: Sequence[HumanResolutionRecord] = (),
    commit_mutations_observed: int = 0,
    summary_verdict: Optional[SummaryVerdict] = None,
    readback_checked_count: Optional[int] = None,
    readback_passed: Optional[bool] = None,
    outputs: Optional[Mapping[str, Any]] = None,
    receipt_id: Optional[str] = None,
    salt_hex: Optional[str] = None,
    timestamp_utc: Optional[str] = None,
) -> SignedFormTxReceipt:
    """Issue a value-scrubbed, Ed25519-signed ``SignedFormTxReceipt``."""
    priv_key = _coerce_private_key(signing_key)

    resolved_request_id = request_id or (
        dossier.request_id if dossier is not None else None
    )
    resolved_pack_id = pack_id or (pack.pack_id if pack is not None else None)
    resolved_pack_digest = pack_digest or (pack.digest() if pack is not None else None)
    resolved_dossier_digest = dossier_digest or (
        dossier.digest() if dossier is not None else None
    )
    if (
        not resolved_request_id
        or not resolved_pack_id
        or not resolved_pack_digest
        or not resolved_dossier_digest
    ):
        raise ValueError(
            "issue_form_tx_receipt requires pack and dossier (or explicit "
            "request_id, pack_id, pack_digest, and dossier_digest)"
        )

    actual_salt = salt_hex if salt_hex is not None else secrets.token_hex(16)
    if not actual_salt:
        raise ValueError("salt_hex must be non-empty hex")
    try:
        salt_bytes = bytes.fromhex(actual_salt)
    except ValueError as exc:
        raise ValueError(
            f"salt_hex must be valid hexadecimal: {actual_salt!r}"
        ) from exc
    if len(salt_bytes) < 8:
        raise ValueError("salt_hex must represent at least 8 random bytes")

    effective_outputs: Mapping[str, Any]
    if outputs is not None:
        effective_outputs = outputs
    elif summary_verdict is not None:
        effective_outputs = summary_verdict.extracted_outputs
    else:
        effective_outputs = {}

    resolved_checked_count = (
        readback_checked_count
        if readback_checked_count is not None
        else (summary_verdict.checked_count if summary_verdict is not None else 0)
    )
    resolved_readback_passed = (
        readback_passed
        if readback_passed is not None
        else (summary_verdict.passed if summary_verdict is not None else False)
    )

    if outcome == FormTxOutcome.VERIFIED_DONE and (
        not resolved_readback_passed
        or resolved_checked_count < 1
        or (
            summary_verdict is not None
            and (
                not summary_verdict.passed
                or summary_verdict.failed_keys
                or summary_verdict.not_checked_keys
                or summary_verdict.missing_required_outputs
            )
        )
    ):
        raise ValueError(
            "Verified receipt requires successful nonempty read-back evidence"
        )
    # absent-evidence-as-success: counts are not evidence. A verified receipt
    # needs the per-field verdicts, every one of them passed, and a passing
    # verdict for every bound field the dossier has a value for: one passing
    # field out of several is partial, never verified.
    if outcome == FormTxOutcome.VERIFIED_DONE:
        if (
            summary_verdict is None
            or not summary_verdict.field_verdicts
            or any(not v.passed for v in summary_verdict.field_verdicts)
        ):
            raise ValueError(
                "Verified receipt requires per-field read-back verdicts, all passed"
            )
        if pack is not None and dossier is not None:
            passed_keys = {v.dossier_key for v in summary_verdict.field_verdicts}
            bound_with_value = {
                b.dossier_key
                for b in pack.bindings
                if dossier.facts.get(b.dossier_key) is not None
            }
            missing = sorted(bound_with_value - passed_keys)
            if missing:
                raise ValueError(
                    "Verified receipt requires every bound field with a value "
                    f"to pass read-back; missing: {missing}"
                )

    hr_digests = tuple(salted_sha256(rec, actual_salt) for rec in human_resolutions)
    salted_outputs = {
        str(k): salted_sha256(v, actual_salt)
        for k, v in sorted(effective_outputs.items(), key=lambda item: str(item[0]))
    }

    payload = FormTxReceiptPayload(
        receipt_id=receipt_id or f"rcpt_{uuid.uuid4().hex}",
        run_id=run_id,
        request_id=resolved_request_id,
        pack_id=resolved_pack_id,
        pack_digest=resolved_pack_digest,
        dossier_digest=resolved_dossier_digest,
        outcome=outcome,
        timestamp_utc=timestamp_utc
        or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        salt_hex=actual_salt,
        fields_filled_auto=fields_filled_auto,
        human_resolutions_count=len(human_resolutions),
        human_resolution_digests=hr_digests,
        commit_mutations_observed=commit_mutations_observed,
        readback_checked_count=resolved_checked_count,
        readback_passed=resolved_readback_passed,
        readback_not_checked_count=(
            len(summary_verdict.not_checked_keys) if summary_verdict is not None else 0
        ),
        salted_output_digests=salted_outputs,
        readback_evidence=(
            summary_verdict.readback_evidence
            if summary_verdict is not None
            else "acting_session_reread"
        ),
    )

    payload_bytes = canonical_json_bytes(payload)
    payload_digest = canonical_sha256(payload)
    signature_bytes = priv_key.sign(payload_bytes)
    pub_hex = ed25519_public_key_hex(priv_key)

    return SignedFormTxReceipt(
        payload=payload,
        payload_sha256=payload_digest,
        algorithm="Ed25519",
        public_key_hex=pub_hex,
        signature_hex=signature_bytes.hex(),
    )


# Fields added to the v1 payload after v1 receipts were first signed on main,
# with the value they read as when absent. A receipt signed before a field
# existed was signed over the payload without it.
_LATER_V1_FIELDS: dict[str, Any] = {
    "readback_not_checked_count": 0,
    "readback_evidence": "acting_session_reread",
}

RECEIPT_FORMAT_CURRENT = "v1"
RECEIPT_FORMAT_OLDER = "v1, older (signed before: {fields})"


def _signed_encodings(payload: Any) -> list[tuple[str, bytes]]:
    """The current encoding, then each older one a receipt may have been signed in.

    An older receipt lacks fields added later; those read as their default,
    so each subset of later fields that hold their default is tried absent.
    """
    out = [(RECEIPT_FORMAT_CURRENT, canonical_json_bytes(payload))]
    data = payload.model_dump(mode="json")
    defaulted = [k for k, d in _LATER_V1_FIELDS.items() if k in data and data[k] == d]
    for size in range(1, len(defaulted) + 1):
        for absent in itertools.combinations(defaulted, size):
            older = {k: v for k, v in data.items() if k not in absent}
            out.append(
                (
                    RECEIPT_FORMAT_OLDER.format(fields=", ".join(absent)),
                    canonical_json_bytes(older),
                )
            )
    return out


def verify_form_tx_receipt(
    receipt: SignedFormTxReceipt,
    *,
    expected_public_key_hex: Optional[str] = None,
) -> bool:
    """Verify ``payload_sha256`` and Ed25519 signature over canonical payload JSON.

    Returns ``False`` on any digest mismatch, malformed hex, key mismatch, or
    single-bit payload/signature tamper. A v1 receipt signed before later
    optional fields existed still verifies; ``form_tx_receipt_format`` says
    which format it is.
    """
    return (
        form_tx_receipt_format(receipt, expected_public_key_hex=expected_public_key_hex)
        is not None
    )


def form_tx_receipt_format(
    receipt: SignedFormTxReceipt,
    *,
    expected_public_key_hex: Optional[str] = None,
) -> Optional[str]:
    """The format a receipt verifies under, or ``None`` when it does not verify."""
    if not isinstance(receipt, SignedFormTxReceipt):
        return None
    if receipt.algorithm != "Ed25519":
        return None
    if expected_public_key_hex is not None and not hmac.compare_digest(
        receipt.public_key_hex.lower(),
        expected_public_key_hex.lower(),
    ):
        return None

    try:
        pub_bytes = bytes.fromhex(receipt.public_key_hex)
        sig_bytes = bytes.fromhex(receipt.signature_hex)
    except ValueError:
        return None

    if len(pub_bytes) != 32 or len(sig_bytes) != 64:
        return None

    try:
        public_key = Ed25519PublicKey.from_public_bytes(pub_bytes)
    except ValueError:
        return None
    for label, encoded in _signed_encodings(receipt.payload):
        if not hmac.compare_digest(
            receipt.payload_sha256, hashlib.sha256(encoded).hexdigest()
        ):
            continue
        try:
            public_key.verify(sig_bytes, encoded)
        except (InvalidSignature, ValueError):
            continue
        return label
    return None


def verify_salted_output_digest(
    receipt: SignedFormTxReceipt,
    output_key: str,
    candidate_value: Any,
) -> bool:
    """Verify receipt signature and test whether ``candidate_value`` matches ``output_key``."""
    if not verify_form_tx_receipt(receipt):
        return False
    recorded_digest = receipt.payload.salted_output_digests.get(output_key)
    if recorded_digest is None:
        return False
    candidate_digest = salted_sha256(candidate_value, receipt.payload.salt_hex)
    return hmac.compare_digest(recorded_digest, candidate_digest)

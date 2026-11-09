"""Seal issuer signatures: wire compatibility with Cloud and fail-closed checks."""

import base64
import json
from pathlib import Path

import pytest

from openadapt_types import ExecuteEvidenceReceiptV1
from openadapt_types.seal import (
    SealSignatureV1,
    parse_key_registry,
    receipt_sha256,
    seal_key_id,
    signing_payload,
    verify_seal_signature,
    verify_sealed_receipt,
)

ed25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
from cryptography.hazmat.primitives import serialization  # noqa: E402

VECTOR = json.loads((Path(__file__).parent / "fixtures" / "cloud-seal-signature-vector.json").read_text())


def _key():
    private = ed25519.Ed25519PrivateKey.generate()
    raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    entry = {"key_id": seal_key_id(raw), "public_key": base64.b64encode(raw).decode(), "status": "active"}
    return private, entry


def _sign(private, entry, sha, run_id="exec-0001-test", issued_at="2026-10-14T12:00:00Z"):
    fields = {
        "schema": "openadapt.seal-signature/v1",
        "issuer": "openadapt-cloud",
        "algorithm": "ed25519",
        "key_id": entry["key_id"],
        "receipt_sha256": sha,
        "run_id": run_id,
        "issued_at": issued_at,
    }
    fields["signature"] = base64.b64encode(private.sign(signing_payload(fields))).decode()
    return fields


def test_cloud_typescript_vector_verifies_in_python() -> None:
    registry = parse_key_registry(VECTOR["registry"])
    result = verify_seal_signature(VECTOR["signature"], "c" * 64, registry)
    assert result.ok and result.key_id == VECTOR["registry"][0]["key_id"]
    tampered = dict(VECTOR["signature"], run_id="exec-0002-test")
    assert verify_seal_signature(tampered, "c" * 64, registry).reason == "bad_signature"


def test_refusals_are_closed() -> None:
    private, entry = _key()
    registry = parse_key_registry([entry])
    sig = _sign(private, entry, "a" * 64)
    assert verify_seal_signature(None, "a" * 64, registry).reason == "unsigned"
    assert verify_seal_signature(dict(sig, extra=1), "a" * 64, registry).reason == "malformed"
    assert verify_seal_signature(sig, "b" * 64, registry).reason == "receipt_mismatch"
    assert verify_seal_signature(sig, "a" * 64, []).reason == "unknown_key"
    revoked = parse_key_registry([dict(entry, status="revoked", revoked_at="2026-01-01T00:00:00Z")])
    assert verify_seal_signature(sig, "a" * 64, revoked).reason == "revoked_key"


def test_receipt_field_is_optional_and_hash_excludes_it() -> None:
    fields = ExecuteEvidenceReceiptV1.model_fields
    assert "seal_signature" in fields and not fields["seal_signature"].is_required()
    receipt = {"a": 1, "b": "é"}
    assert receipt_sha256(receipt) == receipt_sha256({**receipt, "seal_signature": {"x": 1}})


def test_sealed_receipt_round_trip() -> None:
    private, entry = _key()
    registry = parse_key_registry([entry])
    receipt = {"schema_version": "openadapt.execute-evidence-receipt/v1", "execution_id": "exec-0001-test"}
    sealed = {**receipt, "seal_signature": _sign(private, entry, receipt_sha256(receipt))}
    assert verify_sealed_receipt(sealed, registry).ok
    assert not verify_sealed_receipt({**sealed, "execution_id": "exec-9999-test"}, registry).ok
    assert verify_sealed_receipt(receipt, registry).reason == "unsigned"
    assert SealSignatureV1.model_validate(sealed["seal_signature"]).wire() == sealed["seal_signature"]


def test_registry_rejects_mislabeled_keys() -> None:
    _, entry = _key()
    with pytest.raises(ValueError):
        parse_key_registry([dict(entry, key_id="seal-ed25519-0000000000000000")])

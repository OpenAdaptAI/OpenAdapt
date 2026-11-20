"""Public artifacts cannot disclose low-entropy values through derived hashes."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openadapt_flow import crypto
from openadapt_flow.receipt_log import (
    ALG,
    CTY,
    EDDSA,
    KID,
    RECEIPTS,
    LocalLog,
    encode_sign1,
    key_id,
)
from openadapt_flow.tx.receipt import (
    TX_RECEIPT_CONTENT_TYPE,
    audit_legacy_tx_receipt,
    issue_tx_receipt,
    read_private_record,
    verify_tx_receipt,
)
from openadapt_flow.tx.types import canonical_sha256
from openadapt_flow.tx.wire_tap import SessionWireTap, WirePolicy


@pytest.fixture(autouse=True)
def private_test_state(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENADAPT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OPENADAPT_FLOW_AT_REST_HOME", str(tmp_path / "at-rest"))
    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", "synthetic-" + "receipt-fixture-only")


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _sign_variant(payload, root: Path, key: Ed25519PrivateKey) -> Path:
    """Produce genuinely signed/logged adversarial inputs, not signature errors."""
    from cryptography.hazmat.primitives import serialization

    public_raw = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    protected = {
        ALG: EDDSA,
        CTY: TX_RECEIPT_CONTENT_TYPE,
        KID: key_id(public_raw),
    }
    body = _json_bytes(payload)
    bare = encode_sign1(protected, {}, body, key.sign)
    proof = LocalLog(home=root).append(bare)
    encoded = encode_sign1(protected, {RECEIPTS: [proof]}, body, key.sign)
    receipt = root / "artifact" / "tx-receipt.cose"
    receipt.parent.mkdir(parents=True)
    receipt.write_bytes(encoded)
    return receipt


def _dictionary_matches(har, published_commitment, dictionary):
    """Attacker input excludes private summary, original bodies and all keys."""
    entry = har["log"]["entries"][0]
    request, response = entry["request"], entry["response"]
    request_shape = json.loads(request["postData"]["text"])
    response_shape = json.loads(response["content"]["text"])
    matches = []
    for candidate in dictionary:
        guessed_request = dict(request_shape)
        guessed_response = dict(response_shape)
        for shape in (guessed_request, guessed_response):
            if shape["pin"] != "ordinary":
                shape["pin"] = candidate
        record = {
            "seq": 1,
            "method": request["method"],
            "url": request["url"],
            "path": urlsplit(request["url"]).path,
            "request_body_sha256": hashlib.sha256(
                _json_bytes(guessed_request)
            ).hexdigest(),
            "response_status": response["status"],
            "response_body_sha256": hashlib.sha256(
                _json_bytes(guessed_response)
            ).hexdigest(),
            "extracted_entity_id": guessed_response["id"],
            "etag": None,
            "version": None,
            "blocked": False,
            "block_reason": "",
            "is_graphql_mutation": False,
            "timestamp_utc": entry["startedDateTime"],
        }
        if canonical_sha256([record]) == published_commitment:
            matches.append(candidate)
    return matches


@pytest.mark.parametrize("location", ["request", "response", "both"])
def test_public_har_and_receipt_do_not_enable_request_or_response_dictionary(
    tmp_path, location
):
    secret = "cedar"  # Synthetic dictionary entry, never a credential.
    response_value = secret if location in {"response", "both"} else "ordinary"
    response_body = _json_bytes({"id": "row1", "pin": response_value})

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        upstream = f"http://127.0.0.1:{server.server_port}"
        with SessionWireTap(upstream, WirePolicy(secrets=(secret,))) as tap:
            proxy = urlsplit(tap.base_url)
            connection = http.client.HTTPConnection(
                proxy.hostname, proxy.port, timeout=5
            )
            request_value = secret if location in {"request", "both"} else "ordinary"
            connection.request(
                "POST",
                "/records",
                body=_json_bytes({"pin": request_value}),
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            assert response.status == 200 and response.read() == response_body
            connection.close()
            har, private_summary = tap.export_har(), tap.summary()
            key = Ed25519PrivateKey.generate()
            _, receipt = issue_tx_receipt(
                tmp_path / "issuer",
                task_id=secret,
                entity_name=secret,
                operation="create",
                outcome="UNCERTAIN",
                intended_delta={"pin": secret},
                r0={"pin": secret},
                r1={"pin": secret},
                wire_summary=private_summary,
                private_key=key,
            )
            public = verify_tx_receipt(receipt, key.public_key())

        dictionary = ["maple", "cedar", "orchid"]
        assert secret not in json.dumps(har)
        assert secret.encode() not in receipt.read_bytes()
        assert public["schema_version"] == "openadapt.tx-receipt/v3"
        assert public["commitment_algorithm"] == "HMAC-SHA-256"
        assert public["commitment_profile"] == "openadapt.tx-public-commitments/v1"
        assert "wire_ledger_sha256" not in public and "entity_name" not in public
        assert (
            _dictionary_matches(har, public["wire_ledger_hmac_sha256"], dictionary)
            == []
        )
        # The attack really works if the old raw digest is exposed. This private
        # control is not published by the current receipt or supplied above.
        assert _dictionary_matches(har, private_summary.ledger_sha256, dictionary) == [
            secret
        ]

        key_file = receipt.parent / "salt.bin"
        encrypted = key_file.read_bytes()
        assert crypto.is_encrypted(encrypted)
        assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
        private_key = read_private_record(key_file)
        assert len(private_key) == 32 and private_key not in receipt.read_bytes()
        domains = {
            "task_id_hmac_sha256": ("task-id", secret),
            "entity_name_hmac_sha256": ("entity-name", secret),
            "intended_delta_hmac_sha256": ("intended-delta", {"pin": secret}),
            "r0_hmac_sha256": ("record-before", {"pin": secret}),
            "r1_hmac_sha256": ("record-after", {"pin": secret}),
            "wire_ledger_hmac_sha256": ("wire-ledger", private_summary.ledger_sha256),
        }
        for field, (domain, value) in domains.items():
            expected = hmac.new(
                private_key,
                _json_bytes(
                    {
                        "profile": public["commitment_profile"],
                        "domain": domain,
                        "value": value,
                    }
                ),
                hashlib.sha256,
            ).hexdigest()
            assert public[field] == expected
        assert public["task_id_hmac_sha256"] != public["entity_name_hmac_sha256"]
        assert (
            len(
                {
                    public[field]
                    for field in (
                        "intended_delta_hmac_sha256",
                        "r0_hmac_sha256",
                        "r1_hmac_sha256",
                    )
                }
            )
            == 3
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize(
    "case",
    [
        "wrong_algorithm",
        "missing_algorithm",
        "wrong_profile",
        "missing_profile",
        "raw_wire_digest",
        "raw_label",
        "unknown_field",
        "uppercase_digest",
        "numeric_digest",
        "future_schema",
        "non_string_schema",
        "missing_schema",
        "wire_counts_without_commitment",
        "invalid_timestamp",
        "boolean_count",
        "fractional_count",
        "numeric_collateral_verified",
    ],
)
def test_signed_wrong_algorithm_unknown_or_legacy_fields_refuse_current_verification(
    tmp_path, case
):
    key = Ed25519PrivateKey.generate()
    _, receipt = issue_tx_receipt(
        tmp_path / "current",
        task_id="task",
        entity_name="label",
        operation="update",
        outcome="UNCERTAIN",
        private_key=key,
    )
    payload = verify_tx_receipt(receipt, key.public_key())
    if case == "wrong_algorithm":
        payload["commitment_algorithm"] = "SHA-256"
    elif case == "missing_algorithm":
        payload.pop("commitment_algorithm")
    elif case == "wrong_profile":
        payload["commitment_profile"] = "unknown/v1"
    elif case == "missing_profile":
        payload.pop("commitment_profile")
    elif case == "raw_wire_digest":
        payload["wire_ledger_sha256"] = "a" * 64
    elif case == "raw_label":
        payload["entity_name"] = "label"
    elif case == "unknown_field":
        payload["candidate_approved"] = True
    elif case == "uppercase_digest":
        payload["task_id_hmac_sha256"] = "A" * 64
    elif case == "numeric_digest":
        payload["task_id_hmac_sha256"] = 0
    elif case == "future_schema":
        payload["schema_version"] = "openadapt.tx-receipt/v99"
    elif case == "non_string_schema":
        payload["schema_version"] = {"version": 3}
    elif case == "missing_schema":
        payload.pop("schema_version")
    elif case == "wire_counts_without_commitment":
        payload["wire_mutations_allowed"] = 1
    elif case == "invalid_timestamp":
        payload["issued_at"] = "sensitive-label"
    elif case == "boolean_count":
        payload["rows_read"] = True
    elif case == "fractional_count":
        payload["rows_read"] = 1.5
    elif case == "numeric_collateral_verified":
        payload["collateral_effects_verified"] = 0
    altered = _sign_variant(payload, tmp_path / "variant", key)
    with pytest.raises(ValueError, match="invalid current tx-receipt payload"):
        verify_tx_receipt(altered, key.public_key())


@pytest.mark.parametrize(
    "schema", ["openadapt.tx-receipt/v1", "openadapt.tx-receipt/v2"]
)
def test_legacy_integrity_audit_retains_bytes_without_exporting_current_trust_or_values(
    tmp_path, schema
):
    key = Ed25519PrivateKey.generate()
    secret = "historical-sensitive-label"
    legacy = {
        "schema_version": schema,
        "task_id": secret,
        "entity_name": secret,
        "outcome": "CONFIRMED",
        "qualification_state": "atomic_row_enforced",
    }
    root = tmp_path / "legacy"
    receipt = _sign_variant(legacy, root, key)
    salt = receipt.parent / "salt.bin"
    salt.write_bytes(bytes(range(32)))
    original_receipt, original_salt = receipt.read_bytes(), salt.read_bytes()
    with pytest.raises(ValueError, match="legacy_tx_receipt_is_integrity_audit_only"):
        verify_tx_receipt(receipt, key.public_key())
    audit = audit_legacy_tx_receipt(receipt, key.public_key())
    assert audit["signature_valid"] and audit["inclusion_verified"]
    assert audit["privacy_safe"] is False and audit["current_schema_accepted"] is False
    assert audit["source_schema"] == schema
    assert not (
        {"outcome", "qualification_state", "task_id", "entity_name", "payload"}
        & set(audit)
    )
    assert secret not in json.dumps(audit)
    assert (
        receipt.read_bytes() == original_receipt and salt.read_bytes() == original_salt
    )
    with pytest.raises(ValueError):
        audit_legacy_tx_receipt(receipt, Ed25519PrivateKey.generate().public_key())
    changed = bytearray(receipt.read_bytes())
    changed[-1] ^= 1
    receipt.write_bytes(changed)
    with pytest.raises(ValueError):
        audit_legacy_tx_receipt(receipt, key.public_key())


def test_per_receipt_key_rotation_keeps_equal_values_unlinkable_and_legacy_audit_separate(
    tmp_path,
):
    key = Ed25519PrivateKey.generate()
    values = []
    for _ in range(2):
        _, receipt = issue_tx_receipt(
            tmp_path / "same-root",
            task_id="same-task",
            entity_name="same-label",
            operation="update",
            outcome="UNCERTAIN",
            private_key=key,
        )
        values.append(verify_tx_receipt(receipt, key.public_key()))
        with pytest.raises(ValueError, match="requires_known_legacy_schema"):
            audit_legacy_tx_receipt(receipt, key.public_key())
    assert values[0]["task_id_hmac_sha256"] != values[1]["task_id_hmac_sha256"]
    assert values[0]["entity_name_hmac_sha256"] != values[1]["entity_name_hmac_sha256"]

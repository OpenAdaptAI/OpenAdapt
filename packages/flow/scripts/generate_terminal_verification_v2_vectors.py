#!/usr/bin/env python3
"""Regenerate the deterministic non-success terminal-v2 test vectors."""

from __future__ import annotations

import argparse
import hashlib
import json
from base64 import b64encode
from pathlib import Path

from cryptography.hazmat.primitives import serialization

from openadapt_flow.qualification_admission_v2 import canonical_json
from openadapt_flow.runner.hosted_adapter import (
    HostedTerminalEventV2,
)
from openadapt_flow.terminal_verification_v2 import (
    RESULT_LOSS_CLOSURE_PAYLOAD_DOMAIN,
    RESULT_LOSS_CLOSURE_REQUEST_DOMAIN,
    SIGNATURE_DOMAIN_V3,
    evidence_runner_key_id,
    evidence_runner_signer_sha256,
    sign_production_terminal_verification,
)
from tests.test_terminal_verification_v2 import (
    _acknowledged_reconciliation_payload,
    _halted_payload,
    _managed_result_loss_acknowledged_payload,
    _managed_result_loss_payload,
    _payload,
    _private_key,
    _reconciliation_payload,
    _result_loss_request,
)

DEFAULT_OUTPUT = Path("tests/fixtures/terminal_verification_v2_terminal_vectors.json")
DEFAULT_CONSUMER_OUTPUT = Path(
    "tests/fixtures/terminal_verification_v2_terminal_consumer_vectors.json"
)
DELIVERY_INPUT = Path("tests/fixtures/terminal_verification_v2_delivery_vector.json")
DEFAULT_DELIVERY_CONSUMER_OUTPUT = Path(
    "tests/fixtures/terminal_verification_v2_delivery_consumer_vector.json"
)


def consumer_fixture(fixture: dict[str, object]) -> dict[str, object]:
    """Remove producer-only signing material from a cross-language fixture."""

    if "private_key_base64" not in fixture:
        raise ValueError("producer fixture has no private signing key")
    return {key: value for key, value in fixture.items() if key != "private_key_base64"}


def _vector(name: str) -> dict[str, object]:
    payload = {
        "verified-complete": _payload,
        "halted-before-effect-zero-permit": _halted_payload,
        "reconciliation-required-pending-permit": _reconciliation_payload,
        "reconciliation-required-acknowledged-inconclusive": (
            _acknowledged_reconciliation_payload
        ),
        "reconciliation-required-managed-result-loss": _managed_result_loss_payload,
        "reconciliation-required-managed-result-loss-acknowledged": (
            _managed_result_loss_acknowledged_payload
        ),
    }[name]()
    envelope = sign_production_terminal_verification(payload, _private_key())
    raw = canonical_json(envelope)
    effect_records = payload.evidence_manifests.effect.records
    callback = HostedTerminalEventV2(
        run_id=payload.run_id,
        outcome=payload.run_receipt.transaction_outcome,
        report_sha256=payload.run_report_sha256,
        started=True,
        uncertain_delivery=payload.pending_permit_count == 1,
        terminal_verification_artifact_bytes_base64=b64encode(raw).decode("ascii"),
        terminal_verification_artifact_sha256=envelope.artifact_sha256(),
    )
    return {
        "callback": callback.model_dump(mode="json"),
        "effect_state": (
            effect_records[0].model_dump(mode="json") if effect_records else None
        ),
        "envelope_canonical_base64": b64encode(raw).decode("ascii"),
        "name": name,
        "payload_canonical_sha256": hashlib.sha256(
            payload.canonical_bytes()
        ).hexdigest(),
        "signature": envelope.signature,
        "terminal_verification_artifact_sha256": envelope.artifact_sha256(),
        "managed_result_loss": (
            payload.managed_result_loss.model_dump(mode="json")
            if payload.managed_result_loss is not None
            else None
        ),
    }


def _result_loss_closure_vector(payload) -> dict[str, object]:
    request = _result_loss_request()
    closure = payload.delivery_result_loss_closure
    assert closure is not None
    closure_raw = canonical_json(closure)
    chain_raw = canonical_json(payload.permit_chain)
    closure_result = {
        "schema_version": (
            "openadapt.production-delivery-result-loss-closure-result/v2"
        ),
        "status": "closed",
        "closure_artifact_bytes_base64": b64encode(closure_raw).decode("ascii"),
        "closure_artifact_sha256": closure.artifact_sha256(),
        "permit_chain_bytes_base64": b64encode(chain_raw).decode("ascii"),
        "permit_chain_sha256": payload.permit_chain.permit_chain_sha256,
    }
    return {
        "http_method": "POST",
        "http_route": "/api/internal/managed-delivery-result-loss-closure",
        "authorization_credential_source": "hosted_dispatch.lease_token",
        "request_digest_domain_base64": b64encode(
            RESULT_LOSS_CLOSURE_REQUEST_DOMAIN
        ).decode("ascii"),
        "request_canonical_base64": b64encode(request.canonical_bytes()).decode(
            "ascii"
        ),
        "request_sha256": request.request_sha256(),
        "payload_signature_domain_base64": b64encode(
            RESULT_LOSS_CLOSURE_PAYLOAD_DOMAIN
        ).decode("ascii"),
        "closure_payload_sha256": closure.payload_sha256,
        "closure_artifact_canonical_base64": b64encode(closure_raw).decode("ascii"),
        "closure_artifact_sha256": closure.artifact_sha256(),
        "permit_chain_canonical_base64": b64encode(chain_raw).decode("ascii"),
        "permit_chain_sha256": payload.permit_chain.permit_chain_sha256,
        "result_canonical_base64": b64encode(canonical_json(closure_result)).decode(
            "ascii"
        ),
    }


def build_fixture() -> dict[str, object]:
    private_key = _private_key()
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    managed = _managed_result_loss_payload()
    managed_acknowledged = _managed_result_loss_acknowledged_payload()
    return {
        "key_id": evidence_runner_key_id(public_bytes),
        "private_key_base64": b64encode(private_bytes).decode("ascii"),
        "public_key_base64": b64encode(public_bytes).decode("ascii"),
        "schema_version": "openadapt.production-terminal-cross-language-vectors/v3",
        "signature_domain_base64": b64encode(SIGNATURE_DOMAIN_V3).decode("ascii"),
        "signer_sha256": evidence_runner_signer_sha256(public_bytes),
        "managed_result_loss_closure_vector": _result_loss_closure_vector(managed),
        "managed_result_loss_acknowledged_closure_vector": (
            _result_loss_closure_vector(managed_acknowledged)
        ),
        "vectors": [
            _vector("verified-complete"),
            _vector("halted-before-effect-zero-permit"),
            _vector("reconciliation-required-pending-permit"),
            _vector("reconciliation-required-acknowledged-inconclusive"),
            _vector("reconciliation-required-managed-result-loss"),
            _vector("reconciliation-required-managed-result-loss-acknowledged"),
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--consumer-output", type=Path, default=DEFAULT_CONSUMER_OUTPUT)
    parser.add_argument("--delivery-input", type=Path, default=DELIVERY_INPUT)
    parser.add_argument(
        "--delivery-consumer-output",
        type=Path,
        default=DEFAULT_DELIVERY_CONSUMER_OUTPUT,
    )
    args = parser.parse_args()
    fixture = build_fixture()
    args.output.write_text(
        json.dumps(fixture, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    args.consumer_output.write_text(
        json.dumps(consumer_fixture(fixture), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    delivery_fixture = json.loads(args.delivery_input.read_text(encoding="utf-8"))
    if not isinstance(delivery_fixture, dict):
        raise ValueError("delivery producer fixture is not an object")
    args.delivery_consumer_output.write_text(
        json.dumps(consumer_fixture(delivery_fixture), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()

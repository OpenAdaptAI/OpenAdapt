"""Reproduce the frozen vector from explicit, non-live test-double inputs.

This never reads the existing vector. Regeneration is an explicit maintenance
operation, separate from the tests that compare against its frozen bytes.
"""

from __future__ import annotations

import json
from typing import Any

from openadapt_flow.approval_mode.named_person_binding import (
    BindingMethod,
    DeterministicPasskeyDouble,
    RegisteredPasskey,
    approval_payload_digest,
    build_binding,
)


def build_synthetic_fixture() -> dict[str, Any]:
    registered = RegisteredPasskey(
        person_id="person:alice",
        credential_id=b"cred-alice-1",
        public_key=b"P" * 32,
        rp_id="approve.openadapt.test",
    )
    digest = approval_payload_digest(
        {
            "request_id": "req-1",
            "workflow": "referral",
            "decision": "approve",
            "fields": ["status"],
        }
    )
    assertion = DeterministicPasskeyDouble().sign_webauthn(
        challenge_sha256=digest, registered=registered
    )
    binding = build_binding(
        person_id=registered.person_id,
        method=BindingMethod.WEBAUTHN_PASSKEY,
        approval_payload_sha256=digest,
        assertion=assertion,
        rp_id=registered.rp_id,
        verified_at="2026-11-05T17:00:00Z",
    )
    return {
        "note": "Synthetic DeterministicPasskeyDouble vector; NOT a live WebAuthn ceremony.",
        "approval_payload_sha256": digest,
        "registered_passkey": {
            "person_id": registered.person_id,
            "credential_id_hex": registered.credential_id.hex(),
            "public_key_hex": registered.public_key.hex(),
            "rp_id": registered.rp_id,
        },
        "assertion_hex": {
            "credential_id": assertion.credential_id.hex(),
            "authenticator_data": assertion.authenticator_data.hex(),
            "client_data_json": assertion.client_data_json.decode("utf-8"),
            "signature": assertion.signature.hex(),
        },
        "binding": binding.model_dump(mode="json"),
    }


def encode_synthetic_fixture() -> bytes:
    return (
        json.dumps(build_synthetic_fixture(), indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")

"""Frozen format compatibility and provenance for the synthetic vector."""

from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest

from tests._named_person_fixture_source import (
    build_synthetic_fixture,
    encode_synthetic_fixture,
)

FLOW = Path(__file__).resolve().parents[1]
REPO = FLOW.parents[1]
CANDIDATE = (
    "packages/flow/tests/fixtures/named_person_binding/webauthn_passkey_double.json"
)


@pytest.fixture(scope="module")
def guard():
    return runpy.run_path(str(REPO / "scripts/check_evidence_keys.py"))


def test_frozen_vector_matches_actual_producer():
    # The frozen file is an independent, checked-in oracle. Tests never rewrite it.
    assert (REPO / CANDIDATE).read_bytes() == encode_synthetic_fixture()


def test_generation_is_repeatable_and_detached():
    first = encode_synthetic_fixture()
    changed = build_synthetic_fixture()
    changed["registered_passkey"]["public_key_hex"] = "00" * 32
    assert encode_synthetic_fixture() == first
    assert (
        build_synthetic_fixture()["registered_passkey"]["public_key_hex"] == "50" * 32
    )


def test_frozen_vector_passes_existing_opaque_candidate_guard(guard):
    assert guard["tracked_key_path_kind"](CANDIDATE) == "synthetic_assertion_candidate"
    assert guard["violations"](REPO, [CANDIDATE]) == []


def test_generated_contract_has_only_explicit_synthetic_fields():
    data = build_synthetic_fixture()
    assert set(data) == {
        "note",
        "approval_payload_sha256",
        "registered_passkey",
        "assertion_hex",
        "binding",
    }
    assert data["registered_passkey"] == {
        "person_id": "person:alice",
        "credential_id_hex": b"cred-alice-1".hex(),
        "public_key_hex": (b"P" * 32).hex(),
        "rp_id": "approve.openadapt.test",
    }
    client = json.loads(data["assertion_hex"]["client_data_json"])
    assert client["fixture"] == "openadapt.named-person.webauthn-double/v1"
    assert client["origin"] == "https://approve.openadapt.test"
    assert data["binding"]["verified_at"] == "2026-11-05T17:00:00Z"


@pytest.mark.parametrize(
    "scope, field, value",
    [
        (None, "extra", "not-permitted"),
        ("registered_passkey", "extra", "not-permitted"),
        ("assertion_hex", "extra", "not-permitted"),
        ("registered_passkey", "rp_id", "approve.example.com"),
        ("registered_passkey", "public_key_hex", "50" * 31),
        ("assertion_hex", "signature", "00" * 31),
        (None, "note", "live assertion"),
    ],
)
def test_changed_generated_data_still_refuses_closed_guard(guard, scope, field, value):
    data = build_synthetic_fixture()
    selected = data if scope is None else data[scope]
    selected[field] = value
    assert guard["synthetic_assertion_refusal"](json.dumps(data)) is not None

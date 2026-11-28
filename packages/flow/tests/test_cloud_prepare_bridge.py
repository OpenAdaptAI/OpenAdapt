"""Unit and mock-HTTP tests for the opt-in zero-PHI Cloud Prepare bridge."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow.cloud_prepare_bridge import (
    CloudPrepareClient,
    ZeroPhiViolationError,
    compute_resolution_sha256,
    form_halt_to_prepare_card,
    prepare_resolution_to_human_input,
)
from openadapt_flow.copilot_protocol import (
    BoundDecisionStore,
    CopilotRunBinding,
    FormHaltItem,
    FormHaltKind,
    HumanDecisionAction,
    ObservedOption,
)


def _make_choice_halt(**overrides: Any) -> FormHaltItem:
    defaults: dict[str, Any] = {
        "halt_id": "halt_cov_001",
        "kind": FormHaltKind.AMBIGUOUS_OPTION_OR_TARGET,
        "screen_id": "claim_step_2",
        "control_id": "coverage_type",
        "dom_selector": "select[name='coverage_type']",
        "field_label": "Coverage Type",
        "dossier_key": "coverage_tier",
        "portal_value": "123-45-6789",
        "dossier_value": "patient@example.com",
        "message": "Multiple coverage options match the current claim.",
        "available_options": (
            ObservedOption(value="PRI", label="Primary Dental"),
            ObservedOption(value="SEC", label="Secondary COB"),
        ),
    }
    defaults.update(overrides)
    return FormHaltItem(**defaults)


def _make_typed_halt(**overrides: Any) -> FormHaltItem:
    defaults: dict[str, Any] = {
        "halt_id": "halt_auth_002",
        "kind": FormHaltKind.UNGROUNDED_REQUIRED_FIELD,
        "screen_id": "claim_step_3",
        "control_id": "preauth_number",
        "dom_selector": "input[name='preauth_code']",
        "field_label": "Pre-Authorization Code",
        "dossier_key": "preauth_code",
        "portal_value": None,
        "dossier_value": None,
        "message": "Portal requires a pre-authorization code before continuing.",
        "available_options": (),
    }
    defaults.update(overrides)
    return FormHaltItem(**defaults)


def _make_binding(run_id: str = "run_cloud_001") -> CopilotRunBinding:
    return CopilotRunBinding(
        run_id=run_id,
        session_nonce="a" * 64,
        generation=1,
        pack_digest="b" * 64,
        dossier_digest="c" * 64,
        operator_id="cloud_operator_1",
        expires_at_unix=int(time.time()) + 300,
    )


def _sign_payload_with_payload_sha256(payload: dict[str, Any]) -> dict[str, Any]:
    signed = dict(payload)
    signed["payload_sha256"] = compute_resolution_sha256(signed)
    return signed


def _sign_cloud_audit_payload(payload: dict[str, Any]) -> dict[str, Any]:
    signed = dict(payload)
    digest = compute_resolution_sha256(signed)
    signed["digest_sha256"] = digest
    signed["signature_scheme"] = "ed25519+sha256"
    signed["signature_preview"] = hashlib.sha256(
        f"ed25519:{digest}".encode("utf-8")
    ).hexdigest()[:32]
    return signed


def test_choice_halt_to_prepare_card_schema():
    halt = _make_choice_halt()
    card = form_halt_to_prepare_card(
        halt,
        portal_origin="https://provider.sunlife.ca",
        card_id="prep_sunlife_001",
        staged_bindings={0: "$member_id_masked"},
        control_labels={0: "Member ID (last 4)"},
        control_selectors={0: "input[name='member_id']"},
        workflow_summary="Dental claim pre-determination (CDT D2740)",
        office_name="Oakville Dental",
        portal_name="Sun Life",
    )

    assert card["card_id"] == "prep_sunlife_001"
    assert card["portal_origin"] == "https://provider.sunlife.ca"
    assert card["staged_bindings"] == {0: "$member_id_masked"}
    assert card["control_labels"] == {
        0: "Member ID (last 4)",
        1: "Coverage Type",
    }
    assert card["control_selectors"] == {
        0: "input[name='member_id']",
        1: "select[name='coverage_type']",
    }
    assert card["commit_mode"] == "DRAFT_TO_COMMIT"
    assert card["redacted_preview"] is True
    assert card["office_name"] == "Oakville Dental"
    assert card["portal_name"] == "Sun Life"
    assert card["workflow_summary"] == "Dental claim pre-determination (CDT D2740)"
    assert card["created_at_utc"].endswith("Z")

    assert len(card["unresolved_slots"]) == 1
    slot = card["unresolved_slots"][0]
    assert slot["control_idx"] == 1
    assert slot["control_label"] == "Coverage Type"
    assert slot["reason"] == halt.message
    assert slot["input_mode"] == "choice"
    assert slot["allowed_enum_options"] == ["Primary Dental", "Secondary COB"]
    assert slot["candidate_vars"] == []
    assert slot["expected_field_type"] == "exact"
    assert slot["parameter_name"] == "coverage_tier"

    # Confirm portal_value and dossier_value were never serialized into the card.
    serialized = json.dumps(card)
    assert "123-45-6789" not in serialized
    assert "patient@example.com" not in serialized


def test_typed_halt_to_prepare_card_schema():
    halt = _make_typed_halt()
    card = form_halt_to_prepare_card(
        halt,
        portal_origin="https://apps.availity.com",
    )

    assert card["card_id"] == "prep_halt_auth_002"
    assert card["portal_origin"] == "https://apps.availity.com"
    assert card["staged_bindings"] == {}
    assert card["control_labels"] == {0: "Pre-Authorization Code"}
    assert card["control_selectors"] == {0: "input[name='preauth_code']"}
    assert card["pause_reason"] == "missing_required_field"
    assert card["pause_category"] == "Missing Info"

    assert len(card["unresolved_slots"]) == 1
    slot = card["unresolved_slots"][0]
    assert slot["control_idx"] == 0
    assert slot["input_mode"] == "typed"
    assert slot["allowed_enum_options"] == []
    assert slot["candidate_vars"] == []
    assert slot["expected_field_type"] == "exact"
    assert slot["parameter_name"] == "preauth_code"


@pytest.mark.parametrize(
    "field_overrides,extra_kwargs",
    [
        ({"message": "Conflict on SSN 123-45-6789"}, {}),
        ({"message": "Contact alice.smith@clinic.org for details"}, {}),
        ({"message": "Call (416) 555-0199 to verify"}, {}),
        ({"message": "Auth failed with Bearer secret_token_abc123456"}, {}),
        ({"message": "Key sk-live_abcdefghijklmnopqrst"}, {}),
        ({"field_label": "Patient Password"}, {}),
        ({"dossier_key": "portal_password"}, {}),
        ({"dom_selector": "input[type='password']"}, {}),
        (
            {
                "available_options": (
                    ObservedOption(value="a", label="alice@example.com"),
                )
            },
            {},
        ),
        ({}, {"workflow_summary": "Claim for 555-01-9999"}),
        ({}, {"staged_bindings": {0: "patient@example.com"}}),
    ],
)
def test_zero_phi_enforcement_rejects_raw_phi_and_secrets(
    field_overrides: dict[str, Any], extra_kwargs: dict[str, Any]
):
    halt = _make_typed_halt(**field_overrides)
    with pytest.raises((ZeroPhiViolationError, ValueError)):
        form_halt_to_prepare_card(
            halt,
            portal_origin="https://provider.sunlife.ca",
            **extra_kwargs,
        )


def test_approve_resolution_maps_option_label_to_value_and_verifies_payload_sha256():
    halt = _make_choice_halt()
    raw_resolution = _sign_payload_with_payload_sha256(
        {
            "card_id": "prep_sunlife_001",
            "action": "approve",
            "resolution": {
                "card_id": "prep_sunlife_001",
                "operator_id": "billing_lead_1",
                "slot_bindings": {"1": "Primary Dental"},
                "approve_all": False,
                "resolved_at_utc": "2026-11-28T15:10:00Z",
            },
            "merged_bindings": {"0": "$member_id_masked", "1": "Primary Dental"},
        }
    )

    decision = prepare_resolution_to_human_input(
        halt,
        raw_resolution,
        remember_as_candidate_rule=True,
    )

    assert decision.halt_id == halt.halt_id
    assert decision.action == HumanDecisionAction.PROVIDE_VALUE
    assert decision.provided_value == "PRI"
    assert decision.actor_id == "billing_lead_1"
    assert decision.mapped_dossier_key == "coverage_tier"
    assert decision.remember_as_candidate_rule is True
    assert decision.note == ""
    assert decision.idempotency_key.startswith("cloud-prepare:prep_sunlife_001:")


def test_approve_resolution_verifies_cloud_digest_sha256_and_signature_preview():
    halt = _make_typed_halt()
    audit_payload = _sign_cloud_audit_payload(
        {
            "schema_version": "prepare_signoff_v1",
            "card_id": "prep_halt_auth_002",
            "portal_origin": "https://apps.availity.com",
            "office_name": None,
            "portal_name": None,
            "commit_mode": "DRAFT_TO_COMMIT",
            "action": "approve",
            "resolution": {
                "card_id": "prep_halt_auth_002",
                "operator_id": "ops_user_2",
                "slot_bindings": {"0": "PA-2026-8841"},
                "approve_all": False,
                "resolved_at_utc": "2026-11-28T15:12:00Z",
            },
            "merged_bindings": {"0": "PA-2026-8841"},
            "zero_phi_verified": True,
            "verification_status": "verified",
        }
    )

    decision = prepare_resolution_to_human_input(halt, audit_payload)
    assert decision.action == HumanDecisionAction.PROVIDE_VALUE
    assert decision.provided_value == "PA-2026-8841"
    assert decision.actor_id == "ops_user_2"


def test_tampered_payload_sha256_raises_value_error():
    halt = _make_choice_halt()
    signed = _sign_payload_with_payload_sha256(
        {
            "card_id": "prep_sunlife_001",
            "action": "approve",
            "resolution": {
                "card_id": "prep_sunlife_001",
                "operator_id": "billing_lead_1",
                "slot_bindings": {"1": "Primary Dental"},
                "approve_all": False,
                "resolved_at_utc": "2026-11-28T15:10:00Z",
            },
        }
    )
    # Tamper with the chosen value after signing.
    signed["resolution"]["slot_bindings"]["1"] = "Secondary COB"

    with pytest.raises(ValueError, match="payload_sha256"):
        prepare_resolution_to_human_input(halt, signed)


def test_tampered_digest_sha256_raises_value_error():
    halt = _make_typed_halt()
    signed = _sign_cloud_audit_payload(
        {
            "schema_version": "prepare_signoff_v1",
            "card_id": "prep_halt_auth_002",
            "portal_origin": "https://apps.availity.com",
            "office_name": None,
            "portal_name": None,
            "commit_mode": "DRAFT_TO_COMMIT",
            "action": "approve",
            "resolution": {
                "card_id": "prep_halt_auth_002",
                "operator_id": "ops_user_2",
                "slot_bindings": {"0": "PA-2026-8841"},
                "approve_all": False,
                "resolved_at_utc": "2026-11-28T15:12:00Z",
            },
            "merged_bindings": {"0": "PA-2026-8841"},
            "zero_phi_verified": True,
        }
    )
    signed["merged_bindings"]["0"] = "PA-9999-TAMPERED"

    with pytest.raises(ValueError, match="digest_sha256"):
        prepare_resolution_to_human_input(halt, signed)


def test_reject_resolution_maps_to_abort_run():
    halt = _make_choice_halt()
    rejected = _sign_payload_with_payload_sha256(
        {
            "card_id": "prep_sunlife_001",
            "action": "reject",
            "rejection_reason": "Policy forbids secondary override",
            "resolution": {
                "card_id": "prep_sunlife_001",
                "operator_id": "clinical_lead",
                "slot_bindings": {},
                "approve_all": False,
                "resolved_at_utc": "2026-11-28T15:15:00Z",
                "rejection_reason": "Policy forbids secondary override",
            },
        }
    )

    decision = prepare_resolution_to_human_input(halt, rejected)
    assert decision.halt_id == halt.halt_id
    assert decision.action == HumanDecisionAction.ABORT_RUN
    assert decision.provided_value is None
    assert decision.actor_id == "clinical_lead"
    assert decision.note == "Policy forbids secondary override"


def test_end_to_end_mock_http_server_and_bound_decision_store_claim(tmp_path: Path):
    state: dict[str, Any] = {
        "enqueued_requests": [],
        "poll_count": 0,
        "auth_headers": [],
        "resolution_ready_after": 2,
    }

    class _MockPrepareHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(self, status: int, body: dict[str, Any]) -> None:
            encoded = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_POST(self) -> None:
            if self.path != "/api/prepare":
                self._send_json(404, {"error": "not_found"})
                return
            state["auth_headers"].append(self.headers.get("Authorization"))
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            payload = json.loads(raw)
            state["enqueued_requests"].append(payload)
            self._send_json(201, {"accepted": True, "card": payload["card"]})

        def do_GET(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path != "/api/prepare":
                self._send_json(404, {"error": "not_found"})
                return
            state["auth_headers"].append(self.headers.get("Authorization"))
            params = urllib.parse.parse_qs(parsed.query)
            card_id = params.get("card_id", [""])[0]
            state["poll_count"] += 1

            if state["poll_count"] < state["resolution_ready_after"]:
                self._send_json(200, {"cards": [], "resolutions": []})
                return

            signed_resolution = _sign_payload_with_payload_sha256(
                {
                    "schema_version": "prepare_signoff_v1",
                    "card_id": card_id,
                    "portal_origin": "https://provider.sunlife.ca",
                    "action": "approve",
                    "resolution": {
                        "card_id": card_id,
                        "operator_id": "cloud_operator_1",
                        "slot_bindings": {"1": "Secondary COB"},
                        "approve_all": False,
                        "resolved_at_utc": "2026-11-28T15:20:00Z",
                    },
                    "merged_bindings": {
                        "0": "$member_id_masked",
                        "1": "Secondary COB",
                    },
                    "zero_phi_verified": True,
                }
            )
            self._send_json(
                200,
                {"cards": [], "resolutions": [signed_resolution]},
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), _MockPrepareHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        client = CloudPrepareClient(
            f"http://127.0.0.1:{port}",
            api_token="oa_test_token_123",
            request_timeout_seconds=5.0,
        )
        halt = _make_choice_halt()
        binding = _make_binding()
        store = BoundDecisionStore(tmp_path / "copilot_session")

        decision = client.resolve_halt_via_cloud(
            halt,
            portal_origin="https://provider.sunlife.ca",
            card_id="prep_e2e_001",
            staged_bindings={0: "$member_id_masked"},
            control_labels={0: "Member ID"},
            timeout_seconds=5.0,
            poll_interval_seconds=0.05,
            binding=binding,
            store=store,
        )

        assert decision.action == HumanDecisionAction.PROVIDE_VALUE
        assert decision.provided_value == "SEC"
        assert decision.actor_id == "cloud_operator_1"

        # Verify wire requests and Bearer auth header.
        assert len(state["enqueued_requests"]) == 1
        enq = state["enqueued_requests"][0]
        assert enq["type"] == "enqueue"
        assert enq["card"]["card_id"] == "prep_e2e_001"
        assert state["poll_count"] == 2
        assert all(h == "Bearer oa_test_token_123" for h in state["auth_headers"])

        # Verify BoundDecisionStore persisted and claims the decision once.
        claimed = store.claim(binding, lambda: None)
        assert claimed is not None
        assert claimed.binding == binding
        assert claimed.decision == decision
        assert store.claim(binding, lambda: None) is None
    finally:
        server.shutdown()
        server.server_close()


def test_poll_resolution_timeout():
    class _PendingOnlyHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            encoded = json.dumps({"cards": [], "resolutions": []}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _PendingOnlyHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        client = CloudPrepareClient(f"http://127.0.0.1:{port}")
        with pytest.raises(TimeoutError, match="prep_timeout_001"):
            client.poll_resolution(
                "prep_timeout_001",
                timeout_seconds=0.15,
                poll_interval_seconds=0.05,
            )
    finally:
        server.shutdown()
        server.server_close()

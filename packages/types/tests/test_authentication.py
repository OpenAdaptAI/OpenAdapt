"""Tests for the value-free authentication task and receipt contracts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from importlib.resources import files

import pytest
from pydantic import ValidationError

from openadapt_types.authentication import (
    AuthenticationContractError,
    AuthenticationReceiptPayloadV1,
    AuthenticationReceiptV1,
    AuthenticationRunBindingV1,
    AuthenticationTaskContractV1,
    issue_authentication_receipt,
    validate_authentication_receipt,
)

SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
HMAC_A = "hmac-sha256:" + "a" * 64
HMAC_B = "hmac-sha256:" + "b" * 64
NOW = datetime(2026, 8, 30, 20, 2, 0, tzinfo=timezone.utc)


def _contract(**updates: object) -> AuthenticationTaskContractV1:
    values: dict[str, object] = {
        "task_id": "authentication.application.0001",
        "human_decision_task_digest": SHA_A,
        "substrate": "browser",
        "allowed_methods": (
            "existing_session",
            "password_manager_autofill",
            "saved_account_selection",
        ),
        "principal_class": "named_user",
        "requires_user_presence": True,
        "mfa_policy": "if_challenged",
        "max_session_age_seconds": 900,
        "verifier_id": "verifier.session.0001",
        "verifier_kind": "authenticated_session_probe",
        "verifier_contract_digest": SHA_A,
        "principal_binding_contract_digest": SHA_B,
        "application_contract_digest": SHA_A,
        "environment_contract_digest": SHA_B,
    }
    values.update(updates)
    return AuthenticationTaskContractV1.model_validate(values)


def _binding(**updates: object) -> AuthenticationRunBindingV1:
    values: dict[str, object] = {
        "app_version_digest": SHA_A,
        "process_execution_id": "execution.process.0001",
        "step_id": "step.login.0001",
        "challenge_digest": SHA_B,
        "principal_binding_hmac": HMAC_A,
        "session_binding_hmac": HMAC_B,
        "operator_authority_digest": SHA_C,
        "verifier_evidence_digest": SHA_A,
        "capture_exclusion_receipt_digest": SHA_B,
    }
    values.update(updates)
    return AuthenticationRunBindingV1.model_validate(values)


def _receipt(
    contract: AuthenticationTaskContractV1,
    binding: AuthenticationRunBindingV1,
    **updates: object,
) -> AuthenticationReceiptV1:
    values: dict[str, object] = {
        "task_contract_digest": contract.digest,
        "task_id": contract.task_id,
        "human_decision_task_digest": contract.human_decision_task_digest,
        "app_version_digest": binding.app_version_digest,
        "process_execution_id": binding.process_execution_id,
        "step_id": binding.step_id,
        "challenge_digest": binding.challenge_digest,
        "method": "saved_account_selection",
        "substrate": contract.substrate,
        "principal_class": contract.principal_class,
        "principal_binding_contract_digest": (
            contract.principal_binding_contract_digest
        ),
        "principal_binding_hmac": binding.principal_binding_hmac,
        "session_binding_hmac": binding.session_binding_hmac,
        "operator_authority_digest": binding.operator_authority_digest,
        "authenticated_at": "2026-08-30T20:00:00Z",
        "verified_at": "2026-08-30T20:01:00Z",
        "fresh_until": "2026-08-30T20:15:00Z",
        "user_presence_outcome": "verified",
        "mfa_outcome": "not_challenged",
        "verifier_id": contract.verifier_id,
        "verifier_contract_digest": contract.verifier_contract_digest,
        "verifier_evidence_digest": binding.verifier_evidence_digest,
        "capture_exclusion_receipt_digest": (binding.capture_exclusion_receipt_digest),
        "broker_binding_digest": binding.broker_binding_digest,
        "outcome": "verified",
    }
    values.update(updates)
    payload = AuthenticationReceiptPayloadV1.model_validate(values)
    return issue_authentication_receipt(payload)


def test_saved_account_selection_is_value_free_and_admitted() -> None:
    contract = _contract()
    binding = _binding()
    receipt = _receipt(contract, binding)
    assert validate_authentication_receipt(contract, binding, receipt, now=NOW)

    wire = receipt.model_dump_json().casefold()
    for forbidden in (
        '"username"',
        '"account_label"',
        '"password"',
        '"cookie"',
        '"token"',
    ):
        assert forbidden not in wire


@pytest.mark.parametrize(
    "unsafe_field",
    ["username", "account_label", "password", "credential_value", "session_token"],
)
def test_receipt_refuses_secret_or_identity_fields(unsafe_field: str) -> None:
    receipt = _receipt(_contract(), _binding())
    values = receipt.model_dump(mode="json", exclude={"receipt_digest"})
    values[unsafe_field] = "must-not-enter-the-receipt"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AuthenticationReceiptPayloadV1.model_validate(values)


def test_capture_policy_only_accepts_source_exclusion() -> None:
    values = _contract().model_dump(mode="json")
    values["capture"]["media_frames"] = "redact_later"
    with pytest.raises(ValidationError):
        AuthenticationTaskContractV1.model_validate(values)


def test_done_does_not_prove_authentication() -> None:
    contract = _contract()
    binding = _binding()
    receipt = _receipt(contract, binding, outcome="indeterminate")
    with pytest.raises(AuthenticationContractError, match="did not confirm"):
        validate_authentication_receipt(contract, binding, receipt, now=NOW)


def test_receipt_cannot_move_to_another_surface() -> None:
    contract = _contract(substrate="browser")
    binding = _binding()
    receipt = _receipt(contract, binding, substrate="windows")
    with pytest.raises(AuthenticationContractError, match="substrate differs"):
        validate_authentication_receipt(contract, binding, receipt, now=NOW)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("process_execution_id", "execution.other.0001"),
        ("step_id", "step.other.0001"),
        ("challenge_digest", SHA_C),
        ("session_binding_hmac", HMAC_A),
        ("capture_exclusion_receipt_digest", SHA_C),
    ],
)
def test_receipt_binds_the_live_process_evidence(field: str, value: str) -> None:
    contract = _contract()
    binding = _binding()
    receipt = _receipt(contract, binding)
    changed = _binding(**{field: value})
    with pytest.raises(AuthenticationContractError, match=f"{field} differs"):
        validate_authentication_receipt(contract, changed, receipt, now=NOW)


def test_receipt_expires_and_mfa_fails_closed() -> None:
    contract = _contract(max_session_age_seconds=300)
    binding = _binding()
    receipt = _receipt(contract, binding, fresh_until="2026-08-30T20:05:00Z")
    assert validate_authentication_receipt(contract, binding, receipt, now=NOW)
    with pytest.raises(AuthenticationContractError, match="receipt is stale"):
        validate_authentication_receipt(
            contract,
            binding,
            receipt,
            now=datetime(2026, 8, 30, 20, 5, 1, tzinfo=timezone.utc),
        )

    mfa = _contract(mfa_policy="required")
    with pytest.raises(AuthenticationContractError, match="required MFA"):
        validate_authentication_receipt(mfa, binding, _receipt(mfa, binding), now=NOW)


def test_receipt_digest_detects_mutation() -> None:
    receipt = _receipt(_contract(), _binding())
    values = receipt.model_dump(mode="json")
    values["verifier_evidence_digest"] = SHA_C
    with pytest.raises(ValidationError, match="digest does not match"):
        AuthenticationReceiptV1.model_validate(values)


def test_schemas_have_no_value_slots() -> None:
    schemas = json.dumps(
        {
            "task": AuthenticationTaskContractV1.model_json_schema(),
            "receipt": AuthenticationReceiptV1.model_json_schema(),
        }
    ).casefold()
    for forbidden in (
        '"username"',
        '"account_label"',
        '"password"',
        '"credential_value"',
        '"session_token"',
    ):
        assert forbidden not in schemas


@pytest.mark.parametrize(
    ("model", "filename"),
    [
        (AuthenticationTaskContractV1, "authentication-task-v1.json"),
        (AuthenticationReceiptV1, "authentication-receipt-v1.json"),
    ],
)
def test_packaged_authentication_schemas_match_models(
    model: object, filename: str
) -> None:
    packaged = json.loads(
        files("openadapt_types.schemas").joinpath(filename).read_text(encoding="utf-8")
    )
    assert packaged == model.model_json_schema()

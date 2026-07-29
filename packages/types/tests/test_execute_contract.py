"""Focused invariants for the public Execute v1 contract."""

import json
from importlib.resources import files

import pytest
from pydantic import ValidationError

from openadapt_types import (
    EffectStrengthV1,
    ExecuteEvidenceContractV1,
    ExecuteEvidenceReceiptV1,
    ExecuteLifecycleStateV1,
    ExecuteRequestV1,
    ExecuteStatusV1,
    ExecuteTerminalOutcomeV1,
    ExecuteWebhookEventTypeV1,
    execute_openapi_document,
    sign_execute_webhook_hmac,
)


def _contract(**updates: object) -> ExecuteEvidenceContractV1:
    fields: dict[str, object] = {
        "authorization_passed": True,
        "identity_passed": True,
        "postcondition_passed": True,
        "effect_passed": True,
        "minimum_effect_strength": EffectStrengthV1.INDEPENDENT_SESSION,
        "observed_effect_strength": EffectStrengthV1.INDEPENDENT_SYSTEM_OF_RECORD,
        "model_used": False,
        "external_network_used": False,
    }
    fields.update(updates)
    return ExecuteEvidenceContractV1.model_validate(fields)


def _receipt(**updates: object) -> ExecuteEvidenceReceiptV1:
    fields: dict[str, object] = {
        "receipt_id": "receipt_12345678",
        "execution_id": "execution_12345678",
        "workflow_digest": "sha256:" + "a" * 64,
        "outcome": ExecuteTerminalOutcomeV1.VERIFIED,
        "contracts": _contract(),
        "delivery_uncertain": False,
        "evidence_digest": "sha256:" + "b" * 64,
        "issued_at": "2026-07-29T12:00:00Z",
    }
    fields.update(updates)
    return ExecuteEvidenceReceiptV1.model_validate(fields)


def test_request_binds_qualified_workflow_environment_and_idempotency() -> None:
    request = ExecuteRequestV1(
        qualification_id="qualification_12345678",
        workflow_version="workflow_20260729",
        workflow_digest="sha256:" + "c" * 64,
        environment_id="environment_12345678",
        parameters={"patient": {"id": "12345"}, "date": "2026-08-15"},
        idempotency_key="caller_key_12345678",
        authorization_context={
            "actor_id": "caller_agent_12345678",
            "authorization_reference": "authorization_12345678",
        },
        minimum_effect_strength=EffectStrengthV1.INDEPENDENT_SYSTEM_OF_RECORD,
    )

    assert request.effect_strength_schema_version == "1"
    assert request.parameters["patient"] == {"id": "12345"}


def test_lifecycle_state_and_terminal_outcome_cannot_be_conflated() -> None:
    waiting = ExecuteStatusV1(
        execution_id="execution_12345678",
        state=ExecuteLifecycleStateV1.WAITING_FOR_RECONCILIATION,
        updated_at="2026-07-29T12:00:00Z",
    )
    assert waiting.terminal_outcome is None

    with pytest.raises(ValidationError, match="only a terminal state"):
        ExecuteStatusV1(
            execution_id="execution_12345678",
            state=ExecuteLifecycleStateV1.WAITING_FOR_RECONCILIATION,
            terminal_outcome=ExecuteTerminalOutcomeV1.RECONCILIATION_REQUIRED,
            updated_at="2026-07-29T12:00:00Z",
        )

    terminal = ExecuteStatusV1(
        execution_id="execution_12345678",
        state=ExecuteLifecycleStateV1.TERMINAL,
        terminal_outcome=ExecuteTerminalOutcomeV1.RECONCILIATION_REQUIRED,
        evidence_receipt_id="receipt_12345678",
        updated_at="2026-07-29T12:00:00Z",
    )
    assert terminal.terminal_outcome is ExecuteTerminalOutcomeV1.RECONCILIATION_REQUIRED


def test_verified_and_rollback_outcomes_require_their_independent_proof() -> None:
    _receipt()

    with pytest.raises(ValidationError, match="below the required strength"):
        _receipt(
            contracts=_contract(
                minimum_effect_strength=EffectStrengthV1.INDEPENDENT_SYSTEM_OF_RECORD,
                observed_effect_strength=EffectStrengthV1.INDEPENDENT_SESSION,
            )
        )

    with pytest.raises(ValidationError, match="independently verified compensation"):
        _receipt(outcome=ExecuteTerminalOutcomeV1.ROLLED_BACK_VERIFIED)

    rolled_back = _receipt(
        outcome=ExecuteTerminalOutcomeV1.ROLLED_BACK_VERIFIED,
        compensation_effect_verified=True,
    )
    assert rolled_back.compensation_effect_verified is True


def test_reconciliation_requires_uncertainty_not_a_success_shaped_receipt() -> None:
    with pytest.raises(ValidationError, match="requires an uncertain"):
        _receipt(outcome=ExecuteTerminalOutcomeV1.RECONCILIATION_REQUIRED)

    receipt = _receipt(
        outcome=ExecuteTerminalOutcomeV1.RECONCILIATION_REQUIRED,
        delivery_uncertain=True,
        contracts=_contract(effect_passed=False, observed_effect_strength=None),
    )
    assert receipt.outcome is ExecuteTerminalOutcomeV1.RECONCILIATION_REQUIRED


def test_webhook_signature_binds_the_closed_state_payload() -> None:
    key = b"k" * 32
    webhook = sign_execute_webhook_hmac(
        key=key,
        fields={
            "event_type": ExecuteWebhookEventTypeV1.EXECUTION_STATE_CHANGED,
            "event_id": "event_12345678",
            "delivery_attempt": 1,
            "issued_at": "2026-07-29T12:00:00Z",
            "issuer_key_id": "webhook_key_12345678",
            "execution": {
                "execution_id": "execution_12345678",
                "state": "running",
                "updated_at": "2026-07-29T12:00:00Z",
            },
        },
    )
    assert webhook.verify_hmac(key)
    assert not webhook.model_copy(update={"delivery_attempt": 2}).verify_hmac(key)


def test_packaged_openapi_is_the_generated_public_contract() -> None:
    generated = execute_openapi_document()
    packaged = files("openadapt_types.schemas").joinpath("execute-v1-openapi.json")
    assert json.loads(packaged.read_text()) == generated
    assert generated["x-openadapt-schema"] == "openadapt.execute-openapi/v1"
    assert set(generated["paths"]) == {
        "/v1/executions",
        "/v1/executions/{execution_id}",
        "/v1/executions/{execution_id}/receipt",
    }

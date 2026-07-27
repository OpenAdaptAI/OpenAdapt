"""Durable tests for the attended human-decision wire contract."""

import json
from importlib.resources import files

import pytest
from pydantic import ValidationError

from openadapt_types import (
    HumanDecisionAction,
    HumanDecisionDeliveryState,
    HumanDecisionEvidenceSummaryV1,
    HumanDecisionQuestionTemplate,
    HumanDecisionQuestionV1,
    HumanDecisionRequiredAuthn,
    HumanDecisionSafeSlotsV1,
    HumanDecisionTaskKind,
    HumanDecisionTaskV1,
    sign_human_decision_task_hmac,
)


def _task_fields() -> dict[str, object]:
    digest = "sha256:" + "a" * 64
    return {
        "task_id": "task_12345678",
        "run_id": "run_123456789",
        "pause_id": "pause_1234567",
        "capability_digest": digest,
        "bundle_digest": "sha256:" + "b" * 64,
        "task_kind": HumanDecisionTaskKind.IDENTITY,
        "delivery_state": HumanDecisionDeliveryState.NOT_DELIVERED,
        "question": HumanDecisionQuestionV1(
            template=HumanDecisionQuestionTemplate.CONFIRM_IDENTITY,
            safe_slots=HumanDecisionSafeSlotsV1(
                required_signal_count=2,
                confirmed_signal_count=1,
            ),
        ),
        "evidence": HumanDecisionEvidenceSummaryV1(
            identity_required_count=2,
            identity_confirmed_count=1,
            frame_available_locally=True,
        ),
        "allowed_actions": (
            HumanDecisionAction.VERIFY_AND_RESUME,
            HumanDecisionAction.ESCALATE,
        ),
        "required_authn": HumanDecisionRequiredAuthn.LOCAL_SESSION,
        "created_at": "2026-07-26T12:00:00Z",
        "expires_at": "2026-07-26T12:05:00Z",
        "nonce": "nonce_12345678",
        "issuer_key_id": "local_attended_v1",
    }


def test_signed_task_detects_any_tamper_without_becoming_authority() -> None:
    key = b"k" * 32
    task = sign_human_decision_task_hmac(key=key, fields=_task_fields())

    assert task.verify_hmac(key)
    assert task.digest.startswith("sha256:")

    tampered = task.model_copy(update={"expires_at": "2026-07-26T12:06:00Z"})
    assert not tampered.verify_hmac(key)


def test_task_refuses_payloads_that_could_relay_sensitive_evidence() -> None:
    task = sign_human_decision_task_hmac(key=b"k" * 32, fields=_task_fields())
    payload = task.model_dump(mode="json")
    payload["screenshot"] = "data:image/png;base64,secret"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        HumanDecisionTaskV1.model_validate(payload)

    fields = _task_fields()
    fields["allowed_actions"] = (
        HumanDecisionAction.VERIFY_AND_RESUME,
        HumanDecisionAction.VERIFY_AND_RESUME,
    )
    with pytest.raises(ValidationError, match="allowed_actions must be unique"):
        sign_human_decision_task_hmac(key=b"k" * 32, fields=fields)


def test_packaged_schema_matches_the_strict_language_agnostic_contract() -> None:
    schema = HumanDecisionTaskV1.model_json_schema()
    assert schema["additionalProperties"] is False
    assert "screenshot" not in json.dumps(schema).lower()
    packaged = files("openadapt_types.schemas").joinpath(
        "human-decision-task-v1.json"
    )
    assert json.loads(packaged.read_text()) == schema

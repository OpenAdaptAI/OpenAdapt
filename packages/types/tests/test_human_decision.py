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


#: Every category of protected content the Cloud-safe task must be unable to
#: carry, with a representative field name and a value that would be a real
#: disclosure. Enumerating the categories, rather than testing one field, is
#: what keeps a later field addition from quietly reopening a closed hole.
FORBIDDEN_EVIDENCE_FIELDS: dict[str, dict[str, object]] = {
    "screenshots": {
        "screenshot": "data:image/png;base64,secret",
        "frame_png": "iVBORw0KGgo=",
        "crop": {"x": 0, "y": 0, "png": "iVBORw0KGgo="},
    },
    "ocr_text": {
        "ocr_text": "Coverage: active",
        "extracted_text": "Patient: J. Doe",
    },
    "expected_and_observed_values": {
        "expected_value": "active",
        "observed_value": "terminated",
        "field_values": {"coverage": "active"},
    },
    "free_text": {
        "reason": "operator says the claim looked fine",
        "intent": "update the coverage row",
        "note": "called the clinic, they confirmed",
        "message": "resumed after manual fix",
    },
    "identifiers": {
        "patient_name": "Jane Doe",
        "mrn": "0093211",
        "record_id": "claim-88213",
        "operator_email": "nurse@example.org",
        "workflow_name": "openimis_claim_adjudication",
    },
    "unknown_fields": {
        "future_extension": "anything at all",
        "x_vendor_hint": 1,
    },
}


@pytest.mark.parametrize("category", sorted(FORBIDDEN_EVIDENCE_FIELDS))
def test_cloud_safe_task_rejects_each_forbidden_category(category: str) -> None:
    """Rejection is structural, so no producer can opt into relaying evidence."""
    signed = sign_human_decision_task_hmac(key=b"k" * 32, fields=_task_fields())
    base = signed.model_dump(mode="json")

    for field, value in FORBIDDEN_EVIDENCE_FIELDS[category].items():
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            HumanDecisionTaskV1.model_validate({**base, field: value})

        nested_question = {**base["question"], field: value}
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            HumanDecisionTaskV1.model_validate({**base, "question": nested_question})

        nested_evidence = {**base["evidence"], field: value}
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            HumanDecisionTaskV1.model_validate({**base, "evidence": nested_evidence})


def test_no_declared_string_field_accepts_free_text() -> None:
    """The exported schema is the contract a non-Python consumer validates.

    Closing unknown fields is not sufficient on its own: a *declared* string
    with no pattern would let protected content travel inside an accepted
    field. Every string in the contract must therefore be closed by a pattern,
    a ``const``, or an ``enum``.
    """
    unconstrained: list[str] = []

    def visit(node: object, path: str) -> None:
        if isinstance(node, dict):
            if node.get("type") == "string" and not (
                {"pattern", "const", "enum"} & set(node)
            ):
                unconstrained.append(path)
            for key, value in node.items():
                visit(value, f"{path}/{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                visit(value, f"{path}/{index}")

    visit(HumanDecisionTaskV1.model_json_schema(), "")
    assert unconstrained == []


@pytest.mark.parametrize(
    "value",
    [
        "J DOE MRN 0093211 XY",
        "2026-07-26 patient jd",
        "coverage active as of",
        "2026-07-26T12:00:00",
    ],
)
def test_timestamps_cannot_smuggle_text_or_drop_their_offset(value: str) -> None:
    fields = _task_fields()
    fields["created_at"] = value
    with pytest.raises(ValidationError):
        sign_human_decision_task_hmac(key=b"k" * 32, fields=fields)


#: One frozen vector pinning the canonicalization rules documented in
#: ``openadapt_types.human_decision``. A cross-language implementation is
#: correct exactly when it reproduces these bytes and this signature. Changing
#: any field name, key ordering, escaping rule, or the signing domain will fail
#: here, which is the signal to cut a new schema version rather than re-sign.
CANONICAL_VECTOR_BYTES = (
    b'{"allowed_actions":["verify_and_resume","escalate"],'
    b'"bundle_digest":"sha256:' + b"b" * 64 + b'",'
    b'"capability_digest":"sha256:' + b"a" * 64 + b'",'
    b'"created_at":"2026-07-26T12:00:00Z","delivery_state":"not_delivered",'
    b'"evidence":{"effect_confirmed_count":null,"effect_required_count":null,'
    b'"frame_available_locally":true,"identity_confirmed_count":1,'
    b'"identity_required_count":2,"minimum_effect_tier":null,'
    b'"observed_effect_tier":null,"sensitive_evidence_local_only":true},'
    b'"expires_at":"2026-07-26T12:05:00Z","issuer_key_id":"local_attended_v1",'
    b'"nonce":"nonce_12345678","pause_id":"pause_1234567",'
    b'"question":{"safe_slots":{"candidate_count":null,'
    b'"confirmed_signal_count":1,"required_signal_count":2},'
    b'"template":"confirm_identity"},"required_authn":"local_session",'
    b'"risk_class":"unknown","run_id":"run_123456789","runner_id":null,'
    b'"schema_version":"openadapt.human-decision-task/v1",'
    b'"signature_algorithm":"hmac-sha256","substrate":"unknown",'
    b'"task_id":"task_12345678","task_kind":"identity","task_revision":1,'
    b'"tenant_id":null}'
)
CANONICAL_VECTOR_DIGEST = (
    "sha256:0ee7fafed2c51e1148384f9583e0875d542d3d04d4eaf0f2ef2f559ed13b0c06"
)
CANONICAL_VECTOR_SIGNATURE = (
    "hmac-sha256:0705665cdd455258b1b7bf3906f4bf53b97c124c88750b7e7d9f5f766269cdee"
)


def test_canonical_bytes_and_signature_are_pinned_for_other_languages() -> None:
    task = sign_human_decision_task_hmac(key=b"k" * 32, fields=_task_fields())

    assert task.canonical_unsigned_bytes() == CANONICAL_VECTOR_BYTES
    assert task.digest == CANONICAL_VECTOR_DIGEST
    assert task.signature == CANONICAL_VECTOR_SIGNATURE


def test_canonicalization_is_ascii_sorted_and_whitespace_free() -> None:
    """Assert the properties a reimplementation has to match, not just bytes."""
    canonical = sign_human_decision_task_hmac(
        key=b"k" * 32, fields=_task_fields()
    ).canonical_unsigned_bytes()

    assert canonical.decode("ascii")
    assert b", " not in canonical and b": " not in canonical
    payload = json.loads(canonical)
    assert list(payload) == sorted(payload)
    assert list(payload["evidence"]) == sorted(payload["evidence"])
    assert "signature" not in payload
    assert "tenant_id" in payload and payload["tenant_id"] is None


def test_delivery_uncertainty_is_a_first_class_value() -> None:
    """"May have been sent" must be statable, never an absent field."""
    assert {state.value for state in HumanDecisionDeliveryState} == {
        "not_delivered",
        "delivered",
        "unknown",
    }

    fields = _task_fields()
    del fields["delivery_state"]
    with pytest.raises(ValidationError, match="delivery_state"):
        sign_human_decision_task_hmac(key=b"k" * 32, fields=fields)

    uncertain = sign_human_decision_task_hmac(
        key=b"k" * 32,
        fields={**_task_fields(), "delivery_state": HumanDecisionDeliveryState.UNKNOWN},
    )
    assert uncertain.model_dump(mode="json")["delivery_state"] == "unknown"

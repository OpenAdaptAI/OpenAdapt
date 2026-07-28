"""Durable tests for the attended human-decision wire contract."""

import hashlib
import hmac
import json
from importlib.resources import files

import pytest
from pydantic import ValidationError

from openadapt_types import (
    HUMAN_DECISION_RECEIPT_REASONS,
    HUMAN_DECISION_RECEIPT_SUCCESS_STATES,
    HumanDecisionAction,
    HumanDecisionDeliveryState,
    HumanDecisionEvidenceSummaryV1,
    HumanDecisionQuestionTemplate,
    HumanDecisionQuestionV1,
    HumanDecisionReceiptReason,
    HumanDecisionReceiptState,
    HumanDecisionReceiptV1,
    HumanDecisionRequiredAuthn,
    HumanDecisionSafeSlotsV1,
    HumanDecisionTaskKind,
    HumanDecisionTaskV1,
    sign_human_decision_receipt_hmac,
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


def unconstrained_string_paths(schema: object) -> list[str]:
    """Walk an exported JSON Schema and report every string left open.

    Shared by the task and the receipt so both contracts are held to the same
    structural rule and neither can drift away from it independently.
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

    visit(schema, "")
    return unconstrained


def test_no_declared_string_field_accepts_free_text() -> None:
    """The exported schema is the contract a non-Python consumer validates.

    Closing unknown fields is not sufficient on its own: a *declared* string
    with no pattern would let protected content travel inside an accepted
    field. Every string in the contract must therefore be closed by a pattern,
    a ``const``, or an ``enum``.
    """
    assert unconstrained_string_paths(HumanDecisionTaskV1.model_json_schema()) == []


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


# ── HumanDecisionReceiptV1 ───────────────────────────────────────────
#
# The receipt closes the round trip the task opened. The runtime's own
# decision record is an append-only audit artifact that carries a free-text
# message and the operator principal on purpose; neither may reach a phone or
# Cloud. Every test below asserts that the receipt cannot carry them at all,
# rather than that some producer removed them on the way out.


def _receipt_fields() -> dict[str, object]:
    return {
        "task_id": "task_12345678",
        "pause_id": "pause_1234567",
        "capability_digest": "sha256:" + "a" * 64,
        "request_digest": "sha256:" + "c" * 64,
        "decision_digest": "sha256:" + "d" * 64,
        "transition_receipt_digest": "sha256:" + "e" * 64,
        "action": HumanDecisionAction.VERIFY_AND_RESUME,
        "state": HumanDecisionReceiptState.COMPLETED,
        "reason_code": HumanDecisionReceiptReason.VERIFIED_AND_RESUMED,
        "report_success": True,
        "decided_at": "2026-07-26T12:03:00Z",
    }


#: Every terminal outcome the runtime engine really produces, in the exact
#: (state, reason_code) pairing the receipt must accept. ``expired`` is
#: reachable through admission rather than through an engine decision record,
#: and is included because a consumer still has to render it.
ENGINE_TERMINAL_OUTCOMES: tuple[tuple[str, str, str], ...] = (
    ("prepared", "accepted_pending_runner", "pending_runner"),
    ("delivery_started", "delivery_uncertain", "delivery_uncertain"),
    ("delivery_uncertain", "delivery_uncertain", "delivery_uncertain"),
    ("completed", "completed", "verified_and_resumed"),
    ("completed/skip", "completed", "skipped_and_resumed"),
    ("refused", "refused", "revalidation_refused"),
    ("halted", "halted", "continuation_halted"),
    ("needs_demonstration", "demonstration_requested", "demonstration_requested"),
    ("escalated", "escalated", "escalation_recorded"),
    ("rejected", "rejected", "rejected_by_operator"),
    ("<admission>", "expired", "expired"),
)


@pytest.mark.parametrize(
    ("engine_status", "state", "reason_code"), ENGINE_TERMINAL_OUTCOMES
)
def test_receipt_represents_every_real_engine_terminal_outcome(
    engine_status: str, state: str, reason_code: str
) -> None:
    """No real terminal outcome has to be collapsed into a different one.

    ``demonstration_requested`` and ``escalated`` are separate states because
    a ``teach`` or ``escalate`` decision is recorded and returned immediately
    with no runner continuation pending; reporting them as
    ``accepted_pending_runner`` would tell the operator to wait for something
    that is never coming.
    """
    del engine_status
    receipt = HumanDecisionReceiptV1.model_validate(
        {
            **_receipt_fields(),
            "state": state,
            "reason_code": reason_code,
            "report_success": state == "completed",
            "action": (
                "skip"
                if reason_code == "skipped_and_resumed"
                else "reject"
                if reason_code == "rejected_by_operator"
                else "verify_and_resume"
            ),
        }
    )
    assert receipt.state.value == state
    assert receipt.reason_code.value == reason_code
    assert receipt.succeeded is (state == "completed")


def test_receipt_reason_code_is_closed_and_never_free_text() -> None:
    """``reason_code`` is the field that would otherwise be a message."""
    assert {reason.value for reason in HumanDecisionReceiptReason} == {
        "pending_runner",
        "verified_and_resumed",
        "skipped_and_resumed",
        "continuation_halted",
        "revalidation_refused",
        "expired",
        "delivery_uncertain",
        "demonstration_requested",
        "escalation_recorded",
        "rejected_by_operator",
    }

    for prose in (
        "resumed after the operator fixed the coverage row",
        "Patient J. Doe: claim 88213 adjudicated",
        "",
        "verified_and_resumed ",
    ):
        with pytest.raises(ValidationError):
            HumanDecisionReceiptV1.model_validate(
                {**_receipt_fields(), "reason_code": prose}
            )

    schema = HumanDecisionReceiptV1.model_json_schema()
    reason_schema = schema["$defs"]["HumanDecisionReceiptReason"]
    assert set(reason_schema["enum"]) == {
        reason.value for reason in HumanDecisionReceiptReason
    }
    assert "pattern" not in reason_schema


def test_receipt_has_no_free_text_field_at_all() -> None:
    """Enumerated per field, so adding an open string fails here immediately."""
    schema = HumanDecisionReceiptV1.model_json_schema()
    assert schema["additionalProperties"] is False
    assert unconstrained_string_paths(schema) == []

    # The two protected fields the runtime's audit record carries by design.
    assert "message" not in schema["properties"]
    assert "operator" not in schema["properties"]
    for forbidden in ("screenshot", "ocr", "patient", "mrn"):
        assert forbidden not in json.dumps(schema).lower()


@pytest.mark.parametrize("category", sorted(FORBIDDEN_EVIDENCE_FIELDS))
def test_receipt_rejects_each_forbidden_category(category: str) -> None:
    """The same enumerated categories the task refuses, refused structurally."""
    base = HumanDecisionReceiptV1.model_validate(_receipt_fields()).model_dump(
        mode="json"
    )
    for field, value in FORBIDDEN_EVIDENCE_FIELDS[category].items():
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            HumanDecisionReceiptV1.model_validate({**base, field: value})


@pytest.mark.parametrize(
    "value",
    [
        "J DOE MRN 0093211 XY",
        "2026-07-26 patient jd",
        "coverage active as of",
        "2026-07-26T12:00:00",
    ],
)
def test_receipt_timestamps_cannot_smuggle_text_or_drop_their_offset(
    value: str,
) -> None:
    """``decided_at`` carries the same RFC 3339 pattern 0.6.1 established.

    The pattern lives on the field, so it reaches the exported schema and a
    non-Python consumer inherits it rather than only a length bound.
    """
    with pytest.raises(ValidationError):
        HumanDecisionReceiptV1.model_validate(
            {**_receipt_fields(), "decided_at": value}
        )

    exported = HumanDecisionReceiptV1.model_json_schema()["properties"]["decided_at"]
    assert exported["pattern"] == (
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$"
    )


def test_receipt_delivery_uncertainty_is_a_first_class_value() -> None:
    """ "May have been sent" must be statable as a terminal outcome.

    The acceptance criterion is that losing the network after a tap shows
    pending/uncertain and never success. A receipt that could not express
    uncertainty would force the producer to pick success or refusal, so the
    three-state vocabulary is asserted on both the task and the receipt.
    """
    assert {state.value for state in HumanDecisionDeliveryState} == {
        "not_delivered",
        "delivered",
        "unknown",
    }

    uncertain = HumanDecisionReceiptV1.model_validate(
        {
            **_receipt_fields(),
            "state": HumanDecisionReceiptState.DELIVERY_UNCERTAIN,
            "reason_code": HumanDecisionReceiptReason.DELIVERY_UNCERTAIN,
            "report_success": None,
            "transition_receipt_digest": None,
        }
    )
    assert uncertain.model_dump(mode="json")["state"] == "delivery_uncertain"
    assert uncertain.succeeded is False

    pending = HumanDecisionReceiptV1.model_validate(
        {
            **_receipt_fields(),
            "state": HumanDecisionReceiptState.ACCEPTED_PENDING_RUNNER,
            "reason_code": HumanDecisionReceiptReason.PENDING_RUNNER,
            "report_success": None,
        }
    )
    assert pending.succeeded is False

    for required in ("state", "reason_code"):
        fields = _receipt_fields()
        del fields[required]
        with pytest.raises(ValidationError, match=required):
            HumanDecisionReceiptV1.model_validate(fields)


def test_receipt_state_and_reason_code_cannot_be_paired_arbitrarily() -> None:
    """Two fields, but not two independent fields.

    They are split because one state has more than one cause: ``completed`` is
    reached both by resuming after verification and by resuming after a skip,
    and a consumer must tell those apart without parsing prose. Leaving the
    pair unconstrained would let a producer emit ``completed`` with reason
    ``expired``, so the permitted combinations are pinned.
    """
    assert set(HUMAN_DECISION_RECEIPT_REASONS) == set(HumanDecisionReceiptState)
    covered = set().union(*HUMAN_DECISION_RECEIPT_REASONS.values())
    assert covered == set(HumanDecisionReceiptReason)

    for state, reasons in HUMAN_DECISION_RECEIPT_REASONS.items():
        for reason in HumanDecisionReceiptReason:
            fields = {
                **_receipt_fields(),
                "state": state,
                "reason_code": reason,
                "report_success": None,
            }
            if reason in reasons:
                assert HumanDecisionReceiptV1.model_validate(fields).state is state
            else:
                with pytest.raises(ValidationError, match="is not a cause of state"):
                    HumanDecisionReceiptV1.model_validate(fields)


def test_receipt_cannot_report_success_outside_a_success_state() -> None:
    """A non-success terminal outcome can never carry ``report_success``.

    "Runnable is not certified": an uncertain delivery, a halt, or a refusal
    must not be renderable as a success by a consumer that only reads the
    boolean.
    """
    assert HUMAN_DECISION_RECEIPT_SUCCESS_STATES == frozenset(
        {HumanDecisionReceiptState.COMPLETED}
    )
    for state in HumanDecisionReceiptState:
        reason = sorted(HUMAN_DECISION_RECEIPT_REASONS[state], key=lambda r: r.value)[0]
        fields = {
            **_receipt_fields(),
            "state": state,
            "reason_code": reason,
            "report_success": True,
        }
        if state in HUMAN_DECISION_RECEIPT_SUCCESS_STATES:
            assert HumanDecisionReceiptV1.model_validate(fields).report_success is True
        else:
            with pytest.raises(ValidationError, match="report_success cannot be true"):
                HumanDecisionReceiptV1.model_validate(fields)


def test_receipt_action_reuses_the_portable_task_vocabulary() -> None:
    """A consumer compares the receipt against the task without translating.

    The engine's internal ``continue`` has no representation here; the only
    action names are the ones a task advertises in ``allowed_actions``.
    """
    receipt_action = HumanDecisionReceiptV1.model_json_schema()["properties"]["action"]
    assert receipt_action["$ref"] == "#/$defs/HumanDecisionAction"
    assert {action.value for action in HumanDecisionAction} == {
        "verify_and_resume",
        "skip",
        "reject",
        "teach",
        "escalate",
    }
    with pytest.raises(ValidationError):
        HumanDecisionReceiptV1.model_validate(
            {**_receipt_fields(), "action": "continue"}
        )


def test_packaged_receipt_schema_matches_the_exported_contract() -> None:
    schema = HumanDecisionReceiptV1.model_json_schema()
    packaged = files("openadapt_types.schemas").joinpath(
        "human-decision-receipt-v1.json"
    )
    assert json.loads(packaged.read_text()) == schema


#: One frozen vector pinning the receipt canonicalization, exactly as the task
#: contract does. A cross-language implementation is correct when it reproduces
#: these bytes, this digest, and this signature hex.
RECEIPT_VECTOR_BYTES = (
    b'{"action":"verify_and_resume",'
    b'"capability_digest":"sha256:' + b"a" * 64 + b'",'
    b'"decided_at":"2026-07-26T12:03:00Z",'
    b'"decision_digest":"sha256:' + b"d" * 64 + b'",'
    b'"pause_id":"pause_1234567","reason_code":"verified_and_resumed",'
    b'"report_success":true,'
    b'"request_digest":"sha256:' + b"c" * 64 + b'",'
    b'"schema_version":"openadapt.human-decision-receipt/v1",'
    b'"signature_algorithm":"hmac-sha256","state":"completed",'
    b'"task_id":"task_12345678","task_revision":1,'
    b'"transition_receipt_digest":"sha256:' + b"e" * 64 + b'"}'
)
RECEIPT_VECTOR_DIGEST = (
    "sha256:1fc191fc298e155c305a744e816bf58c09ac07a68b9096d13f34aa0eba6797c6"
)
RECEIPT_VECTOR_SIGNATURE = (
    "hmac-sha256:385e7802b4c1ec8ff557f4c15e8b1e8c63d87fbffb08b9d61df27c59e9c44c34"
)


def test_receipt_canonical_bytes_and_signature_are_pinned_for_other_languages() -> None:
    receipt = sign_human_decision_receipt_hmac(key=b"k" * 32, fields=_receipt_fields())

    assert receipt.canonical_unsigned_bytes() == RECEIPT_VECTOR_BYTES
    assert receipt.digest == RECEIPT_VECTOR_DIGEST
    assert receipt.signature == RECEIPT_VECTOR_SIGNATURE
    assert receipt.verify_hmac(b"k" * 32)


def test_receipt_canonicalization_matches_the_documented_rules() -> None:
    """Assert the properties a reimplementation has to match, not just bytes."""
    canonical = sign_human_decision_receipt_hmac(
        key=b"k" * 32, fields=_receipt_fields()
    ).canonical_unsigned_bytes()

    assert canonical.decode("ascii")
    assert b", " not in canonical and b": " not in canonical
    payload = json.loads(canonical)
    assert list(payload) == sorted(payload)
    assert "signature" not in payload
    # Optional fields are present with an explicit null, never omitted.
    nullable = json.loads(
        sign_human_decision_receipt_hmac(
            key=b"k" * 32,
            fields={
                **_receipt_fields(),
                "transition_receipt_digest": None,
                "report_success": None,
            },
        ).canonical_unsigned_bytes()
    )
    assert nullable["transition_receipt_digest"] is None
    assert nullable["report_success"] is None


def test_receipt_signature_is_domain_separated_from_the_task_signature() -> None:
    """A task signature can never be replayed as a receipt signature."""
    key = b"k" * 32
    receipt = sign_human_decision_receipt_hmac(key=key, fields=_receipt_fields())

    assert receipt.verify_hmac(key)
    assert not receipt.verify_hmac(b"j" * 32)

    tampered = receipt.model_copy(update={"state": HumanDecisionReceiptState.REFUSED})
    assert not tampered.verify_hmac(key)

    # Same canonical payload, task domain instead of the receipt domain.
    task_domain_signature = (
        "hmac-sha256:"
        + hmac.new(
            key,
            b"openadapt.human-decision-task/v1\x00"
            + receipt.canonical_unsigned_bytes(),
            hashlib.sha256,
        ).hexdigest()
    )
    assert task_domain_signature != receipt.signature
    assert not receipt.model_copy(
        update={"signature": task_domain_signature}
    ).verify_hmac(key)


def test_unsigned_receipt_never_verifies() -> None:
    """The local loopback receipt is unsigned; it must not pass as signed."""
    unsigned = HumanDecisionReceiptV1.model_validate(_receipt_fields())
    assert unsigned.signature is None
    assert not unsigned.verify_hmac(b"k" * 32)

    with pytest.raises(ValueError, match="at least 32 bytes"):
        unsigned.verify_hmac(b"short")
    with pytest.raises(ValueError, match="must not contain a signature"):
        sign_human_decision_receipt_hmac(
            key=b"k" * 32,
            fields={**_receipt_fields(), "signature": "hmac-sha256:" + "0" * 64},
        )


#: The exact JSON body ``openadapt-flow``'s console decision route returns
#: (``console/human_decisions.decision_receipt(...).model_dump(mode="json")``),
#: transcribed from its closed local model. Pinning the producer's real payload
#: is what makes this a shared contract rather than a parallel one: if Flow's
#: half drifts, this fixture stops validating here.
FLOW_WIRE_RECEIPT: dict[str, object] = {
    "schema_version": "openadapt.human-decision-receipt/v1",
    "task_id": "task_ab12cd34ef56",
    "task_revision": 1,
    "pause_id": "ab12cd34ef56",
    "capability_digest": "sha256:" + "1" * 64,
    "request_digest": "sha256:" + "2" * 64,
    "decision_digest": "sha256:" + "3" * 64,
    "transition_receipt_digest": "sha256:" + "4" * 64,
    "action": "verify_and_resume",
    "state": "completed",
    "reason_code": "verified_and_resumed",
    "report_success": True,
    "decided_at": "2026-07-26T12:03:04.123456+00:00",
}


def test_the_producers_real_wire_payload_validates_unchanged() -> None:
    receipt = HumanDecisionReceiptV1.model_validate(FLOW_WIRE_RECEIPT)

    assert receipt.action is HumanDecisionAction.VERIFY_AND_RESUME
    assert receipt.state is HumanDecisionReceiptState.COMPLETED
    assert receipt.reason_code is HumanDecisionReceiptReason.VERIFIED_AND_RESUMED
    assert receipt.succeeded is True
    # A microsecond RFC 3339 instant with a numeric offset is what the runtime
    # emits; the pattern must accept it without reformatting the signed bytes.
    assert receipt.decided_at == FLOW_WIRE_RECEIPT["decided_at"]

    # The producer supplies every required field; the only fields it omits are
    # the two optional signing fields, which the local loopback lane does not
    # use. Nothing else may be missing, and nothing extra may be added.
    supplied = set(FLOW_WIRE_RECEIPT)
    declared = set(HumanDecisionReceiptV1.model_fields)
    assert supplied <= declared
    assert declared - supplied == {"signature_algorithm", "signature"}
    required = {
        name
        for name, field in HumanDecisionReceiptV1.model_fields.items()
        if field.is_required()
    }
    assert required <= supplied


def test_a_task_can_advertise_the_complete_action_vocabulary() -> None:
    """``max_length`` bounds the vocabulary, never the offer.

    A pause that is skippable, re-verifiable, rejectable, teachable, and
    escalatable is a legitimate pause. When ``reject`` was added, a bound left
    at four would have silently made the most permissive pause unrepresentable
    -- a refusal to issue the task at all, in the exact case the operator has
    the most choice.
    """
    fields = _task_fields()
    fields["allowed_actions"] = tuple(HumanDecisionAction)
    task = sign_human_decision_task_hmac(key=b"k" * 32, fields=fields)
    assert len(task.allowed_actions) == len(HumanDecisionAction) == 5
    assert task.verify_hmac(b"k" * 32)


def test_rejected_is_terminal_and_distinct_from_escalated() -> None:
    """Parking a run and ending it must not project to the same receipt.

    ``escalate`` leaves the durable pause intact for a colleague; ``reject``
    ends the run. A consumer reads the pair to decide whether to tell the
    operator that someone will pick this up, and the two answers are opposite.
    Neither is a success, and neither may carry ``report_success``.
    """
    rejected = HumanDecisionReceiptV1.model_validate(
        {
            **_receipt_fields(),
            "action": HumanDecisionAction.REJECT,
            "state": HumanDecisionReceiptState.REJECTED,
            "reason_code": HumanDecisionReceiptReason.REJECTED_BY_OPERATOR,
            "report_success": None,
        }
    )
    assert rejected.state is not HumanDecisionReceiptState.ESCALATED
    assert rejected.state is not HumanDecisionReceiptState.HALTED
    assert not rejected.succeeded

    for state in (
        HumanDecisionReceiptState.ESCALATED,
        HumanDecisionReceiptState.HALTED,
        HumanDecisionReceiptState.COMPLETED,
    ):
        with pytest.raises(ValidationError, match="is not a cause of state"):
            HumanDecisionReceiptV1.model_validate(
                {
                    **_receipt_fields(),
                    "action": HumanDecisionAction.REJECT,
                    "state": state,
                    "reason_code": HumanDecisionReceiptReason.REJECTED_BY_OPERATOR,
                    "report_success": None,
                }
            )

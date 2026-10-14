"""Contract tests for portable finite business decisions."""

from __future__ import annotations

import json
from importlib.resources import files

import pytest
from pydantic import ValidationError

from openadapt_types import (
    BusinessDecisionAnswerReceiptState,
    BusinessDecisionAnswerV1,
    BusinessDecisionDeliveryPolicyV1,
    BusinessDecisionPresentationV1,
    BusinessDecisionTaskV1,
    ExecuteDecisionRequiredWebhookV1,
    ExecuteWebhookEventTypeV1,
    HumanDecisionAction,
    HumanDecisionDeliveryState,
    HumanDecisionEvidenceSummaryV1,
    HumanDecisionQuestionTemplate,
    HumanDecisionQuestionV1,
    HumanDecisionRequiredAuthn,
    HumanDecisionSafeSlotsV1,
    HumanDecisionTaskKind,
    HumanDecisionTaskV1,
    sign_business_decision_answer_hmac,
    sign_business_decision_answer_receipt_hmac,
    sign_business_decision_delivery_policy_hmac,
    sign_business_decision_task_hmac,
    sign_execute_webhook_hmac,
    sign_human_decision_task_hmac,
    validate_business_decision_answer,
    validate_business_decision_delivery,
)

KEY = b"k" * 32


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _task_fields() -> dict[str, object]:
    return {
        "task_id": "task_12345678",
        "task_revision": 3,
        "tenant_id": "tenant_12345678",
        "runner_id": "runner_12345678",
        "run_id": "run_1234567890",
        "pause_id": "pause_12345678",
        "pause_binding_digest": _digest("0"),
        "request_id": "request_123456",
        "request_revision": 2,
        "request_digest": _digest("1"),
        "supersedes_request_digest": _digest("2"),
        "bundle_digest": _digest("3"),
        "workflow_contract_digest": _digest("5"),
        "governed_runtime_inputs_digest": _digest("6"),
        "decision_contract_digest": _digest("7"),
        "decision_contract_revision": 4,
        "delivery_policy_digest": _digest("4"),
        "program_scope_digest": _digest("8"),
        "control_frames_digest": _digest("9"),
        "presentation_ref": "present_12345678",
        "presentation_digest": _digest("a"),
        "options": (
            {"option_id": "approve", "target_binding_digest": _digest("b")},
            {"option_id": "decline", "target_binding_digest": _digest("c")},
        ),
        "authorized_role_refs": ("role_approver_01", "role_reviewer_01"),
        "authorized_route_refs": ("route_mobile_001",),
        "authorized_answer_issuer_key_ids": ("cloud_signing_001",),
        "role_mapping_digest": "hmac-sha256:" + "d" * 64,
        "required_authn": "aal2",
        "delivery_mode": "remote_answerable",
        "local_evidence_required": False,
        "required_evidence_count": 0,
        "relay_capability_digest": _digest("e"),
        "idempotency_scope_digest": _digest("f"),
        "created_at": "2026-08-08T12:00:00Z",
        "expires_at": "2026-08-08T12:05:00Z",
        "issuer_key_id": "runner_signing_01",
    }


def _answer_fields(task: BusinessDecisionTaskV1) -> dict[str, object]:
    return {
        "task_id": task.task_id,
        "task_revision": task.task_revision,
        "task_digest": task.digest,
        "request_digest": task.request_digest,
        "option_id": "approve",
        "idempotency_key": "answer_attempt_01",
        "authenticated_principal_ref": "principal_123456",
        "authenticated_role_ref": task.authorized_role_refs[0],
        "authn_assurance": task.required_authn,
        "authenticated_route_ref": task.authorized_route_refs[0],
        "role_mapping_digest": task.role_mapping_digest,
        "authentication_context_digest": _digest("0"),
        "answered_at": "2026-08-08T12:01:00Z",
        "issuer_key_id": "cloud_signing_001",
    }


def _operational_task_fields() -> dict[str, object]:
    return {
        "task_id": "task_operational_01",
        "run_id": "run_operational_001",
        "pause_id": "pause_operational_01",
        "capability_digest": _digest("1"),
        "bundle_digest": _digest("2"),
        "task_kind": HumanDecisionTaskKind.IDENTITY,
        "delivery_state": HumanDecisionDeliveryState.NOT_DELIVERED,
        "question": HumanDecisionQuestionV1(
            template=HumanDecisionQuestionTemplate.CONFIRM_IDENTITY,
            safe_slots=HumanDecisionSafeSlotsV1(required_signal_count=2),
        ),
        "evidence": HumanDecisionEvidenceSummaryV1(identity_required_count=2),
        "allowed_actions": (HumanDecisionAction.VERIFY_AND_RESUME,),
        "required_authn": HumanDecisionRequiredAuthn.LOCAL_SESSION,
        "created_at": "2026-08-08T12:00:00Z",
        "expires_at": "2026-08-08T12:05:00Z",
        "nonce": "nonce_operational_01",
        "issuer_key_id": "runner_signing_01",
    }


def test_signed_task_binds_the_complete_remote_authority_context() -> None:
    task = sign_business_decision_task_hmac(key=KEY, fields=_task_fields())
    assert task.verify_hmac(KEY)

    changes = {
        "tenant_id": "tenant_87654321",
        "runner_id": "runner_87654321",
        "run_id": "run_8765432100",
        "pause_id": "pause_87654321",
        "pause_binding_digest": _digest("a"),
        "request_digest": _digest("0"),
        "bundle_digest": _digest("0"),
        "workflow_contract_digest": _digest("0"),
        "decision_contract_digest": _digest("0"),
        "decision_contract_revision": 5,
        "authorized_role_refs": ("role_reviewer_01",),
        "authorized_route_refs": ("route_mobile_002",),
        "authorized_answer_issuer_key_ids": ("cloud_signing_002",),
        "expires_at": "2026-08-08T12:06:00Z",
        "idempotency_scope_digest": _digest("0"),
    }
    for field, value in changes.items():
        assert not task.model_copy(update={field: value}).verify_hmac(KEY)


def test_answer_admission_requires_exact_option_role_revision_and_expiry() -> None:
    task = sign_business_decision_task_hmac(key=KEY, fields=_task_fields())
    answer = sign_business_decision_answer_hmac(
        key=KEY,
        fields=_answer_fields(task),
    )
    validate_business_decision_answer(
        task,
        answer,
        task_signing_key=KEY,
        answer_signing_key=KEY,
        at="2026-08-08T12:01:01Z",
    )

    refused = {
        "option_id": "other",
        "authenticated_role_ref": "role_unknown_000",
        "task_revision": 2,
        "request_digest": _digest("0"),
        "authn_assurance": "local_enterprise_identity",
        "authenticated_route_ref": "route_unknown_01",
        "issuer_key_id": "cloud_signing_999",
    }
    for field, value in refused.items():
        changed = sign_business_decision_answer_hmac(
            key=KEY,
            fields={**_answer_fields(task), field: value},
        )
        with pytest.raises(ValueError):
            validate_business_decision_answer(
                task,
                changed,
                task_signing_key=KEY,
                answer_signing_key=KEY,
                at="2026-08-08T12:01:01Z",
            )

    with pytest.raises(ValueError, match="expired"):
        validate_business_decision_answer(
            task,
            answer,
            task_signing_key=KEY,
            answer_signing_key=KEY,
            at="2026-08-08T12:05:01Z",
        )


def test_local_context_task_cannot_be_answered_remotely() -> None:
    task = sign_business_decision_task_hmac(
        key=KEY,
        fields={
            **_task_fields(),
            "delivery_mode": "local_answer_required",
            "local_evidence_required": True,
            "required_evidence_count": 1,
        },
    )
    answer = sign_business_decision_answer_hmac(key=KEY, fields=_answer_fields(task))
    with pytest.raises(ValueError, match="local answer"):
        validate_business_decision_answer(
            task,
            answer,
            task_signing_key=KEY,
            answer_signing_key=KEY,
            at="2026-08-08T12:01:01Z",
        )


def test_answer_admission_authenticates_both_signed_envelopes() -> None:
    task = sign_business_decision_task_hmac(key=KEY, fields=_task_fields())
    answer = sign_business_decision_answer_hmac(key=KEY, fields=_answer_fields(task))

    with pytest.raises(ValueError, match="task signature"):
        validate_business_decision_answer(
            task,
            answer,
            task_signing_key=b"x" * 32,
            answer_signing_key=KEY,
            at="2026-08-08T12:01:01Z",
        )
    with pytest.raises(ValueError, match="answer signature"):
        validate_business_decision_answer(
            task,
            answer,
            task_signing_key=KEY,
            answer_signing_key=b"x" * 32,
            at="2026-08-08T12:01:01Z",
        )

    with pytest.raises(ValidationError, match="protected local evidence"):
        sign_business_decision_task_hmac(
            key=KEY,
            fields={
                **_task_fields(),
                "delivery_mode": "remote_answerable",
                "local_evidence_required": True,
                "required_evidence_count": 1,
            },
        )


def test_renewal_requires_one_exact_predecessor_binding() -> None:
    with pytest.raises(ValidationError, match="first request revision"):
        sign_business_decision_task_hmac(
            key=KEY,
            fields={**_task_fields(), "request_revision": 1},
        )
    with pytest.raises(ValidationError, match="bind its predecessor"):
        sign_business_decision_task_hmac(
            key=KEY,
            fields={**_task_fields(), "supersedes_request_digest": None},
        )


def test_runner_receipt_never_reports_business_success() -> None:
    task = sign_business_decision_task_hmac(key=KEY, fields=_task_fields())
    answer = sign_business_decision_answer_hmac(key=KEY, fields=_answer_fields(task))
    receipt = sign_business_decision_answer_receipt_hmac(
        key=KEY,
        fields={
            "task_id": task.task_id,
            "task_revision": task.task_revision,
            "task_digest": task.digest,
            "request_digest": task.request_digest,
            "answer_digest": answer.digest,
            "option_id": answer.option_id,
            "state": "answer_recorded",
            "reason_code": "recorded_pending_revalidation",
            "runner_decision_receipt_digest": _digest("1"),
            "decided_at": "2026-08-08T12:01:01Z",
            "issuer_key_id": "runner_signing_01",
        },
    )
    assert receipt.verify_hmac(KEY)
    assert receipt.state is BusinessDecisionAnswerReceiptState.ANSWER_RECORDED
    assert receipt.succeeded is False
    assert "verified" not in json.dumps(receipt.model_dump(mode="json"))
    assert "report_success" not in type(receipt).model_fields


def test_delivery_policy_binds_reviewed_copy_and_remote_authority() -> None:
    review_digest = _digest("9")
    presentation = BusinessDecisionPresentationV1(
        presentation_ref="present_12345678",
        presentation_revision=1,
        decision_contract_digest=_digest("7"),
        decision_contract_revision=4,
        category={
            "text": "Schedule exception",
            "classification": "reviewed_remote_safe",
            "egress_review_digest": review_digest,
        },
        title={
            "text": "Choose the reviewed path",
            "classification": "reviewed_remote_safe",
            "egress_review_digest": review_digest,
        },
        role_label={
            "text": "Scheduling lead",
            "classification": "reviewed_remote_safe",
            "egress_review_digest": review_digest,
        },
        question={
            "text": "Which reviewed path should continue?",
            "classification": "reviewed_remote_safe",
            "egress_review_digest": review_digest,
        },
        why_judgment_needed={
            "text": "The local policy leaves this exception to the scheduling lead.",
            "classification": "reviewed_remote_safe",
            "egress_review_digest": review_digest,
        },
        context_cards=(
            {
                "context_id": "context_policy_01",
                "kind": "policy",
                "label": {
                    "text": "Policy",
                    "classification": "reviewed_remote_safe",
                    "egress_review_digest": review_digest,
                },
                "value": {
                    "text": "Use the approved exception path.",
                    "classification": "reviewed_remote_safe",
                    "egress_review_digest": review_digest,
                },
            },
        ),
        options=(
            {
                "option_id": "approve",
                "label": {
                    "text": "Approve",
                    "classification": "reviewed_remote_safe",
                    "egress_review_digest": review_digest,
                },
                "detail": {
                    "text": "Continue to the qualified exception branch.",
                    "classification": "reviewed_remote_safe",
                    "egress_review_digest": review_digest,
                },
                "consequence": {
                    "text": "The runner will revalidate the live state before action.",
                    "classification": "reviewed_remote_safe",
                    "egress_review_digest": review_digest,
                },
            },
            {
                "option_id": "decline",
                "label": {
                    "text": "Decline",
                    "classification": "reviewed_remote_safe",
                    "egress_review_digest": review_digest,
                },
            },
        ),
        reason_codes=("institutional_knowledge_required", "policy_exception"),
        review_contract_digest=_digest("8"),
    )
    policy = sign_business_decision_delivery_policy_hmac(
        key=KEY,
        fields={
            "policy_ref": "policy_12345678",
            "policy_revision": 1,
            "decision_contract_digest": presentation.decision_contract_digest,
            "decision_contract_revision": presentation.decision_contract_revision,
            "presentation_ref": presentation.presentation_ref,
            "presentation_digest": presentation.digest,
            "presentation_egress_review_digest": review_digest,
            "authorized_role_refs": ("role_approver_01", "role_reviewer_01"),
            "authorized_route_refs": ("route_mobile_001",),
            "authorized_answer_issuer_key_ids": ("cloud_signing_001",),
            "role_mapping_digest": "hmac-sha256:" + "d" * 64,
            "required_authn": "aal2",
            "delivery_mode": "remote_answerable",
            "relay_capability_digest": _digest("e"),
            "created_at": "2026-08-08T12:00:00Z",
            "expires_at": "2026-08-08T12:05:00Z",
            "issuer_key_id": "qualification_key_01",
        },
    )
    assert policy.verify_hmac(KEY)
    assert policy.presentation_digest == presentation.digest

    task = sign_business_decision_task_hmac(
        key=KEY,
        fields={
            **_task_fields(),
            "delivery_policy_digest": policy.digest,
            "presentation_digest": presentation.digest,
        },
    )
    validate_business_decision_delivery(
        task,
        presentation,
        policy,
        task_signing_key=KEY,
        qualification_signing_key=KEY,
        at="2026-08-08T12:01:00Z",
    )

    changed_presentation = presentation.model_copy(
        update={
            "question": presentation.question.model_copy(
                update={"text": "Which other reviewed path should continue?"}
            )
        }
    )
    with pytest.raises(ValueError, match="presentation_digest"):
        validate_business_decision_delivery(
            task,
            changed_presentation,
            policy,
            task_signing_key=KEY,
            qualification_signing_key=KEY,
            at="2026-08-08T12:01:00Z",
        )


def test_short_answer_idempotency_key_is_refused() -> None:
    task = sign_business_decision_task_hmac(key=KEY, fields=_task_fields())
    with pytest.raises(ValidationError, match="idempotency_key"):
        sign_business_decision_answer_hmac(
            key=KEY,
            fields={**_answer_fields(task), "idempotency_key": "too-short"},
        )


def test_cross_language_digest_and_signature_vectors_are_stable() -> None:
    task = sign_business_decision_task_hmac(key=KEY, fields=_task_fields())
    answer = sign_business_decision_answer_hmac(key=KEY, fields=_answer_fields(task))
    receipt = sign_business_decision_answer_receipt_hmac(
        key=KEY,
        fields={
            "task_id": task.task_id,
            "task_revision": task.task_revision,
            "task_digest": task.digest,
            "request_digest": task.request_digest,
            "answer_digest": answer.digest,
            "option_id": answer.option_id,
            "state": "answer_recorded",
            "reason_code": "recorded_pending_revalidation",
            "runner_decision_receipt_digest": _digest("1"),
            "decided_at": "2026-08-08T12:01:01Z",
            "issuer_key_id": "runner_signing_01",
        },
    )

    assert (task.digest, task.signature) == (
        "sha256:79d37ef8c8a913e171f9703119337f5ddfefb62808d1154904b428bf0a31c8a4",
        "hmac-sha256:347549b44bf01b10cf0f41ee15034cfca49c4b851797caff95b7ae473a37531d",
    )
    assert (answer.digest, answer.signature) == (
        "sha256:2c84e3c2adda8c234131aa9920e54da4ac025a86e9c51dba3ec06ab2b60e6153",
        "hmac-sha256:cdb985b640e7cc3c668a9a7d0e78e12990de1e247a6edc6b6bfaa31e0567f412",
    )
    assert (receipt.digest, receipt.signature) == (
        "sha256:70a83847d4b77d284953e9b0e148436488c8648237e01b27ca4a91ea641a63be",
        "hmac-sha256:7cdb2795d0bb896757eaaf8c148bdc3240e870b243e2a94fdea87cc37f1bbe81",
    )


def _unconstrained_string_paths(schema: object) -> list[str]:
    paths: list[str] = []

    def visit(node: object, path: str) -> None:
        if isinstance(node, dict):
            if node.get("type") == "string" and not (
                {"pattern", "const", "enum"} & set(node)
            ):
                paths.append(path)
            for key, value in node.items():
                visit(value, f"{path}/{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                visit(value, f"{path}/{index}")

    visit(schema, "")
    return paths


@pytest.mark.parametrize(
    "model,filename",
    [
        (BusinessDecisionTaskV1, "business-decision-task-v1.json"),
        (BusinessDecisionAnswerV1, "business-decision-answer-v1.json"),
        (BusinessDecisionDeliveryPolicyV1, "business-decision-delivery-policy-v1.json"),
    ],
)
def test_portable_contract_has_no_free_text_or_sensitive_extension(
    model: type[
        BusinessDecisionTaskV1
        | BusinessDecisionAnswerV1
        | BusinessDecisionDeliveryPolicyV1
    ],
    filename: str,
) -> None:
    schema = model.model_json_schema()
    assert schema["additionalProperties"] is False
    assert _unconstrained_string_paths(schema) == []
    encoded = json.dumps(schema).lower()
    for term in ("screenshot", "ocr", "patient", "claim", "loan", "message"):
        assert term not in encoded
    packaged = files("openadapt_types.schemas").joinpath(filename)
    assert json.loads(packaged.read_text()) == schema


def test_unknown_live_context_fields_are_structurally_refused() -> None:
    task = sign_business_decision_task_hmac(key=KEY, fields=_task_fields())
    payload = task.model_dump(mode="json")
    for field, value in {
        "screenshot": "data:image/png;base64,secret",
        "record_id": "ABC-123",
        "question": "Should this person be approved?",
        "option_label": "Approve the claim",
    }.items():
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            BusinessDecisionTaskV1.model_validate({**payload, field: value})


def test_remote_delivery_refuses_unreviewed_arbitrary_human_text() -> None:
    base = {
        "presentation_ref": "present_12345678",
        "presentation_revision": 1,
        "decision_contract_digest": _digest("7"),
        "decision_contract_revision": 4,
        "question": {
            "text": "Approve live patient John Smith, MRN 123456?",
            "classification": "local_only",
        },
        "options": (
            {
                "option_id": "approve",
                "label": {
                    "text": "Approve John Smith",
                    "classification": "local_only",
                },
            },
            {
                "option_id": "decline",
                "label": {
                    "text": "Decline",
                    "classification": "local_only",
                },
            },
        ),
        "review_contract_digest": _digest("8"),
    }
    presentation = BusinessDecisionPresentationV1.model_validate(base)
    review_digest = _digest("9")
    policy = sign_business_decision_delivery_policy_hmac(
        key=KEY,
        fields={
            "policy_ref": "policy_12345678",
            "policy_revision": 1,
            "decision_contract_digest": presentation.decision_contract_digest,
            "decision_contract_revision": presentation.decision_contract_revision,
            "presentation_ref": presentation.presentation_ref,
            "presentation_digest": presentation.digest,
            "presentation_egress_review_digest": review_digest,
            "authorized_role_refs": ("role_approver_01", "role_reviewer_01"),
            "authorized_route_refs": ("route_mobile_001",),
            "authorized_answer_issuer_key_ids": ("cloud_signing_001",),
            "role_mapping_digest": "hmac-sha256:" + "d" * 64,
            "required_authn": "aal2",
            "delivery_mode": "remote_answerable",
            "relay_capability_digest": _digest("e"),
            "created_at": "2026-08-08T12:00:00Z",
            "expires_at": "2026-08-08T12:05:00Z",
            "issuer_key_id": "qualification_key_01",
        },
    )
    task = sign_business_decision_task_hmac(
        key=KEY,
        fields={
            **_task_fields(),
            "delivery_policy_digest": policy.digest,
            "presentation_digest": presentation.digest,
        },
    )
    with pytest.raises(ValueError, match="local-only text"):
        validate_business_decision_delivery(
            task,
            presentation,
            policy,
            task_signing_key=KEY,
            qualification_signing_key=KEY,
            at="2026-08-08T12:01:00Z",
        )

    with pytest.raises(ValidationError, match="egress review digest"):
        BusinessDecisionPresentationV1.model_validate(
            {
                **base,
                "question": {
                    "text": "Approve live patient John Smith, MRN 123456?",
                    "classification": "reviewed_remote_safe",
                },
            }
        )


def test_option_ids_are_finite_and_unique() -> None:
    with pytest.raises(ValidationError, match="option ids must be unique"):
        sign_business_decision_task_hmac(
            key=KEY,
            fields={
                **_task_fields(),
                "options": (
                    {"option_id": "approve", "target_binding_digest": _digest("b")},
                    {"option_id": "approve", "target_binding_digest": _digest("c")},
                ),
            },
        )

    too_many = tuple(
        {
            "option_id": f"option_{index}",
            "target_binding_digest": _digest(format(index, "x")[-1]),
        }
        for index in range(33)
    )
    with pytest.raises(ValidationError):
        sign_business_decision_task_hmac(
            key=KEY,
            fields={**_task_fields(), "options": too_many},
        )


def test_execute_decision_webhook_preserves_operational_and_business_tasks() -> None:
    operational = sign_human_decision_task_hmac(
        key=KEY,
        fields=_operational_task_fields(),
    )
    operational_webhook = sign_execute_webhook_hmac(
        key=KEY,
        fields={
            "event_type": ExecuteWebhookEventTypeV1.DECISION_REQUIRED,
            "event_id": "event_operational_01",
            "delivery_attempt": 1,
            "issued_at": "2026-08-08T12:00:01Z",
            "issuer_key_id": "webhook_signing_01",
            "execution_id": "execution_operational_01",
            "decision": operational.model_dump(mode="json"),
        },
    )
    assert isinstance(operational_webhook.decision, HumanDecisionTaskV1)
    assert operational_webhook.decision == operational

    business = sign_business_decision_task_hmac(key=KEY, fields=_task_fields())
    business_webhook = sign_execute_webhook_hmac(
        key=KEY,
        fields={
            "event_type": ExecuteWebhookEventTypeV1.DECISION_REQUIRED,
            "event_id": "event_business_0001",
            "delivery_attempt": 1,
            "issued_at": "2026-08-08T12:00:01Z",
            "issuer_key_id": "webhook_signing_01",
            "execution_id": "execution_business_0001",
            "decision": business.model_dump(mode="json"),
        },
    )
    reparsed = ExecuteDecisionRequiredWebhookV1.model_validate_json(
        business_webhook.model_dump_json()
    )
    assert isinstance(reparsed.decision, BusinessDecisionTaskV1)
    assert reparsed.decision == business
    assert reparsed.verify_hmac(KEY)

    schema = json.dumps(ExecuteDecisionRequiredWebhookV1.model_json_schema())
    assert "BusinessDecisionTaskV1" in schema
    assert "HumanDecisionTaskV1" in schema
    assert '"propertyName": "schema_version"' in schema

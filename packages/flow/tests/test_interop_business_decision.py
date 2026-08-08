"""Tests for the portable typed business-decision boundary."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from openadapt_types import (
    BusinessDecisionDeliveryMode,
    BusinessDecisionPresentationV1,
    BusinessDecisionRequiredAuthn,
    sign_business_decision_answer_hmac,
    sign_business_decision_delivery_policy_hmac,
)

from openadapt_flow.interop.business_decision import (
    admit_portable_business_decision_answer,
    business_decision_role_mapping_digest,
    project_portable_business_decision_task,
    project_recorded_business_decision_answer_receipt,
)
from openadapt_flow.runtime.durable.business_decision import BusinessDecisionStore
from tests.test_business_decision import _pause

TASK_KEY = b"t" * 32
ANSWER_KEY = b"a" * 32
POLICY_KEY = b"p" * 32
ROLE_MAPPING_KEY = b"r" * 32
PRIVACY_KEY = b"v" * 32
ROLE_REFS = {
    "operator": "authz_role_0001",
    "supervisor": "authz_role_0002",
}
EGRESS_REVIEW_DIGEST = "sha256:" + "7" * 64


def _plus_one_second(value: str) -> str:
    return (datetime.fromisoformat(value) + timedelta(seconds=1)).isoformat()


def _presentation(request) -> BusinessDecisionPresentationV1:
    return BusinessDecisionPresentationV1(
        presentation_ref="presentation_01",
        presentation_revision=1,
        decision_contract_digest="sha256:" + request.decision.contract_sha256(),
        decision_contract_revision=1,
        question={
            "text": request.decision.question,
            "classification": "reviewed_remote_safe",
            "egress_review_digest": EGRESS_REVIEW_DIGEST,
        },
        options=tuple(
            {
                "option_id": option.id,
                "label": {
                    "text": option.label,
                    "classification": "reviewed_remote_safe",
                    "egress_review_digest": EGRESS_REVIEW_DIGEST,
                },
            }
            for option in request.decision.options
        ),
        review_contract_digest="sha256:" + "8" * 64,
    )


def _policy(
    request,
    presentation: BusinessDecisionPresentationV1,
    *,
    delivery_mode: str = "remote_answerable",
    relay_digest: str = "sha256:" + "3" * 64,
):
    return sign_business_decision_delivery_policy_hmac(
        key=POLICY_KEY,
        fields={
            "policy_ref": "decision_policy_01",
            "policy_revision": 1,
            "decision_contract_digest": presentation.decision_contract_digest,
            "decision_contract_revision": presentation.decision_contract_revision,
            "presentation_ref": presentation.presentation_ref,
            "presentation_digest": presentation.digest,
            "presentation_egress_review_digest": (
                EGRESS_REVIEW_DIGEST if delivery_mode == "remote_answerable" else None
            ),
            "authorized_role_refs": tuple(ROLE_REFS.values()),
            "authorized_route_refs": ("cloud_aal2_route",),
            "authorized_answer_issuer_key_ids": ("cloud_signer_001",),
            "role_mapping_digest": business_decision_role_mapping_digest(
                ROLE_REFS, key=ROLE_MAPPING_KEY
            ),
            "required_authn": "aal2",
            "delivery_mode": delivery_mode,
            "relay_capability_digest": relay_digest,
            "created_at": request.issued_at,
            "expires_at": request.expires_at,
            "issuer_key_id": "qualification_signer_01",
        },
    )


def _project(store: BusinessDecisionStore, *, delivery_mode="remote_answerable"):
    request, _ = store.read_active_request()
    presentation = _presentation(request)
    policy = _policy(request, presentation, delivery_mode=delivery_mode)
    return project_portable_business_decision_task(
        store,
        signing_key=TASK_KEY,
        tenant_id="tenant_example_01",
        runner_id="runner_example_01",
        presentation=presentation,
        delivery_policy=policy,
        delivery_policy_signing_key=POLICY_KEY,
        expected_delivery_policy_issuer_key_id="qualification_signer_01",
        role_refs=ROLE_REFS,
        role_mapping_key=ROLE_MAPPING_KEY,
        privacy_key=PRIVACY_KEY,
        active_relay_capability_digest="sha256:" + "3" * 64,
        issuer_key_id="runner_signer_01",
        at=_plus_one_second(request.issued_at),
    )


def _answer(task, request, **changes):
    fields = {
        "task_id": task.task_id,
        "task_revision": task.task_revision,
        "task_digest": task.digest,
        "request_digest": task.request_digest,
        "option_id": "accept",
        "idempotency_key": "answer_mobile_0001",
        "authenticated_principal_ref": "principal_cloud_01",
        "authenticated_role_ref": "authz_role_0001",
        "authn_assurance": BusinessDecisionRequiredAuthn.AAL2,
        "authenticated_route_ref": "cloud_aal2_route",
        "role_mapping_digest": task.role_mapping_digest,
        "authentication_context_digest": "sha256:" + "4" * 64,
        "answered_at": _plus_one_second(request.issued_at),
        "issuer_key_id": "cloud_signer_001",
    }
    fields.update(changes)
    return sign_business_decision_answer_hmac(key=ANSWER_KEY, fields=fields)


def _admit(store, task, answer, *, role_refs=ROLE_REFS):
    request, _ = store.read_active_request()
    return admit_portable_business_decision_answer(
        store,
        task,
        answer,
        task_signing_key=TASK_KEY,
        expected_task_issuer_key_id="runner_signer_01",
        answer_signing_key=ANSWER_KEY,
        expected_answer_issuer_key_id="cloud_signer_001",
        expected_tenant_id="tenant_example_01",
        expected_runner_id="runner_example_01",
        role_refs=role_refs,
        role_mapping_key=ROLE_MAPPING_KEY,
        at=_plus_one_second(request.issued_at),
    )


def test_projection_reuses_mobile_lane_without_relaying_runner_text(tmp_path) -> None:
    _workflow, store, _backend, request = _pause(tmp_path, required_evidence=False)
    task = _project(store)

    assert task.verify_hmac(TASK_KEY)
    assert task.delivery_mode is BusinessDecisionDeliveryMode.REMOTE_ANSWERABLE
    assert task.task_revision == task.request_revision == 1
    assert tuple(option.option_id for option in task.options) == ("accept", "reject")
    assert task.pause_binding_digest == request.pause_binding_sha256
    assert task.request_digest == request.digest
    assert task.run_id != request.run_id
    assert task.delivery_policy_digest.startswith("sha256:")

    payload = task.model_dump_json().lower()
    for local_text in (
        request.run_id.lower(),
        request.decision.question.lower(),
        "perform accepted path",
        "operator",
        "supervisor",
    ):
        assert local_text not in payload

    answer = _answer(task, request)
    submission, principal = _admit(store, task, answer)
    assert submission.option_id == "accept"
    assert submission.evidence_artifact_sha256s == {}
    assert principal.roles == ("operator",)
    assert principal.authenticated_by == "cloud_aal2_route"

    local_receipt = BusinessDecisionStore(store.run_dir).submit(
        submission,
        principal=principal,
        now=datetime.fromisoformat(_plus_one_second(request.issued_at)),
    )
    portable_receipt = project_recorded_business_decision_answer_receipt(
        store,
        task,
        answer,
        task_signing_key=TASK_KEY,
        expected_task_issuer_key_id="runner_signer_01",
        answer_signing_key=ANSWER_KEY,
        expected_answer_issuer_key_id="cloud_signer_001",
        signing_key=TASK_KEY,
        issuer_key_id="runner_signer_01",
        expected_tenant_id="tenant_example_01",
        expected_runner_id="runner_example_01",
        role_refs=ROLE_REFS,
        role_mapping_key=ROLE_MAPPING_KEY,
        at=_plus_one_second(request.issued_at),
    )
    assert portable_receipt.runner_decision_receipt_digest == local_receipt.digest
    assert portable_receipt.succeeded is False


def test_remote_projection_refuses_protected_evidence(tmp_path) -> None:
    _workflow, store, _backend, _request = _pause(tmp_path, required_evidence=True)
    with pytest.raises(ValueError, match="protected local evidence"):
        _project(store)

    task = _project(store, delivery_mode="local_answer_required")
    assert task.delivery_mode is BusinessDecisionDeliveryMode.LOCAL_ANSWER_REQUIRED
    assert task.local_evidence_required is True


def test_projection_refuses_changed_presentation_or_policy(tmp_path) -> None:
    _workflow, store, _backend, request = _pause(tmp_path, required_evidence=False)
    presentation = _presentation(request)
    changed = presentation.model_copy(
        update={
            "question": presentation.question.model_copy(
                update={"text": "A different question"}
            )
        }
    )
    policy = _policy(request, changed)
    with pytest.raises(ValueError, match="presentation differs"):
        project_portable_business_decision_task(
            store,
            signing_key=TASK_KEY,
            tenant_id="tenant_example_01",
            runner_id="runner_example_01",
            presentation=changed,
            delivery_policy=policy,
            delivery_policy_signing_key=POLICY_KEY,
            expected_delivery_policy_issuer_key_id="qualification_signer_01",
            role_refs=ROLE_REFS,
            role_mapping_key=ROLE_MAPPING_KEY,
            privacy_key=PRIVACY_KEY,
            active_relay_capability_digest="sha256:" + "3" * 64,
            issuer_key_id="runner_signer_01",
            at=_plus_one_second(request.issued_at),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"authn_assurance": "local_enterprise_identity"},
        {"authenticated_route_ref": "route_unknown_01"},
        {"issuer_key_id": "cloud_signer_999"},
    ],
)
def test_admission_refuses_authentication_downgrade_or_wrong_route(
    tmp_path, changes
) -> None:
    _workflow, store, _backend, request = _pause(tmp_path, required_evidence=False)
    task = _project(store)
    answer = _answer(task, request, **changes)
    with pytest.raises(ValueError):
        _admit(store, task, answer)


def test_admission_refuses_changed_local_role_mapping(tmp_path) -> None:
    _workflow, store, _backend, request = _pause(tmp_path, required_evidence=False)
    task = _project(store)
    answer = _answer(task, request)
    with pytest.raises(ValueError, match="mapping differs"):
        _admit(
            store,
            task,
            answer,
            role_refs={
                "operator": "authz_role_changed",
                "supervisor": "authz_role_0002",
            },
        )


def test_receipt_refuses_a_different_portable_answer(tmp_path) -> None:
    _workflow, store, _backend, request = _pause(tmp_path, required_evidence=False)
    task = _project(store)
    answer = _answer(task, request)
    submission, principal = _admit(store, task, answer)
    BusinessDecisionStore(store.run_dir).submit(
        submission,
        principal=principal,
        now=datetime.fromisoformat(_plus_one_second(request.issued_at)),
    )
    different_answer = _answer(
        task,
        request,
        authenticated_role_ref="authz_role_0002",
        idempotency_key="answer_mobile_0002",
        authentication_context_digest="sha256:" + "5" * 64,
    )

    with pytest.raises(ValueError, match="retained Flow receipt differs"):
        project_recorded_business_decision_answer_receipt(
            store,
            task,
            different_answer,
            task_signing_key=TASK_KEY,
            expected_task_issuer_key_id="runner_signer_01",
            answer_signing_key=ANSWER_KEY,
            expected_answer_issuer_key_id="cloud_signer_001",
            signing_key=TASK_KEY,
            issuer_key_id="runner_signer_01",
            expected_tenant_id="tenant_example_01",
            expected_runner_id="runner_example_01",
            role_refs=ROLE_REFS,
            role_mapping_key=ROLE_MAPPING_KEY,
            at=_plus_one_second(request.issued_at),
        )


def test_recorded_receipt_remains_available_after_task_expiry(tmp_path) -> None:
    _workflow, store, _backend, request = _pause(tmp_path, required_evidence=False)
    task = _project(store)
    answer = _answer(task, request)
    submission, principal = _admit(store, task, answer)
    BusinessDecisionStore(store.run_dir).submit(
        submission,
        principal=principal,
        now=datetime.fromisoformat(_plus_one_second(request.issued_at)),
    )
    after_expiry = (
        datetime.fromisoformat(request.expires_at) + timedelta(seconds=1)
    ).isoformat()

    receipt = project_recorded_business_decision_answer_receipt(
        store,
        task,
        answer,
        task_signing_key=TASK_KEY,
        expected_task_issuer_key_id="runner_signer_01",
        answer_signing_key=ANSWER_KEY,
        expected_answer_issuer_key_id="cloud_signer_001",
        signing_key=TASK_KEY,
        issuer_key_id="runner_signer_01",
        expected_tenant_id="tenant_example_01",
        expected_runner_id="runner_example_01",
        role_refs=ROLE_REFS,
        role_mapping_key=ROLE_MAPPING_KEY,
        at=after_expiry,
    )

    assert receipt.answer_digest == answer.digest
    assert receipt.succeeded is False

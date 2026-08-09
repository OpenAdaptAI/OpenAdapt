"""End-to-end contract tests for the customer-runner decision relay."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from openadapt_types import BusinessDecisionAnswerReceiptV1

from openadapt_flow.console.decision_relay import RelayUncertain
from openadapt_flow.interop.business_decision_cloud import (
    ANSWERS_POLL_PATH,
    REGISTRATIONS_PATH,
    BusinessDecisionCloudKeys,
    BusinessDecisionCloudRefused,
    BusinessDecisionCloudRelay,
    build_qualified_business_decision_cloud_relay,
)
from openadapt_flow.qualification import (
    EnvironmentBoundary,
    QualificationRefusalCode,
    evaluate_qualification,
    init_project,
    set_business_decision_deliveries,
)
from openadapt_flow.qualified_business_decisions import (
    QualifiedBusinessDecisionContextCard,
    QualifiedBusinessDecisionDelivery,
    QualifiedBusinessDecisionOptionCopy,
    business_decision_delivery_review_digest,
)
from openadapt_flow.runtime.durable.business_decision import BusinessDecisionStore
from tests.test_business_decision import _pause
from tests.test_interop_business_decision import (
    ANSWER_KEY,
    POLICY_KEY,
    ROLE_MAPPING_KEY,
    ROLE_REFS,
    RUNNER_BEARER,
    TASK_KEY,
    _answer,
    _plus_one_second,
    _policy,
    _presentation,
    _project,
)

EGRESS_REVIEW_DIGEST = "sha256:" + "0" * 64


@dataclass
class _CloudTransport:
    task: Any
    answer: Any
    lease_expires_at: str
    receipt_uncertain_once: bool = False
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def post(
        self, path: str, payload: dict[str, Any], *, timeout_s: float
    ) -> tuple[int, dict[str, Any]]:
        assert timeout_s > 0
        self.calls.append((path, payload))
        if path == REGISTRATIONS_PATH:
            return 201, {
                "accepted": True,
                "created": True,
                "state": "open",
                "task_id": self.task.task_id,
                "task_revision": self.task.task_revision,
                "task_digest": self.task.digest,
                "presentation_digest": self.task.presentation_digest,
                "one_use_scope_digest": self.task.idempotency_scope_digest,
                "answer_authority": "withheld_until_authenticated_choice",
                "local_evidence": "not_accepted_by_cloud",
            }
        if path == ANSWERS_POLL_PATH:
            return 200, {
                "delivery": {
                    "answer_id": "answer_12345678",
                    "answer": self.answer.model_dump(mode="json"),
                    "answer_digest": self.answer.digest,
                    "lease_id": "lease_12345678",
                    "lease_attempt": 1,
                    "lease_expires_at": self.lease_expires_at,
                },
                "one_use": True,
                "runner_revalidation_required": True,
                "effect_outcome": "not_reported_by_answer_delivery",
            }
        if path == "/api/business-decisions/answers/answer_12345678/receipt":
            receipt = BusinessDecisionAnswerReceiptV1.model_validate(payload["receipt"])
            if self.receipt_uncertain_once:
                self.receipt_uncertain_once = False
                raise RelayUncertain("the receipt may have arrived")
            return 200, {
                "accepted": True,
                "created": True,
                "state": receipt.state.value,
                "reason_code": receipt.reason_code.value,
                "receipt_digest": receipt.digest,
                "verified_effect": False,
            }
        raise AssertionError(f"unexpected Cloud path {path}")


def _relay(
    tmp_path,
    *,
    changed_task_digest: bool = False,
    uncertain: bool = False,
    expired_lease: bool = False,
):
    _workflow, store, _backend, request = _pause(tmp_path, required_evidence=False)
    task = _project(store)
    presentation = _presentation(request)
    policy = _policy(request, presentation)
    answer = _answer(
        task,
        request,
        **({"task_digest": "sha256:" + "0" * 64} if changed_task_digest else {}),
    )
    transport = _CloudTransport(
        task=task,
        answer=answer,
        lease_expires_at=(request.issued_at if expired_lease else request.expires_at),
        receipt_uncertain_once=uncertain,
    )
    relay = BusinessDecisionCloudRelay(
        transport,
        runner_token=RUNNER_BEARER,
        store=store,
        task=task,
        presentation=presentation,
        delivery_policy=policy,
        role_policy={
            "schema_version": "openadapt.cloud-business-decision-role-policy/v1",
            "policy_ref": "cloud_role_policy_01",
            "task_digest": task.digest,
        },
        role_refs=ROLE_REFS,
        route_ref="cloud_aal2_route",
        tenant_id="tenant_example_01",
        runner_id="runner_example_01",
        keys=BusinessDecisionCloudKeys(
            task_signing_key=TASK_KEY,
            task_issuer_key_id="runner_signer_01",
            qualification_signing_key=POLICY_KEY,
            qualification_issuer_key_id="qualification_signer_01",
            answer_signing_key=ANSWER_KEY,
            answer_issuer_key_id="cloud_signer_001",
            receipt_signing_key=TASK_KEY,
            receipt_issuer_key_id="runner_signer_01",
            role_mapping_key=ROLE_MAPPING_KEY,
        ),
    )
    return relay, transport, store, request


def test_cloud_relay_completes_one_exact_local_answer_cycle(tmp_path) -> None:
    relay, transport, store, request = _relay(tmp_path)
    at = _plus_one_second(request.issued_at)

    assert relay.publish(at=at) is True
    cycle = relay.serve_once(at=at, wait_s=0)

    assert cycle is not None
    assert cycle.receipt_confirmed is True
    retained = BusinessDecisionStore(store.run_dir).read_receipt(request.digest)
    assert retained is not None
    assert retained[0].option_id == "accept"
    assert [path for path, _payload in transport.calls] == [
        REGISTRATIONS_PATH,
        ANSWERS_POLL_PATH,
        "/api/business-decisions/answers/answer_12345678/receipt",
    ]
    registration = transport.calls[0][1]
    assert registration["runner_signature_attestation"]["envelope_digest"].startswith(
        "sha256:"
    )
    receipt_payload = transport.calls[-1][1]
    assert receipt_payload["runner_receipt_attestation"].startswith("hmac-sha256:")


def test_uncertain_receipt_can_reuse_the_same_local_answer(tmp_path) -> None:
    relay, _transport, store, request = _relay(tmp_path, uncertain=True)
    at = _plus_one_second(request.issued_at)
    assert relay.publish(at=at) is True
    delivery = relay.poll(wait_s=0)
    assert delivery is not None

    uncertain = relay.record(delivery, at=at)
    confirmed = relay.record(delivery, at=at)

    assert uncertain.receipt_confirmed is False
    assert confirmed.receipt_confirmed is True
    assert uncertain.receipt_digest == confirmed.receipt_digest
    retained = BusinessDecisionStore(store.run_dir).read_receipt(request.digest)
    assert retained is not None
    assert retained[0].option_id == "accept"


def test_cloud_relay_refuses_an_answer_for_another_task_digest(tmp_path) -> None:
    relay, _transport, store, request = _relay(tmp_path, changed_task_digest=True)
    at = _plus_one_second(request.issued_at)
    assert relay.publish(at=at) is True

    with pytest.raises(
        BusinessDecisionCloudRefused, match="differs from the local task"
    ):
        relay.poll(wait_s=0)

    assert BusinessDecisionStore(store.run_dir).read_receipt(request.digest) is None


def test_cloud_relay_refuses_an_expired_delivery_before_local_admission(
    tmp_path,
) -> None:
    relay, _transport, store, request = _relay(tmp_path, expired_lease=True)
    at = _plus_one_second(request.issued_at)
    assert relay.publish(at=at) is True
    delivery = relay.poll(wait_s=0)
    assert delivery is not None

    with pytest.raises(BusinessDecisionCloudRefused, match="lease expired"):
        relay.record(delivery, at=at)

    assert BusinessDecisionStore(store.run_dir).read_receipt(request.digest) is None


@dataclass
class _QualifiedRegistrationTransport:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def post(
        self, path: str, payload: dict[str, Any], *, timeout_s: float
    ) -> tuple[int, dict[str, Any]]:
        from openadapt_types import BusinessDecisionTaskV1

        assert timeout_s > 0
        self.calls.append((path, payload))
        task = BusinessDecisionTaskV1.model_validate(payload["task"])
        return 201, {
            "accepted": True,
            "created": True,
            "state": "open",
            "task_id": task.task_id,
            "task_revision": task.task_revision,
            "task_digest": task.digest,
            "presentation_digest": task.presentation_digest,
            "one_use_scope_digest": task.idempotency_scope_digest,
            "answer_authority": "withheld_until_authenticated_choice",
            "local_evidence": "not_accepted_by_cloud",
        }


def _qualified_delivery(workflow) -> QualifiedBusinessDecisionDelivery:
    assert workflow.program is not None
    decision = workflow.program.states["review"].decision
    assert decision is not None
    binding = QualifiedBusinessDecisionDelivery(
        graph_id="__program__",
        state_id="review",
        decision_contract_sha256=decision.contract_sha256(),
        presentation_ref="presentation_review_01",
        egress_review_digest=EGRESS_REVIEW_DIGEST,
        review_contract_digest=EGRESS_REVIEW_DIGEST,
        category="Policy decision",
        title="Choose the qualified path",
        role_label="Authorized operator",
        why_judgment_needed="A reviewed policy exception needs human judgment.",
        context_cards=(
            QualifiedBusinessDecisionContextCard(
                context_id="policy_context_01",
                kind="policy",
                label="Policy",
                value="Use the reviewed exception rule.",
            ),
        ),
        option_copy={
            "accept": QualifiedBusinessDecisionOptionCopy(
                detail="Continue on the declared accepted path.",
                consequence="Flow will recheck the live application before action.",
            ),
            "reject": QualifiedBusinessDecisionOptionCopy(
                detail="Continue on the declared rejected path.",
            ),
        },
        reason_codes=("institutional_knowledge_required",),
        policy_ref="decision_policy_01",
        role_refs=ROLE_REFS,
        authorized_route_refs=("cloud_aal2_route",),
        registration_route_ref="cloud_aal2_route",
        answer_issuer_key_id="cloud_signer_001",
        required_authn="aal2",
        relay_capability_digest="sha256:" + "3" * 64,
        qualification_issuer_key_id="qualification_signer_01",
        task_issuer_key_id="runner_signer_01",
        receipt_issuer_key_id="runner_signer_01",
    )
    digest = business_decision_delivery_review_digest(decision, binding)
    return binding.model_copy(
        update={"egress_review_digest": digest, "review_contract_digest": digest}
    )


def test_qualified_bundle_builds_exact_mobile_relay_without_manual_contracts(
    tmp_path, monkeypatch
) -> None:
    workflow, store, _backend, request = _pause(tmp_path, required_evidence=False)
    init_project(
        workflow,
        environment=EnvironmentBoundary(
            target_kind="web",
            application="Reference application",
            application_version="1",
            environment_digest="1" * 64,
            runtime_version="test",
        ),
    )
    set_business_decision_deliveries(workflow, [_qualified_delivery(workflow)])
    monkeypatch.setattr(
        "openadapt_flow.qualification.current_certification_matches",
        lambda _workflow, *, policy: True,
    )
    transport = _QualifiedRegistrationTransport()
    relay = build_qualified_business_decision_cloud_relay(
        workflow,
        object(),
        store,
        transport,
        runner_token=RUNNER_BEARER,
        tenant_id="tenant_example_01",
        runner_id="runner_example_01",
        keys=BusinessDecisionCloudKeys(
            task_signing_key=TASK_KEY,
            task_issuer_key_id="runner_signer_01",
            qualification_signing_key=POLICY_KEY,
            qualification_issuer_key_id="qualification_signer_01",
            answer_signing_key=ANSWER_KEY,
            answer_issuer_key_id="cloud_signer_001",
            receipt_signing_key=TASK_KEY,
            receipt_issuer_key_id="runner_signer_01",
            role_mapping_key=ROLE_MAPPING_KEY,
        ),
        privacy_key=b"v" * 32,
        at=_plus_one_second(request.issued_at),
    )

    assert relay.publish(at=_plus_one_second(request.issued_at)) is True
    registration = transport.calls[0][1]
    presentation = registration["presentation"]
    assert presentation["question"]["text"] == request.decision.question
    assert presentation["context_cards"][0]["kind"] == "policy"
    assert registration["delivery_policy"]["required_authn"] == "aal2"
    assert "task_signing_key" not in repr(registration)


def test_qualified_relay_refuses_without_current_certification(tmp_path) -> None:
    workflow, store, _backend, request = _pause(tmp_path, required_evidence=False)
    init_project(
        workflow,
        environment=EnvironmentBoundary(
            target_kind="web",
            application="Reference application",
            application_version="1",
            environment_digest="1" * 64,
            runtime_version="test",
        ),
    )
    set_business_decision_deliveries(workflow, [_qualified_delivery(workflow)])

    with pytest.raises(BusinessDecisionCloudRefused, match="certification"):
        build_qualified_business_decision_cloud_relay(
            workflow,
            object(),
            store,
            _QualifiedRegistrationTransport(),
            runner_token=RUNNER_BEARER,
            tenant_id="tenant_example_01",
            runner_id="runner_example_01",
            keys=BusinessDecisionCloudKeys(
                task_signing_key=TASK_KEY,
                task_issuer_key_id="runner_signer_01",
                qualification_signing_key=POLICY_KEY,
                qualification_issuer_key_id="qualification_signer_01",
                answer_signing_key=ANSWER_KEY,
                answer_issuer_key_id="cloud_signer_001",
                receipt_signing_key=TASK_KEY,
                receipt_issuer_key_id="runner_signer_01",
                role_mapping_key=ROLE_MAPPING_KEY,
            ),
            privacy_key=b"v" * 32,
            at=_plus_one_second(request.issued_at),
        )


def test_qualified_relay_refuses_a_certified_workflow_from_another_run(
    tmp_path, monkeypatch
) -> None:
    workflow, store, _backend, request = _pause(tmp_path, required_evidence=False)
    other = workflow.model_copy(deep=True)
    other.name = "another-workflow"
    init_project(
        other,
        environment=EnvironmentBoundary(
            target_kind="web",
            application="Reference application",
            application_version="1",
            environment_digest="1" * 64,
            runtime_version="test",
        ),
    )
    set_business_decision_deliveries(other, [_qualified_delivery(other)])
    monkeypatch.setattr(
        "openadapt_flow.qualification.current_certification_matches",
        lambda _workflow, *, policy: True,
    )

    with pytest.raises(BusinessDecisionCloudRefused, match="durable run"):
        build_qualified_business_decision_cloud_relay(
            other,
            object(),
            store,
            _QualifiedRegistrationTransport(),
            runner_token=RUNNER_BEARER,
            tenant_id="tenant_example_01",
            runner_id="runner_example_01",
            keys=BusinessDecisionCloudKeys(
                task_signing_key=TASK_KEY,
                task_issuer_key_id="runner_signer_01",
                qualification_signing_key=POLICY_KEY,
                qualification_issuer_key_id="qualification_signer_01",
                answer_signing_key=ANSWER_KEY,
                answer_issuer_key_id="cloud_signer_001",
                receipt_signing_key=TASK_KEY,
                receipt_issuer_key_id="runner_signer_01",
                role_mapping_key=ROLE_MAPPING_KEY,
            ),
            privacy_key=b"v" * 32,
            at=_plus_one_second(request.issued_at),
        )


def test_qualification_refuses_copy_changed_after_remote_review(tmp_path) -> None:
    workflow, _store, _backend, _request = _pause(tmp_path, required_evidence=False)
    init_project(
        workflow,
        environment=EnvironmentBoundary(
            target_kind="web",
            application="Reference application",
            application_version="1",
            environment_digest="1" * 64,
            runtime_version="test",
        ),
    )
    binding = _qualified_delivery(workflow)
    binding = binding.model_copy(
        update={"title": "Changed after the reviewed digest was created"}
    )

    with pytest.raises(ValueError, match="exact qualification review"):
        set_business_decision_deliveries(workflow, [binding])


def test_qualification_refuses_a_stale_mobile_decision_binding(tmp_path) -> None:
    workflow, _store, _backend, _request = _pause(tmp_path, required_evidence=False)
    init_project(
        workflow,
        environment=EnvironmentBoundary(
            target_kind="web",
            application="Reference application",
            application_version="1",
            environment_digest="1" * 64,
            runtime_version="test",
        ),
    )
    set_business_decision_deliveries(workflow, [_qualified_delivery(workflow)])
    assert workflow.program is not None
    state = workflow.program.states["review"]
    assert state.decision is not None
    state.decision = state.decision.model_copy(
        update={"question": "Which new declared path should continue?"}
    )

    report = evaluate_qualification(workflow)

    assert any(
        refusal.code is QualificationRefusalCode.BUSINESS_DECISION_DELIVERY_INVALID
        for refusal in report.refusals
    )

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

"""The outbound lane, its refusal matrix, and its uncertainty discipline.

Two things are proved here that a code reading cannot establish:

1. **The signature verifier agrees with the hosted signer, byte for byte.**
   ``RELAY_FIXTURE`` was produced by running the control plane's own
   ``signHumanDecisionRelay`` — its ``canonicalJson``, its digest input, its
   HMAC construction — in Node. If either side's canonicalization drifts, the
   first test in this file fails, rather than every decision silently failing
   to verify in the field.
2. **An uncertain request is never converted into a claim.** A relay that
   cannot confirm what happened reports ``unknown``; nothing in this module is
   permitted to say "delivered".
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any, Optional

import pytest

from openadapt_flow.console.attention import attention_item
from openadapt_flow.console.decision_relay import (
    RELAY_SCHEMA,
    DecisionRelay,
    PublishState,
    RelayedDecision,
    RelayRefused,
    RelayUncertain,
    _canonical,
    resolve_runner_token,
    verify_relay,
)
from openadapt_flow.deployment import DeploymentConfig
from openadapt_flow.ir import (
    ActionKind,
    Postcondition,
    PostconditionKind,
    Step,
    Workflow,
)
from openadapt_flow.runtime.durable.attended import (
    AttendedActionRefused,
    AttendedActionStore,
)
from openadapt_flow.runtime.replayer import Replayer
from tests.test_attended_actions import _ResultExecutor
from tests.test_replayer import FakeBackend, FakeVision

PROTECTED_VALUE = "Wilhelmina Featherstonehaugh"
TOKEN = "oar_" + "Z" * 40

#: Produced by the hosted control plane's own signer (Node, `signHumanDecisionRelay`
#: over `canonicalJson`), not by this repository. Regenerating it requires
#: running that code; editing it by hand defeats the point of the test below.
RELAY_FIXTURE: dict[str, Any] = {
    "schema_version": "openadapt.human-decision-relay/v2",
    "decision_id": "hdd_11111111-2222-3333-4444-555555555555",
    "tenant_id": "tenant_exact_01",
    "runner_id": "runner_exact_01",
    "task_id": "task_pause_exact_0001",
    "task_revision": 1,
    "task_digest": "sha256:" + "a" * 64,
    "task_signature": "hmac-sha256:" + "b" * 64,
    "capability_digest": "sha256:" + "c" * 64,
    "phase": "paused",
    "event_sequence": 3,
    "idempotency_scope_digest": "sha256:" + "d" * 64,
    "binding_digest": "sha256:" + "e" * 64,
    "decision_action": "verify_and_resume",
    "action": "continue",
    "idempotency_key": "relay-idempotency-key-0000001",
    "actor_id": "11111111-2222-3333-4444-666666666666",
    "assurance": "aal2",
    "submitted_at": "2026-07-27T12:00:00Z",
    "expires_at": "2026-07-27T12:30:00Z",
    "execution_authority": "customer_runtime",
    "local_revalidation_required": True,
    "signature_algorithm": "hmac-sha256",
    "relay_digest": (
        "sha256:af955186f39252dfc65c7af81f72fa0b0421256e129491eb335114c9dceb718e"
    ),
    "relay_signature": (
        "hmac-sha256:00c1b1f77cbd706c071b5d6ba2f1ee1b7d967b28415598f75cdd386d70e6c85c"
    ),
}


def _sign(unsigned: dict[str, Any], token: str = TOKEN) -> dict[str, Any]:
    """Mint one relay the way the control plane does.

    Only legitimate because the cross-language fixture test below pins this
    construction against the hosted signer's actual output.
    """
    digest = "sha256:" + hashlib.sha256(_canonical(unsigned)).hexdigest()
    body = (
        RELAY_SCHEMA.encode("utf-8")
        + b"\0"
        + _canonical({**unsigned, "relay_digest": digest})
    )
    signature = (
        "hmac-sha256:"
        + hmac.new(token.encode("utf-8"), body, hashlib.sha256).hexdigest()
    )
    return {**unsigned, "relay_digest": digest, "relay_signature": signature}


# ------------------------------------------------- cross-language signature


def test_the_verifier_agrees_with_the_hosted_signer_byte_for_byte():
    """The one test that cannot be satisfied by reading either side's code."""
    unsigned = {
        key: value
        for key, value in RELAY_FIXTURE.items()
        if key not in {"relay_digest", "relay_signature"}
    }
    minted = _sign(unsigned)
    assert minted["relay_digest"] == RELAY_FIXTURE["relay_digest"]
    assert minted["relay_signature"] == RELAY_FIXTURE["relay_signature"]
    assert verify_relay(RELAY_FIXTURE, TOKEN) is True


@pytest.mark.parametrize(
    "field,value",
    [
        ("action", "skip"),
        ("capability_digest", "sha256:" + "f" * 64),
        ("event_sequence", 4),
        ("binding_digest", "sha256:" + "0" * 64),
        ("local_revalidation_required", False),
        ("actor_id", "11111111-2222-3333-4444-777777777777"),
    ],
)
def test_altering_any_signed_field_invalidates_the_relay(field, value):
    assert verify_relay({**RELAY_FIXTURE, field: value}, TOKEN) is False


def test_a_relay_signed_for_another_runner_does_not_verify():
    assert verify_relay(RELAY_FIXTURE, "oar_" + "Y" * 40) is False


def test_an_extra_field_invalidates_the_relay():
    """A field this client does not know about is a contract disagreement."""
    assert verify_relay({**RELAY_FIXTURE, "note": PROTECTED_VALUE}, TOKEN) is False


# --------------------------------------------------------------- credentials


@pytest.mark.parametrize(
    "token",
    [
        "",
        "   ",
        "oap_" + "Z" * 43,  # a Cloud pairing secret
        "oapp_" + "Z" * 43,  # a decision-portal secret
        "oai_ingest_" + "Z" * 32,  # an ingest token
        "oar_short",
    ],
)
def test_only_a_runner_credential_is_accepted(token, monkeypatch):
    monkeypatch.delenv("OPENADAPT_RUNNER_TOKEN", raising=False)
    with pytest.raises(RelayRefused):
        resolve_runner_token(token or None)


def test_the_runner_token_comes_from_the_environment_when_not_passed(monkeypatch):
    monkeypatch.setenv("OPENADAPT_RUNNER_TOKEN", TOKEN)
    assert resolve_runner_token() == TOKEN


# --------------------------------------------------------------- transports


class FakeTransport:
    """Records every request and returns scripted responses."""

    def __init__(self, responses: Optional[dict[str, Any]] = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, path, payload, *, timeout_s):
        self.calls.append((path, payload))
        response = self.responses.get(path.split("/")[-1], (204, {}))
        if isinstance(response, Exception):
            raise response
        return response


def _deployment(**remote: Any) -> DeploymentConfig:
    return DeploymentConfig.model_validate(
        {
            "human_decisions": {
                "remote": {
                    "enabled": True,
                    "tenant_id": "tenant_exact_01",
                    "runner_id": "runner_exact_01",
                    **remote,
                }
            }
        }
    )


def _relay(deployment: Optional[DeploymentConfig] = None, **responses: Any):
    transport = FakeTransport(responses)
    return (
        DecisionRelay(transport, token=TOKEN, deployment=deployment or _deployment()),
        transport,
    )


def test_the_relay_refuses_to_start_without_an_exact_remote_binding():
    with pytest.raises(RelayRefused):
        DecisionRelay(FakeTransport(), token=TOKEN, deployment=DeploymentConfig())


# ---------------------------------------------------------- a real halted run


def _halted_run(tmp_path: Path):
    workflow = Workflow(
        name="relay-e2e",
        params={"patient": PROTECTED_VALUE},
        steps=[
            Step(
                id="human",
                intent=f"confirm coverage for {PROTECTED_VALUE}",
                action=ActionKind.KEY,
                key="A",
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT,
                        text="COVERAGE ACTIVE",
                        timeout_s=0.01,
                    )
                ],
            ),
            Step(id="next", intent="record it", action=ActionKind.KEY, key="B"),
        ],
    )
    bundle = tmp_path / "bundles" / "one"
    run = tmp_path / "runs" / "one"
    workflow.save(bundle)
    report = Replayer(
        FakeBackend(), vision=FakeVision(), durable=True, poll_interval_s=0.0
    ).run(
        workflow,
        bundle_dir=bundle,
        run_dir=run,
        params={"patient": PROTECTED_VALUE},
    )
    assert report.success is False
    item = attention_item(run.parent, run)
    assert item is not None
    return run, item


# ------------------------------------------------------------------ publish


def test_publishing_sends_the_projection_and_nothing_else(tmp_path):
    run, item = _halted_run(tmp_path)
    relay, transport = _relay(
        tasks=(200, {"accepted": True, "created": True, "task_id": "task_x"})
    )
    outcome = relay.publish(run, item)

    assert outcome.state is PublishState.PUBLISHED
    assert outcome.is_certain is True
    assert outcome.delivery_tier == "remote_closed_context"
    assert outcome.carried_context is True

    (path, payload) = transport.calls[0]
    assert path == "/api/human-decisions/tasks"
    body = json.dumps(payload)
    # The run's protected parameter, its workflow name, and the step intent are
    # all absent from what left the machine.
    assert PROTECTED_VALUE not in body
    assert "relay-e2e" not in body
    assert "COVERAGE ACTIVE" not in body
    assert (
        payload["halt_context"]["schema_version"] == "openadapt.remote-halt-context/v1"
    )


def test_republishing_an_existing_task_is_idempotent_not_a_second_task(tmp_path):
    run, item = _halted_run(tmp_path)
    relay, _ = _relay(
        tasks=(200, {"accepted": True, "created": False, "task_id": "task_x"})
    )
    assert relay.publish(run, item).state is PublishState.ALREADY_PUBLISHED


def test_a_transport_failure_is_unknown_and_is_not_retried(tmp_path):
    """The exact rule the engine applies to a dispatched action, applied here."""
    run, item = _halted_run(tmp_path)
    relay, transport = _relay(tasks=RelayUncertain("connection reset"))
    outcome = relay.publish(run, item)
    assert outcome.state is PublishState.UNKNOWN
    assert outcome.is_certain is False
    # One attempt. The module does not decide for the operator that it failed.
    assert len(transport.calls) == 1


def test_a_server_error_is_unknown_rather_than_failed(tmp_path):
    run, item = _halted_run(tmp_path)
    relay, _ = _relay(tasks=(503, {}))
    assert relay.publish(run, item).state is PublishState.UNKNOWN


def test_a_refused_projection_does_not_silently_degrade(tmp_path):
    """A control plane that rejects the contract must not look like success."""
    run, item = _halted_run(tmp_path)
    relay, _ = _relay(tasks=(400, {"error": "remote decision projection is invalid"}))
    with pytest.raises(RelayRefused, match="local console remains"):
        relay.publish(run, item)


def test_publishing_at_the_identifier_tier_reports_that_it_carried_no_context(
    tmp_path,
):
    run, item = _halted_run(tmp_path)
    relay, transport = _relay(
        deployment=_deployment(context_tier="remote_identifiers"),
        tasks=(200, {"accepted": True, "created": True, "task_id": "t"}),
    )
    outcome = relay.publish(run, item)
    assert outcome.delivery_tier == "remote_identifiers"
    assert outcome.carried_context is False
    assert transport.calls[0][1]["halt_context"] is None


# --------------------------------------------------------------------- poll


def _bound_relay_payload(run: Path, item, deployment) -> dict[str, Any]:
    from openadapt_flow.console.human_decisions import portable_remote_decision_task

    projection = portable_remote_decision_task(run, item, deployment=deployment)
    capability = AttendedActionStore(run).read()
    return {
        "schema_version": RELAY_SCHEMA,
        "decision_id": "hdd_11111111-2222-3333-4444-555555555555",
        "tenant_id": "tenant_exact_01",
        "runner_id": "runner_exact_01",
        "task_id": projection.task.task_id,
        "task_revision": projection.task.task_revision,
        "task_digest": projection.task_digest,
        "task_signature": projection.task.signature,
        "capability_digest": capability.digest,
        "phase": "paused",
        "event_sequence": projection.event_sequence,
        "idempotency_scope_digest": projection.idempotency_scope_digest,
        "binding_digest": projection.binding_digest,
        "decision_action": "verify_and_resume",
        "action": "continue",
        "idempotency_key": "relay-idempotency-key-0000001",
        "actor_id": "operator_subject_01",
        "assurance": "aal2",
        "submitted_at": "2026-07-27T12:00:00Z",
        "expires_at": projection.task.expires_at,
        "execution_authority": "customer_runtime",
        "local_revalidation_required": True,
        "signature_algorithm": "hmac-sha256",
    }


def test_no_waiting_decision_is_not_an_error(tmp_path):
    relay, _ = _relay(poll=(204, {}))
    assert relay.poll(wait_s=0.0) is None


@pytest.mark.parametrize(
    "mutation",
    [
        {"assurance": "aal1"},
        {"execution_authority": "control_plane"},
        {"local_revalidation_required": False},
        {"phase": "running"},
        {"action": "delete_everything"},
        {"tenant_id": "tenant_someone_else"},
        {"runner_id": "runner_someone_else"},
    ],
)
def test_a_relay_that_claims_more_than_a_decision_is_refused(tmp_path, mutation):
    """A relay may carry intent. It may never carry authority."""
    run, item = _halted_run(tmp_path)
    payload = {**_bound_relay_payload(run, item, _deployment()), **mutation}
    relay, _ = _relay(poll=(200, {"decision": _sign(payload)}))
    with pytest.raises(RelayRefused):
        relay.poll(wait_s=0.0)


def test_an_unsigned_or_forged_relay_is_refused(tmp_path):
    run, item = _halted_run(tmp_path)
    payload = _bound_relay_payload(run, item, _deployment())
    forged = _sign(payload, token="oar_" + "Y" * 40)
    relay, _ = _relay(poll=(200, {"decision": forged}))
    with pytest.raises(RelayRefused, match="not signed by this runner"):
        relay.poll(wait_s=0.0)


def test_an_uncertain_poll_is_simply_no_decision_this_cycle():
    """A poll has no side effect, so uncertainty about it is not uncertainty."""
    relay, _ = _relay(poll=RelayUncertain("timeout"))
    assert relay.poll(wait_s=0.0) is None


# ------------------------------------------------------------------ execute


def test_a_relayed_decision_runs_the_normal_governed_path(tmp_path):
    run, item = _halted_run(tmp_path)
    deployment = _deployment()
    payload = _bound_relay_payload(run, item, deployment)
    relay, transport = _relay(
        deployment=deployment,
        poll=(200, {"decision": _sign(payload)}),
        ack=(200, {"accepted": True}),
    )
    executor = _ResultExecutor()
    outcome = relay.serve_once(run, item, wait_s=0.0, executor=executor)

    assert outcome is not None
    assert outcome.status == "completed"
    # The engine executed it, not the relay.
    assert executor.calls == 1
    ack_path, ack_body = transport.calls[-1]
    assert ack_path.endswith("/ack")
    assert ack_body["result"] == "accepted"
    assert ack_body["relay_digest"].startswith("sha256:")


@pytest.mark.parametrize(
    "action,disposition",
    [
        ("continue", "completed_by_operator"),
        ("skip", "not_applicable"),
        ("reject", "rejected_by_operator"),
        ("teach", "teach_requested"),
        ("escalate", "cannot_complete"),
    ],
)
def test_every_action_a_phone_can_give_carries_the_disposition_the_engine_wants(
    action, disposition
):
    """A single hardcoded disposition would silently reduce this lane to Continue.

    ``execute_attended_action`` refuses a mismatched (action, disposition) pair
    by design. So an answer of Skip, Reject, Teach or Escalate taken on a phone
    would have been refused AFTER the operator gave it, with a message about a
    disposition they never chose.
    """
    from openadapt_flow.console.decision_relay import _ENGINE_DISPOSITION

    assert _ENGINE_DISPOSITION[action] == disposition


def test_the_relayed_vocabulary_is_the_engine_vocabulary():
    """A phone must not be able to offer an action the relay cannot deliver."""
    from openadapt_flow.console.decision_relay import _ENGINE_ACTIONS
    from openadapt_flow.runtime.durable.attended import AttendedActionRequest

    engine = set(AttendedActionRequest.model_fields["action"].annotation.__args__)
    assert set(_ENGINE_ACTIONS) == engine


def test_a_relayed_reject_reaches_the_engine_as_a_reject(tmp_path):
    """`reject` terminates the run; `escalate` parks it. Not interchangeable."""
    run, item = _halted_run(tmp_path)
    deployment = _deployment()
    payload = _bound_relay_payload(run, item, deployment)
    payload["action"] = "reject"
    payload["decision_action"] = "reject"
    relay, transport = _relay(
        deployment=deployment,
        poll=(200, {"decision": _sign(payload)}),
        ack=(200, {"accepted": True}),
    )
    outcome = relay.serve_once(run, item, wait_s=0.0, executor=_ResultExecutor())

    assert outcome is not None
    assert outcome.action == "reject"
    assert transport.calls[-1][1]["result"] == "accepted"


def test_a_governed_refusal_is_acknowledged_rather_than_swallowed(tmp_path):
    """The operator must learn their answer was refused, not see silence."""
    run, item = _halted_run(tmp_path)
    deployment = _deployment()
    payload = _bound_relay_payload(run, item, deployment)
    payload["capability_digest"] = "sha256:" + "9" * 64
    relay, transport = _relay(
        deployment=deployment,
        poll=(200, {"decision": _sign(payload)}),
        ack=(200, {"accepted": True}),
    )
    with pytest.raises(AttendedActionRefused):
        relay.serve_once(run, item, wait_s=0.0, executor=_ResultExecutor())
    assert transport.calls[-1][1]["result"] == "refused"


def test_an_unacknowledged_decision_is_reported_as_unconfirmed_not_done(tmp_path):
    """A lost acknowledgement leaves the decision leased for safe re-delivery."""
    run, item = _halted_run(tmp_path)
    deployment = _deployment()
    payload = _bound_relay_payload(run, item, deployment)
    relay, _ = _relay(deployment=deployment, ack=RelayUncertain("connection reset"))
    decision = RelayedDecision(
        decision_id=str(payload["decision_id"]), relay=_sign(payload)
    )
    assert relay.acknowledge(decision, "accepted") is False


def test_an_unknown_acknowledgement_result_is_refused(tmp_path):
    relay, _ = _relay()
    decision = RelayedDecision(decision_id="hdd_0000000000000001", relay=RELAY_FIXTURE)
    with pytest.raises(RelayRefused):
        relay.acknowledge(decision, "delivered")

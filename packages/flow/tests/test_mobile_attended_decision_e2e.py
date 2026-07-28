"""The mobile attended-decision loop, end to end, plus its refusal matrix.

This is the design's own acceptance test. It starts from a REAL durable halt
produced by the replayer -- not a hand-built pause fixture -- walks the exact
chain a phone walks:

    halt -> PendingEscalation + AttendedPauseCapability
         -> PHI-free signed task projection
         -> phone-facing console route
         -> signed operator decision
         -> AttendedActionService / execute_attended_action
         -> exact capability validated, single-flight lease acquired
         -> live application re-read
         -> verify and continue, or refuse
         -> closed terminal receipt

and then proves every refusal in the acceptance matrix at that same boundary.
The phone returns a DECISION, never an execution result: actuation authority
stays in the local engine, and the console adds no second resume path.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from openadapt_types import HumanDecisionReceiptV1

from openadapt_flow.console import data, human_decisions
from openadapt_flow.console.app import create_app
from openadapt_flow.console.attention import attention_item
from openadapt_flow.console.human_decisions import (
    RemoteAttendedActionRequest,
    RemoteDecisionPrincipal,
    RemoteDecisionProjection,
    decision_receipt,
    execute_remote_attended_action,
    portable_remote_decision_task,
)
from openadapt_flow.deployment import DeploymentConfig
from openadapt_flow.ir import (
    ActionKind,
    Postcondition,
    PostconditionKind,
    RunReport,
    Step,
    Workflow,
)
from openadapt_flow.runtime.durable.attended import (
    AttendedActionRefused,
    AttendedActionStore,
    BoundAttendedExecutor,
    execute_attended_action,
    issue_attended_capability,
)
from openadapt_flow.runtime.durable.checkpoint import CheckpointStore
from openadapt_flow.runtime.replayer import Replayer
from tests.test_replayer import FakeBackend, FakeVision, Match

# Protected values that exist in the run but must never reach the phone.
WORKFLOW_NAME = "mobile-attended-e2e"
HUMAN_INTENT = "complete the payer sign-in challenge for the member record"
EXPECTED_TEXT = "COVERAGE ACTIVE"


def _workflow() -> Workflow:
    return Workflow(
        name=WORKFLOW_NAME,
        steps=[
            Step(
                id="human",
                intent=HUMAN_INTENT,
                action=ActionKind.KEY,
                key="A",
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT,
                        text=EXPECTED_TEXT,
                        timeout_s=0.01,
                    )
                ],
            ),
            Step(
                id="next",
                intent="record the confirmed coverage",
                action=ActionKind.KEY,
                key="B",
            ),
        ],
    )


def _halt(tmp_path: Path, *, name: str = "one", workflow: Workflow | None = None):
    """Drive a real durable run until the engine halts for a human."""
    workflow = workflow or _workflow()
    bundle = tmp_path / "bundles" / name
    run = tmp_path / "runs" / name
    workflow.save(bundle)
    report = Replayer(
        FakeBackend(),
        vision=FakeVision(),
        durable=True,
        poll_interval_s=0.0,
    ).run(workflow, bundle_dir=bundle, run_dir=run)
    assert report.success is False
    capability = AttendedActionStore(run).read()
    return workflow, bundle.parent, run.parent, bundle, run, capability


def _completed_live_session():
    """A live application in which the person finished the human step."""
    backend = FakeBackend()
    vision = FakeVision()
    vision.text_results = {
        EXPECTED_TEXT: Match(point=(10, 10), region=(0, 0, 20, 20), confidence=1.0)
    }
    return backend, BoundAttendedExecutor(
        lambda _manifest: Replayer(backend, vision=vision, poll_interval_s=0.0)
    )


def _unchanged_live_session():
    """A live application that still does not show the expected state."""
    backend = FakeBackend()
    return backend, BoundAttendedExecutor(
        lambda _manifest: Replayer(backend, vision=FakeVision(), poll_interval_s=0.0)
    )


class _Service:
    """The shipped embedding seam: it only forwards to the engine."""

    def __init__(self, executor):
        self.executor = executor
        self.calls = 0

    def execute(self, run_dir, request, *, operator):
        self.calls += 1
        return execute_attended_action(
            run_dir, request, operator=operator, executor=self.executor
        )


def _phone(bundles_root, runs_root, monkeypatch, *, service=None, allow_actions=True):
    """A phone reaching the runner through the customer's approved ingress."""
    monkeypatch.setattr(
        "openadapt_flow.console.app._local_operator_identity", lambda: "front-desk"
    )
    app = create_app(
        bundles_root,
        runs_root,
        allow_actions=allow_actions,
        attend=True,
        attended_service=service,
    )
    client = TestClient(
        app,
        base_url="http://127.0.0.1",
        raise_server_exceptions=False,
        headers={
            "Authorization": f"Bearer {app.state.console_access_token}",
            "Origin": "http://127.0.0.1",
            "X-OpenAdapt-CSRF": app.state.console_csrf_token,
        },
    )
    return app, client


def _open_task(client, index: int = 0):
    items = client.get("/api/attention").json()
    item = items[index]
    detail = client.get(f"/api/attention/{item['id']}").json()
    return item, detail


def _decision(detail, *, action="continue", key="mobile-decision-key-01"):
    disposition = {
        "continue": "completed_by_operator",
        "skip": "not_applicable",
        "teach": "teach_requested",
        "escalate": "needs_assistance",
    }[action]
    return {
        "capability_digest": detail["task"]["capability_digest"],
        "task_digest": detail["task_digest"],
        "task_signature": detail["task"]["signature"],
        "idempotency_key": key,
        "action": action,
        "disposition": disposition,
    }


def _post(client, item, payload, action=None):
    action = action or payload["action"]
    return client.post(f"/api/attention/{item['id']}/actions/{action}", json=payload)


PROTECTED = (WORKFLOW_NAME, HUMAN_INTENT, EXPECTED_TEXT, "front-desk")


def _assert_phi_free(blob: str, *, extra: tuple[str, ...] = ()) -> None:
    for protected in PROTECTED + extra:
        assert protected not in blob, protected


# ---------------------------------------------------------------------------
# 1. The loop closes: halt -> mobile task -> operator -> verify -> receipt
# ---------------------------------------------------------------------------


def test_halt_to_mobile_task_to_verified_continue_returns_a_terminal_receipt(
    tmp_path, monkeypatch
):
    workflow, bundles, runs, bundle, run, capability = _halt(tmp_path)
    backend, executor = _completed_live_session()
    service = _Service(executor)
    _app, client = _phone(bundles, runs, monkeypatch, service=service)

    # -- the queue a phone opens ------------------------------------------
    items = client.get("/api/attention").json()
    assert len(items) == 1
    item = items[0]
    # The engine's typed halt category survives the projection: the mobile
    # question is derived from the durable pause, not from a generic label.
    assert item["category"] == "postcondition"
    assert item["capability"]["digest"] == capability.digest
    _assert_phi_free(json.dumps(items), extra=(str(run), str(bundle)))

    # -- the task the phone renders ---------------------------------------
    detail = client.get(f"/api/attention/{item['id']}").json()
    task = detail["task"]
    assert task["schema_version"] == "openadapt.human-decision-task/v1"
    assert task["capability_digest"] == capability.digest
    assert task["required_authn"] == "local_session"
    assert task["allowed_actions"] == ["verify_and_resume", "teach", "escalate"]
    assert AttendedActionStore(run).verify_human_decision_task(task)
    _assert_phi_free(json.dumps(detail), extra=(str(run), str(bundle)))

    # -- the operator completes the fixture, then decides ------------------
    response = _post(client, item, _decision(detail))
    assert response.status_code == 200
    receipt = HumanDecisionReceiptV1.model_validate(response.json())
    assert receipt.state == "completed"
    assert receipt.reason_code == "verified_and_resumed"
    assert receipt.action == "verify_and_resume"
    assert receipt.report_success is True
    assert receipt.capability_digest == capability.digest
    _assert_phi_free(response.text)

    # -- the engine, not the phone, produced that outcome -----------------
    assert service.calls == 1
    store = CheckpointStore(run)
    assert [c.step_id for c in store.checkpoints()] == ["human", "next"]
    assert store.checkpoints()[0].actuation == "human_attended"
    assert store.read_pending() is None
    # The human step was verified, never re-actuated; only the next step ran.
    assert ("press", "A") not in backend.actions
    assert ("press", "B") in backend.actions
    # The loop closed: the durable pause is gone, so the run is no longer an
    # answerable mobile task even though it stays visible for local review.
    remaining = client.get("/api/attention").json()
    assert [entry["durably_paused"] for entry in remaining] == [False]
    closed = client.get(f"/api/attention/{item['id']}").json()
    assert closed["task"] is None and closed["task_digest"] is None

    # A terminal receipt reports that the CONTINUATION completed. It is not a
    # certification: this workflow declares no independent effect contract, so
    # the run's outcome stays honestly short of VERIFIED.
    report, _ = data._load_report(run)
    assert report.success is True
    assert report.execution_outcome == "COMPLETED_UNVERIFIED"


# ---------------------------------------------------------------------------
# 2. Refusal matrix
# ---------------------------------------------------------------------------


def test_expired_decision_refuses_before_actuation(tmp_path, monkeypatch):
    _wf, bundles, runs, _bundle, run, capability = _halt(tmp_path)
    backend, executor = _completed_live_session()
    service = _Service(executor)
    _app, client = _phone(bundles, runs, monkeypatch, service=service)
    item, detail = _open_task(client)

    expired_at = datetime.fromisoformat(capability.expires_at) + timedelta(hours=1)
    monkeypatch.setattr(
        "openadapt_flow.runtime.durable.attended._now", lambda: expired_at
    )
    response = _post(client, item, _decision(detail))
    assert response.status_code == 409
    assert "expired" in response.json()["detail"]
    assert not backend.actions
    assert CheckpointStore(run).read_pending() is not None


def test_forged_task_signature_or_capability_refuses_before_actuation(
    tmp_path, monkeypatch
):
    _wf, bundles, runs, _bundle, run, _capability = _halt(tmp_path)
    backend, executor = _completed_live_session()
    _app, client = _phone(bundles, runs, monkeypatch, service=_Service(executor))
    item, detail = _open_task(client)

    forgeries = (
        {"task_signature": "hmac-sha256:" + "0" * 64},
        {"task_digest": "sha256:" + "0" * 64},
        {"capability_digest": "sha256:" + "0" * 64},
    )
    for index, forged in enumerate(forgeries):
        payload = {
            **_decision(detail, key=f"forged-decision-key-{index:02d}"),
            **forged,
        }
        assert _post(client, item, payload).status_code == 409
    assert not backend.actions
    assert CheckpointStore(run).read_pending() is not None


def test_wrong_pause_and_wrong_bundle_refuse_before_actuation(tmp_path, monkeypatch):
    workflow, bundles, runs, _b_one, run_one, _cap_one = _halt(tmp_path, name="one")
    _wf_two, _b2, _r2, bundle_two, run_two, _cap_two = _halt(tmp_path, name="two")
    backend, executor = _completed_live_session()
    _app, client = _phone(bundles, runs, monkeypatch, service=_Service(executor))

    items = {item["id"]: item for item in client.get("/api/attention").json()}
    assert len(items) == 2
    by_run = {}
    for identifier in items:
        detail = client.get(f"/api/attention/{identifier}").json()
        by_run[detail["task"]["pause_id"]] = (items[identifier], detail)
    one = AttendedActionStore(run_one).read().pause_id
    two = AttendedActionStore(run_two).read().pause_id

    # A task signed for one pause cannot decide a different pause.
    item_two, _detail_two = by_run[two]
    _item_one, detail_one = by_run[one]
    crossed = _post(client, item_two, _decision(detail_one, key="crossed-pause-key-1"))
    assert crossed.status_code == 409

    # A bundle revised after issuance invalidates the exact binding.
    item_two, detail_two = by_run[two]
    workflow.steps.append(
        Step(id="added", intent="added", action=ActionKind.KEY, key="C")
    )
    workflow.save(bundle_two)
    revised = _post(client, item_two, _decision(detail_two, key="wrong-bundle-key-01"))
    assert revised.status_code == 409

    assert not backend.actions
    assert CheckpointStore(run_one).read_pending() is not None
    assert CheckpointStore(run_two).read_pending() is not None


@pytest.mark.parametrize(
    "drift",
    [
        {"tenant_id": "tenant_other_001"},
        {"runner_id": "runner_other_001"},
        {"event_sequence": 2},
        {"binding_digest": "sha256:" + "0" * 64},
        {"idempotency_scope_digest": "sha256:" + "0" * 64},
        {"task_signature": "hmac-sha256:" + "0" * 64},
    ],
)
def test_remote_decision_refuses_wrong_tenant_runner_or_event_sequence(tmp_path, drift):
    _wf, _bundles, runs_root, _bundle, run, capability = _halt(tmp_path)
    item = attention_item(runs_root, run)
    assert item is not None
    deployment = DeploymentConfig.model_validate(
        {
            "human_decisions": {
                "remote": {
                    "enabled": True,
                    "tenant_id": "tenant_exact_001",
                    "runner_id": "runner_exact_001",
                }
            }
        }
    )
    projection = portable_remote_decision_task(run, item, deployment=deployment)
    request = RemoteAttendedActionRequest(
        capability_digest=capability.digest,
        idempotency_key="remote-decision-key-0001",
        action="continue",
        disposition="completed_by_operator",
        task_digest=projection.task_digest,
        task_signature=projection.task.signature,
        tenant_id="tenant_exact_001",
        runner_id="runner_exact_001",
        phase=projection.phase,
        event_sequence=projection.event_sequence,
        idempotency_scope_digest=projection.idempotency_scope_digest,
        binding_digest=projection.binding_digest,
    )
    principal = RemoteDecisionPrincipal(
        subject="remote_operator_001",
        tenant_id="tenant_exact_001",
        runner_id="runner_exact_001",
    )
    backend, executor = _completed_live_session()
    with pytest.raises(AttendedActionRefused):
        execute_remote_attended_action(
            run,
            item,
            request.model_copy(update=drift),
            deployment=deployment,
            principal=principal,
            executor=executor,
        )
    assert not backend.actions
    assert CheckpointStore(run).read_pending() is not None


def test_action_outside_allowed_actions_refuses(tmp_path, monkeypatch):
    _wf, bundles, runs, _bundle, run, capability = _halt(tmp_path)
    backend, executor = _completed_live_session()
    _app, client = _phone(bundles, runs, monkeypatch, service=_Service(executor))
    item, detail = _open_task(client)
    assert "skip" not in detail["task"]["allowed_actions"]

    refused = _post(
        client, item, _decision(detail, action="skip", key="skip-not-allowed-key-01")
    )
    assert refused.status_code == 409
    assert "not allowed" in refused.json()["detail"]
    assert not backend.actions
    assert CheckpointStore(run).read_pending() is not None


def test_portable_and_engine_action_vocabularies_translate_without_a_gap():
    """The two action vocabularies must be a strict bijection.

    The task advertises portable names (``verify_and_resume``); the request
    carries engine names (``continue``). A mapping between two vocabularies is
    exactly where an "outside allowed_actions" check can pass while admitting
    something the task never authorized, so pin both directions.
    """
    engine_actions = {"continue", "skip", "teach", "escalate"}
    portable_actions = {"verify_and_resume", "skip", "teach", "escalate"}
    assert set(human_decisions._ACTION_MAP) == engine_actions
    assert set(human_decisions._ACTION_MAP.values()) == portable_actions
    # Injective: no two engine actions may collapse onto one portable name.
    assert len(set(human_decisions._ACTION_MAP.values())) == len(engine_actions)
    # Every receipt state mapping is total over the engine decision statuses.
    from openadapt_flow.runtime.durable.attended import AttendedDecision

    statuses = set(AttendedDecision.model_fields["status"].annotation.__args__)
    assert statuses == set(human_decisions._RECEIPT_STATE)


def test_same_key_replays_the_receipt_and_a_different_payload_refuses(
    tmp_path, monkeypatch
):
    _wf, bundles, runs, _bundle, run, _capability = _halt(tmp_path)
    # The live application still does not show the expected state, so the
    # decision is refused and the pause survives for the replay assertions.
    backend, executor = _unchanged_live_session()
    service = _Service(executor)
    _app, client = _phone(bundles, runs, monkeypatch, service=service)
    item, detail = _open_task(client)

    payload = _decision(detail, key="replayed-decision-key-01")
    first = _post(client, item, payload)
    assert first.status_code == 200
    assert first.json()["state"] == "refused"
    assert first.json()["reason_code"] == "revalidation_refused"

    second = _post(client, item, payload)
    assert second.status_code == 200
    assert second.json() == first.json()

    conflicting = {
        **_decision(detail, action="escalate", key="replayed-decision-key-01"),
    }
    refused = _post(client, item, conflicting)
    assert refused.status_code == 409
    assert "different request" in refused.json()["detail"]
    assert not backend.actions


def test_two_operators_cannot_both_decide_the_same_task(tmp_path, monkeypatch):
    _wf, bundles, runs, _bundle, run, _capability = _halt(tmp_path)
    second_result: dict[str, object] = {}
    holder: dict[str, object] = {}

    class _ConcurrentExecutor:
        """Runs while the first decision holds the single-flight lease."""

        def continue_run(self, run_dir, capability, approval):
            from openadapt_flow.runtime.durable.attended import (
                AttendedExecutionResult,
            )

            client = holder["client"]
            item = holder["item"]
            detail = holder["detail"]
            other = _post(client, item, _decision(detail, key="second-operator-key-01"))
            second_result["status"] = other.status_code
            second_result["detail"] = other.json().get("detail")
            return AttendedExecutionResult(
                status="completed",
                message="verified",
                report_success=True,
                next_transition=capability.expected_next_transition,
            )

        def skip_run(self, run_dir, capability, approval):
            return self.continue_run(run_dir, capability, approval)

    service = _Service(_ConcurrentExecutor())
    _app, client = _phone(bundles, runs, monkeypatch, service=service)
    item, detail = _open_task(client)
    holder.update({"client": client, "item": item, "detail": detail})

    first = _post(client, item, _decision(detail, key="first-operator-key-01"))
    assert first.status_code == 200
    assert first.json()["state"] == "completed"
    assert second_result["status"] == 409
    assert "already in progress" in str(second_result["detail"])


def test_a_task_that_becomes_stale_while_open_refuses(tmp_path, monkeypatch):
    _wf, bundles, runs, _bundle, run, capability = _halt(tmp_path)
    backend, executor = _completed_live_session()
    _app, client = _phone(bundles, runs, monkeypatch, service=_Service(executor))
    item, detail = _open_task(client)

    # The run halts again on the same step before the operator answers: the
    # engine issues a new exact pause capability and the open page is stale.
    workflow = _workflow()
    store = CheckpointStore(run)
    pending = store.read_pending()
    assert pending is not None
    replaced = pending.model_copy(update={"created_at": "2026-07-27T23:59:00+00:00"})
    store.write_pending(replaced)
    reissued = issue_attended_capability(
        run,
        store=store,
        pending=replaced,
        workflow=workflow,
        result=data._load_report(run)[0].results[-1],
    )
    assert reissued.digest != capability.digest

    stale = _post(client, item, _decision(detail, key="stale-task-key-0001"))
    assert stale.status_code == 409
    assert not backend.actions
    assert CheckpointStore(run).read_pending() is not None


def test_live_state_changing_after_the_displayed_frame_refuses(tmp_path, monkeypatch):
    _wf, bundles, runs, _bundle, run, _capability = _halt(tmp_path)
    backend, executor = _unchanged_live_session()
    _app, client = _phone(bundles, runs, monkeypatch, service=_Service(executor))
    item, detail = _open_task(client)
    # The phone displayed a retained frame; the runner re-reads the LIVE
    # application and finds it does not satisfy the step's contract.
    assert detail["presentation"]["before_artifact_id"] is not None

    response = _post(client, item, _decision(detail, key="stale-frame-key-0001"))
    assert response.status_code == 200
    receipt = HumanDecisionReceiptV1.model_validate(response.json())
    assert receipt.state == "refused"
    assert receipt.report_success is False
    assert not backend.actions
    assert CheckpointStore(run).checkpoints() == []
    assert CheckpointStore(run).read_pending() is not None


def test_uncertain_delivery_is_reported_as_uncertain_and_survives_a_fresh_key(
    tmp_path, monkeypatch
):
    _wf, bundles, runs, _bundle, run, _capability = _halt(tmp_path)

    class _LosesTheAnswer:
        """Delivery may have crossed the boundary; no terminal receipt came."""

        def continue_run(self, run_dir, capability, approval):
            raise RuntimeError("the live session died after the delivery boundary")

        def skip_run(self, run_dir, capability, approval):
            return self.continue_run(run_dir, capability, approval)

    _app, client = _phone(
        bundles, runs, monkeypatch, service=_Service(_LosesTheAnswer())
    )
    item, detail = _open_task(client)
    assert detail["task"]["delivery_state"] == "unknown"

    response = _post(client, item, _decision(detail, key="uncertain-key-000001"))
    # Never a success, never an opaque 500, and never "refused" -- the phone
    # must be told the action may already have happened.
    assert response.status_code == 202
    receipt = HumanDecisionReceiptV1.model_validate(response.json())
    assert receipt.state == "delivery_uncertain"
    assert receipt.reason_code == "delivery_uncertain"
    assert receipt.report_success is None

    statuses = [
        entry["status"]
        for entry in json.loads((run / "attended_decisions.json").read_text())[
            "decisions"
        ]
    ]
    assert statuses == ["prepared", "delivery_started", "delivery_uncertain"]

    # A fresh idempotency key must not launder the uncertainty away.
    fresh = _post(client, item, _decision(detail, key="uncertain-key-000002"))
    assert fresh.status_code == 409
    assert "delivery" in fresh.json()["detail"]

    # Re-opening the task now reports "may have been sent" rather than
    # collapsing back into the pre-decision delivery state.
    reopened = client.get(f"/api/attention/{item['id']}").json()
    assert reopened["task"]["delivery_state"] == "unknown"
    assert reopened["task"]["task_kind"] == "delivery_uncertain"
    assert reopened["task"]["question"]["template"] == "review_uncertain_delivery"
    assert CheckpointStore(run).read_pending() is not None


def test_a_pause_that_had_not_been_delivered_becomes_uncertain_after_a_lost_answer(
    tmp_path,
):
    """The three delivery states must never collapse into one another."""
    from openadapt_flow.ir import StepResult

    workflow = _workflow()
    bundle = tmp_path / "bundles" / "nd"
    run = tmp_path / "runs" / "nd"
    workflow.save(bundle)
    store = CheckpointStore(run)
    from openadapt_flow.runtime.durable.checkpoint import (
        PendingEscalation,
        RunManifest,
    )

    store.write_manifest(
        RunManifest(
            run_id="run-not-delivered",
            workflow_name=workflow.name,
            bundle_dir=str(bundle),
            params={},
        )
    )
    result = StepResult(
        step_id="human",
        intent=HUMAN_INTENT,
        ok=False,
        error="could not resolve the intended target",
    )
    pending = PendingEscalation(
        workflow_name=workflow.name,
        step_index=0,
        step_id="human",
        intent=HUMAN_INTENT,
        category="resolution",
        reason="could not resolve the intended target",
        resume_from_index=0,
    )
    store.write_pending(pending)
    RunReport(
        workflow_name=workflow.name,
        started_at="2026-07-27T12:00:00+00:00",
        success=False,
        results=[result],
    ).save(run)
    capability = issue_attended_capability(
        run, store=store, pending=pending, workflow=workflow, result=result
    )
    assert capability.delivery_state == "not_delivered"

    item = attention_item(run.parent, run)
    assert item is not None
    before = human_decisions.decision_detail(run, item)
    assert before["task"]["delivery_state"] == "not_delivered"

    class _LosesTheAnswer:
        def continue_run(self, run_dir, capability, approval):
            raise RuntimeError("lost after the delivery boundary")

        def skip_run(self, run_dir, capability, approval):
            return self.continue_run(run_dir, capability, approval)

    from openadapt_flow.runtime.durable.attended import AttendedActionRequest

    with pytest.raises(RuntimeError):
        execute_attended_action(
            run,
            AttendedActionRequest(
                capability_digest=capability.digest,
                idempotency_key="not-delivered-key-01",
                action="continue",
                disposition="completed_by_operator",
            ),
            operator="front-desk",
            executor=_LosesTheAnswer(),
        )

    after = human_decisions.decision_detail(run, item)
    assert after["task"]["delivery_state"] == "unknown"
    assert after["task"]["task_kind"] == "delivery_uncertain"
    # The task identity changed, so an already-open phone page is now stale and
    # cannot answer the pre-uncertainty question.
    assert after["task_digest"] != before["task_digest"]


# ---------------------------------------------------------------------------
# 3. Boundary contracts: what may cross to the phone at all
# ---------------------------------------------------------------------------


def test_cloud_safe_schema_rejects_evidence_free_text_and_unknown_fields(
    tmp_path, monkeypatch
):
    from openadapt_types import HumanDecisionTaskV1

    _wf, bundles, runs, _bundle, run, _capability = _halt(tmp_path)
    _app, client = _phone(bundles, runs, monkeypatch)
    _item, detail = _open_task(client)
    task = detail["task"]
    validated = HumanDecisionTaskV1.model_validate(task)
    assert validated.verify_hmac((run / ".attended_capability.key").read_bytes())

    smuggled = (
        {"screenshot": "data:image/png;base64,AAAA"},
        {"screenshot_b64": "AAAA"},
        {"ocr_text": [EXPECTED_TEXT]},
        {"observed_value": EXPECTED_TEXT},
        {"expected_value": EXPECTED_TEXT},
        {"reason": HUMAN_INTENT},
        {"intent": HUMAN_INTENT},
        {"note": "the member said it was fine"},
        {"patient_mrn": "MRN-0001"},
        {"record_id": "MRN-0001"},
        {"workflow_name": WORKFLOW_NAME},
        {"run_dir": str(run)},
    )
    for field in smuggled:
        with pytest.raises(ValidationError):
            HumanDecisionTaskV1.model_validate({**task, **field})

    # Every closed envelope on the phone boundary forbids unknown fields.
    for model in (
        HumanDecisionReceiptV1,
        RemoteDecisionProjection,
        RemoteAttendedActionRequest,
        RemoteDecisionPrincipal,
    ):
        assert model.model_config["extra"] == "forbid"


def test_the_terminal_receipt_cannot_represent_protected_content(tmp_path):
    """Free text and operator identity are structurally absent, not stripped."""
    from openadapt_flow.runtime.durable.attended import AttendedDecision

    decision = AttendedDecision(
        pause_id="a" * 32,
        capability_digest="sha256:" + "b" * 64,
        request_digest="sha256:" + "c" * 64,
        idempotency_key="terminal-receipt-key-1",
        action="continue",
        operator="front-desk",
        status="completed",
        message=f"{HUMAN_INTENT} at {tmp_path}",
        report_success=True,
    )
    receipt = decision_receipt(decision)
    # Every exported field is an opaque id, a digest, a closed enum, or a
    # pattern-checked timestamp. There is no field a free string can ride in.
    assert set(HumanDecisionReceiptV1.model_fields) == {
        "schema_version",
        "task_id",
        "task_revision",
        "pause_id",
        "capability_digest",
        "request_digest",
        "decision_digest",
        "transition_receipt_digest",
        "action",
        "state",
        "reason_code",
        "report_success",
        "decided_at",
        "signature_algorithm",
        "signature",
    }
    body = receipt.model_dump_json()
    _assert_phi_free(body, extra=(str(tmp_path),))
    assert "message" not in body and "operator" not in body
    # The audit record itself is unchanged and still carries the full evidence.
    assert decision.operator == "front-desk"


def test_no_receipt_field_can_carry_free_text():
    """A timestamp-shaped field must not accept 40 characters of prose.

    This is the exact hole a length-only bound leaves open: a contract whose
    purpose is that protected content is unrepresentable is defeated if any
    field accepts arbitrary text, whatever it is named.
    """
    valid = {
        "task_id": "task_" + "a" * 32,
        "pause_id": "a" * 32,
        "capability_digest": "sha256:" + "b" * 64,
        "request_digest": "sha256:" + "c" * 64,
        "decision_digest": "sha256:" + "d" * 64,
        "action": "verify_and_resume",
        "state": "completed",
        "reason_code": "verified_and_resumed",
        "report_success": True,
        "decided_at": "2026-07-27T12:00:00+00:00",
    }
    assert HumanDecisionReceiptV1.model_validate(valid).decided_at.endswith("+00:00")
    for smuggled in (
        "Jane Roe MRN-0001 coverage",
        "2026-07-27T12:00:00+00:00 Jane",
        "                    ",
        HUMAN_INTENT[:40],
    ):
        with pytest.raises(ValidationError):
            HumanDecisionReceiptV1.model_validate({**valid, "decided_at": smuggled})
    # State and reason are not independent, and only a completed run succeeded.
    with pytest.raises(ValidationError):
        HumanDecisionReceiptV1.model_validate({**valid, "reason_code": "expired"})
    with pytest.raises(ValidationError):
        HumanDecisionReceiptV1.model_validate(
            {**valid, "state": "refused", "reason_code": "revalidation_refused"}
        )


def test_every_engine_terminal_state_projects_to_a_permitted_receipt_pair():
    """Flow's status map must stay inside the shared contract's pair table."""
    from openadapt_types import HUMAN_DECISION_RECEIPT_REASONS

    from openadapt_flow.runtime.durable.attended import AttendedDecision

    for status, (state, reason) in human_decisions._RECEIPT_STATE.items():
        assert reason in HUMAN_DECISION_RECEIPT_REASONS[state], status
        decision = AttendedDecision(
            pause_id="a" * 32,
            capability_digest="sha256:" + "b" * 64,
            request_digest="sha256:" + "c" * 64,
            idempotency_key="pair-table-key-0001",
            action="continue",
            operator="front-desk",
            status=status,
            message="engine text that must not be exported",
            report_success=True if state == "completed" else None,
        )
        receipt = decision_receipt(decision)
        assert receipt.state.value == state
        assert receipt.reason_code.value == reason
    # The skip cause is distinguished from the verify cause under one state.
    skipped = AttendedDecision(
        pause_id="a" * 32,
        capability_digest="sha256:" + "b" * 64,
        request_digest="sha256:" + "c" * 64,
        idempotency_key="pair-table-key-0002",
        action="skip",
        operator="front-desk",
        status="completed",
        message="skipped",
        report_success=True,
    )
    assert decision_receipt(skipped).reason_code.value == "skipped_and_resumed"


def test_remote_binding_digests_are_ascii_canonicalization_invariant(tmp_path):
    """Aligning canonicalization must not move the Cloud-verified digests.

    ``_sha256`` now uses the normative ``ensure_ascii=True``. The remote
    projection's binding and idempotency-scope digests are cross-language
    contract fields, so prove they are computed over pattern-constrained ASCII
    only and therefore cannot have shifted.
    """
    import json

    _wf, _bundles, runs_root, _bundle, run, _capability = _halt(tmp_path)
    item = attention_item(runs_root, run)
    assert item is not None
    deployment = DeploymentConfig.model_validate(
        {
            "human_decisions": {
                "remote": {
                    "enabled": True,
                    "tenant_id": "tenant_exact_001",
                    "runner_id": "runner_exact_001",
                }
            }
        }
    )
    projection = portable_remote_decision_task(run, item, deployment=deployment)
    exported = projection.model_dump(mode="json")
    serialized = json.dumps(exported, ensure_ascii=False)
    assert serialized.isascii(), "a non-ASCII value would make the digests diverge"
    assert projection.binding_digest.startswith("sha256:")
    assert projection.idempotency_scope_digest.startswith("sha256:")


def test_protected_evidence_is_authorization_scoped_no_store_and_bounded(
    tmp_path, monkeypatch
):
    _wf, bundles, runs, _bundle, run, _capability = _halt(tmp_path)
    app, client = _phone(bundles, runs, monkeypatch)
    item, detail = _open_task(client)
    artifact_id = detail["presentation"]["before_artifact_id"]
    assert artifact_id

    path = f"/api/runs/{item['id']}/artifact"
    unauthenticated = TestClient(app, base_url="http://127.0.0.1")
    assert unauthenticated.get(path, params={"id": artifact_id}).status_code == 401

    response = client.get(path, params={"id": artifact_id})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    # Nothing that would let an intermediary or the PWA revalidate and cache.
    assert "etag" not in response.headers
    assert "last-modified" not in response.headers
    assert response.headers["cross-origin-resource-policy"] == "same-origin"

    raw = next(run.rglob("*.png"))
    original = raw.read_bytes()
    monkeypatch.setattr(
        "openadapt_flow.console.app._MAX_PROTECTED_CROP_BYTES", len(original) - 1
    )
    bounded = client.get(path, params={"id": artifact_id})
    assert bounded.status_code == 413
    # The raw artifact on disk is untouched by any of this.
    assert raw.read_bytes() == original


def test_the_console_registers_no_service_worker_cache():
    static = Path(__file__).resolve().parent.parent / "openadapt_flow/console/static"
    for name in ("console.js", "index.html"):
        source = (static / name).read_text(encoding="utf-8")
        assert "serviceWorker" not in source
        assert "caches.open" not in source
    # The decision outcome the phone renders comes from the closed receipt's
    # reason code, never from server-supplied display text.
    assert "result.message" not in (static / "console.js").read_text(encoding="utf-8")


def test_no_notification_contains_protected_content(tmp_path, monkeypatch):
    _wf, bundles, runs, _bundle, run, _capability = _halt(tmp_path)
    _app, client = _phone(bundles, runs, monkeypatch)
    body = client.get("/api/attention/notification").json()
    assert set(body) == {"title", "body", "open_count", "route"}
    assert body["open_count"] == 1
    _assert_phi_free(json.dumps(body), extra=(str(run), str(bundles)))


def test_verified_is_impossible_without_the_complete_contract(tmp_path, monkeypatch):
    """A decision alone never produces a verified outcome."""
    _wf, bundles, runs, _bundle, run, _capability = _halt(tmp_path)
    backend, executor = _unchanged_live_session()
    _app, client = _phone(bundles, runs, monkeypatch, service=_Service(executor))
    item, detail = _open_task(client)

    for action, expected in (
        ("escalate", "escalated"),
        ("teach", "demonstration_requested"),
        ("continue", "refused"),
    ):
        response = _post(
            client, item, _decision(detail, action=action, key=f"contract-{action}-01")
        )
        assert response.status_code == 200, action
        receipt = HumanDecisionReceiptV1.model_validate(response.json())
        assert receipt.state == expected, action
        assert receipt.report_success is not True, action

    assert not backend.actions
    assert CheckpointStore(run).checkpoints() == []
    assert CheckpointStore(run).read_pending() is not None
    report, _ = data._load_report(run)
    assert report.execution_outcome != "VERIFIED"
    assert report.success is False


# ---------------------------------------------------------------------------
# 4. The exact wire contract a portal shell must implement
# ---------------------------------------------------------------------------


def _drive(tmp_path, monkeypatch, *, name, workflow, executor, action="continue"):
    """Drive one decision through the phone-facing route and return the reply."""
    _wf, bundles, runs, _bundle, run, _cap = _halt(
        tmp_path, name=name, workflow=workflow
    )
    _app, client = _phone(bundles, runs, monkeypatch, service=_Service(executor))
    item, detail = _open_task(client)
    response = _post(
        client, item, _decision(detail, action=action, key=f"contract-{name}-key-01")
    )
    return response


def test_console_route_emits_exactly_the_documented_receipt_contract(
    tmp_path, monkeypatch
):
    """Pin every (HTTP status, state, reason_code) a phone can observe.

    A portal shell renders these and nothing else. The set is asserted exactly
    so a future engine outcome cannot reach a phone that has no copy for it --
    the failure this contract exists to prevent is a real outcome being shown
    to an operator as some other outcome.
    """

    class _LosesTheAnswer:
        def continue_run(self, run_dir, capability, approval):
            raise RuntimeError("the live session died after the delivery boundary")

        def skip_run(self, run_dir, capability, approval):
            return self.continue_run(run_dir, capability, approval)

    halting = Workflow(
        name=WORKFLOW_NAME,
        steps=[
            _workflow().steps[0],
            Step(
                id="next",
                intent="record the confirmed coverage",
                action=ActionKind.KEY,
                key="B",
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT,
                        text="NEVER APPEARS",
                        timeout_s=0.01,
                    )
                ],
            ),
        ],
    )

    observed = set()

    # verified continuation
    ok = _drive(
        tmp_path,
        monkeypatch,
        name="ok",
        workflow=_workflow(),
        executor=_completed_live_session()[1],
    )
    observed.add((ok.status_code, ok.json()["state"], ok.json()["reason_code"]))

    # the continuation ran and then halted again -- NOT a refusal
    halted = _drive(
        tmp_path,
        monkeypatch,
        name="halted",
        workflow=halting,
        executor=_completed_live_session()[1],
    )
    observed.add(
        (halted.status_code, halted.json()["state"], halted.json()["reason_code"])
    )

    # live state did not match: nothing was actuated
    refused = _drive(
        tmp_path,
        monkeypatch,
        name="refused",
        workflow=_workflow(),
        executor=_unchanged_live_session()[1],
    )
    observed.add(
        (refused.status_code, refused.json()["state"], refused.json()["reason_code"])
    )

    for action, name in (("teach", "teach"), ("escalate", "escalate")):
        reply = _drive(
            tmp_path,
            monkeypatch,
            name=name,
            workflow=_workflow(),
            executor=_completed_live_session()[1],
            action=action,
        )
        observed.add(
            (reply.status_code, reply.json()["state"], reply.json()["reason_code"])
        )

    # no terminal receipt came back: the action may already have landed
    uncertain = _drive(
        tmp_path,
        monkeypatch,
        name="uncertain",
        workflow=_workflow(),
        executor=_LosesTheAnswer(),
    )
    observed.add(
        (
            uncertain.status_code,
            uncertain.json()["state"],
            uncertain.json()["reason_code"],
        )
    )

    assert observed == {
        (200, "completed", "verified_and_resumed"),
        (200, "halted", "continuation_halted"),
        (200, "refused", "revalidation_refused"),
        (200, "demonstration_requested", "demonstration_requested"),
        (200, "escalated", "escalation_recorded"),
        (202, "delivery_uncertain", "delivery_uncertain"),
    }
    # A receipt never carries display text; the consumer owns the wording.
    for reply in (ok, halted, refused, uncertain):
        assert "message" not in reply.json()
        assert "status" not in reply.json()


def test_a_pre_admission_refusal_is_not_a_receipt(tmp_path, monkeypatch):
    """Refusals before admission keep the shipped ``{"detail": ...}`` shape.

    A shell must branch on the response shape, not assume every reply is a
    receipt: a stale or unauthorized decision never reaches the journal, so
    there is no terminal state to report.
    """
    _wf, bundles, runs, _bundle, run, _capability = _halt(tmp_path)
    backend, executor = _completed_live_session()
    _app, client = _phone(bundles, runs, monkeypatch, service=_Service(executor))
    item, detail = _open_task(client)

    stale = _post(
        client,
        item,
        {
            **_decision(detail, key="pre-admission-key-0001"),
            "task_signature": "hmac-sha256:" + "0" * 64,
        },
    )
    assert stale.status_code == 409
    assert set(stale.json()) == {"detail"}
    assert isinstance(stale.json()["detail"], str)
    assert "state" not in stale.json() and "reason_code" not in stale.json()
    assert not backend.actions

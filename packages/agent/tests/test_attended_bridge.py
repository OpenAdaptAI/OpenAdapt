"""Needs Attention and attended decisions stay on Flow's exact engine contract."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path

import pytest

from openadapt_agent.attended import AttendedBridgeError
from openadapt_agent.bridge import AgentBridge, BridgeError
from openadapt_agent.flow_service import open_attended_service
from openadapt_agent.runner import RunnerConfig


@pytest.fixture()
def paused_attention(tmp_path, monkeypatch):
    from openadapt_flow.ir import (
        ActionKind,
        HaltObservation,
        Postcondition,
        PostconditionKind,
        RunReport,
        Step,
        StepResult,
        Workflow,
    )
    from openadapt_flow.runtime.durable import (
        CheckpointStore,
        PendingEscalation,
        RunManifest,
        issue_attended_capability,
    )
    from openadapt_flow.runtime.durable.approval import approval_pause_digest
    from openadapt_flow.runtime.durable.authority import DurableAuthority

    workflow = Workflow(
        name="Attended reference",
        steps=[
            Step(
                id="human",
                intent="complete the local challenge",
                action=ActionKind.KEY,
                key="Enter",
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT,
                        text="DONE",
                    )
                ],
            ),
            Step(
                id="next",
                intent="continue deterministically",
                action=ActionKind.KEY,
                key="Tab",
            ),
        ],
    )
    bundle = tmp_path / "bundles" / "attended"
    workflow.save(bundle)
    runs = tmp_path / "runs"
    run = runs / "run-one"
    monkeypatch.setenv(
        "OPENADAPT_DURABLE_AUTHORITY_DB",
        str(tmp_path / "durable-authority" / "authority.sqlite3"),
    )
    store = CheckpointStore(run)
    manifest = RunManifest(
        run_id="run-instance-a",
        namespace_id="namespace-instance-a",
        canonical_run_dir=str(run.resolve()),
        workflow_name=workflow.name,
        bundle_dir=str(bundle),
        params={},
    )
    store.write_fresh_manifest(manifest)
    authority = DurableAuthority(run, store)
    authority_digest = authority.validate(manifest).progress_digest
    pending = PendingEscalation(
        run_id=manifest.run_id,
        workflow_name=workflow.name,
        step_index=0,
        step_id="human",
        intent="complete the local challenge",
        category="human_required",
        reason="local human presence required",
        proposed_options=["complete locally", "resume"],
        resume_from_index=0,
    )
    store.write_pending(pending)
    failed = StepResult(
        step_id="human",
        intent="complete the local challenge",
        ok=False,
        error="local human presence required",
    )
    RunReport(
        workflow_name=workflow.name,
        started_at="2026-07-19T00:00:00+00:00",
        success=False,
        results=[failed],
        halt=HaltObservation(
            state_id="human",
            intent="complete the local challenge",
            reason="local human presence required",
        ),
    ).save(run)
    capability = issue_attended_capability(
        run,
        store=store,
        pending=pending,
        workflow=workflow,
        result=failed,
    )
    authority.advance(
        manifest,
        expected_progress_digest=authority_digest,
        phase="paused",
        pause_binding_sha256=approval_pause_digest(pending),
    )
    return {
        "bundle": bundle,
        "bundles": bundle.parent,
        "runs": runs,
        "run": run,
        "store": store,
        "capability": capability,
    }


class ResultExecutor:
    def __init__(self):
        self.calls = 0
        self.actions = []

        from openadapt_flow.runtime.durable import BoundAttendedExecutor
        from openadapt_flow.runtime.replayer import Replayer

        actions = self.actions

        class Backend:
            viewport = (300, 200)

            def __init__(self):
                from PIL import Image

                buffer = io.BytesIO()
                Image.new("RGB", self.viewport, (240, 240, 240)).save(buffer, format="PNG")
                self.frame = buffer.getvalue()
                self.guarded_keyboard_point = None

            def screenshot(self):
                return self.frame

            def press(self, key):
                actions.append(("press", key))

            def guarded_keyboard_frame(self):
                return self.frame

            def arm_guarded_keyboard(self, x, y):
                self.guarded_keyboard_point = (int(x), int(y))

            def cancel_guarded_keyboard(self):
                self.guarded_keyboard_point = None

            def press_guarded(self, key, *, expected_frame_sha256):
                from openadapt_flow.ir import ActionDeliveryReceipt

                assert self.guarded_keyboard_point is not None
                self.guarded_keyboard_point = None
                assert hashlib.sha256(self.frame).hexdigest() == expected_frame_sha256
                actions.append(("press", key))
                return ActionDeliveryReceipt(
                    receipt_id=f"agent-test-{len(actions)}",
                    operation="physical_press",
                    native=False,
                    delivered_at="2026-07-25T00:00:00+00:00",
                )

        class Vision:
            @staticmethod
            def text_present(_screen_png, text, *, region=None, min_ratio=0.8):
                del region, min_ratio
                return text == "DONE"

            @staticmethod
            def wait_settled(backend, **_kwargs):
                return backend.screenshot()

            @staticmethod
            def phash_png(_png, region=None):
                del region
                return "aa"

            @staticmethod
            def phash_distance(_left, _right):
                return 0

        self.bound = BoundAttendedExecutor(
            lambda _manifest: Replayer(
                Backend(),
                vision=Vision(),
                poll_interval_s=0.0,
            )
        )

    def continue_run(self, run_dir, capability, approval):
        self.calls += 1
        return self.bound.continue_run(run_dir, capability, approval)

    def skip_run(self, run_dir, capability, approval):
        self.calls += 1
        return self.bound.skip_run(run_dir, capability, approval)


class FailingExecutor(ResultExecutor):
    def continue_run(self, _run_dir, _capability, _approval):
        self.calls += 1
        raise RuntimeError("backend transport failed after delivery began")


class DirectFlowService:
    """Test double that still executes Flow's exact public action function."""

    def __init__(self, executor):
        self.executor = executor
        self.deciders = []

    def execute(self, run_dir, request, *, operator, decided_by="unknown"):
        from openadapt_flow.runtime.durable import execute_attended_action

        # Recorded rather than defaulted: a fake that silently accepted
        # `unknown` would hide a bridge that stopped declaring itself.
        self.deciders.append(decided_by)
        return execute_attended_action(
            run_dir,
            request,
            operator=operator,
            decided_by=decided_by,
            executor=self.executor,
        )


def make_bridge(paused, *, allow_actions=False, service=None):
    return AgentBridge(
        paused["bundles"],
        RunnerConfig(
            flow_cli=("openadapt-flow-stub",),
            runs_dir=paused["runs"],
        ),
        allow_attended_actions=allow_actions,
        attended_service=service,
    )


def item_and_args(bridge, *, action="continue"):
    item = bridge.dispatch("list_needs_attention", {})["items"][0]
    confirmation = {
        "continue": "human_completed",
        "skip": "confirmed_not_applicable",
        "reject": "confirm_run_must_not_proceed",
        "teach": "request_demonstration",
        "escalate": "request_assistance",
    }[action]
    return item, {
        "attention_id": item["id"],
        "capability_digest": item["capability"]["digest"],
        "idempotency_key": f"stable-request-{action}-0001",
        confirmation: True,
    }


def test_needs_attention_is_phi_safe_and_always_readable(paused_attention):
    bridge = make_bridge(paused_attention)
    specs = {spec.name for spec in bridge.list_tool_specs()}
    assert {"list_needs_attention", "get_attention_item"} <= specs
    assert not (set(("continue_attention", "skip_attention")) & specs)

    listing = bridge.dispatch("list_needs_attention", {})
    assert listing["open_count"] == 1
    assert listing["actions_enabled"] is False
    serialized = json.dumps(listing)
    assert "Attended reference" not in serialized
    assert "local human presence required" not in serialized
    assert str(paused_attention["run"]) not in serialized

    item = listing["items"][0]
    assert item["human_required"] is True
    # Relayed verbatim from Flow's signed capability. `reject` appears because
    # the engine offers it at this pause, not because the bridge added it.
    assert item["capability"]["allowed_actions"] == [
        "continue",
        "reject",
        "teach",
        "escalate",
    ]
    assert bridge.dispatch("get_attention_item", {"attention_id": item["id"]}) == item
    for invalid in ("../run-one", "a/b", "", "0" * 23):
        with pytest.raises(BridgeError):
            bridge.dispatch("get_attention_item", {"attention_id": invalid})
    with pytest.raises(BridgeError):
        bridge.dispatch("get_attention_item", {"attention_id": 123})


def test_continue_is_exactly_bound_and_idempotent_without_reactuation(
    paused_attention,
):
    executor = ResultExecutor()
    bridge = make_bridge(
        paused_attention,
        allow_actions=True,
        service=DirectFlowService(executor),
    )
    specs = {spec.name for spec in bridge.list_tool_specs()}
    assert {
        "continue_attention",
        "skip_attention",
        "teach_attention",
        "escalate_attention",
    } <= specs
    continue_schema = next(
        spec.input_schema for spec in bridge.list_tool_specs() if spec.name == "continue_attention"
    )
    assert continue_schema["additionalProperties"] is False
    assert continue_schema["properties"]["human_completed"]["const"] is True
    assert "challenge_answer" not in continue_schema["properties"]
    item, arguments = item_and_args(bridge)

    first = bridge.dispatch("continue_attention", dict(arguments))
    second = bridge.dispatch("continue_attention", dict(arguments))
    assert first == second
    assert first["status"] == "completed"
    assert first["success"] is True
    assert executor.calls == 1
    assert executor.actions == [("press", "Tab")]
    decisions = json.loads((paused_attention["run"] / "attended_decisions.json").read_text())[
        "decisions"
    ]
    assert [decision["status"] for decision in decisions] == [
        "prepared",
        "delivery_started",
        "completed",
    ]


def test_stale_capability_extra_fields_and_false_confirmation_never_execute(
    paused_attention,
):
    executor = ResultExecutor()
    bridge = make_bridge(
        paused_attention,
        allow_actions=True,
        service=DirectFlowService(executor),
    )
    _item, arguments = item_and_args(bridge)

    wrong = dict(arguments, capability_digest="sha256:" + "0" * 64)
    with pytest.raises(BridgeError, match="changed"):
        bridge.dispatch("continue_attention", wrong)
    extra = dict(arguments, challenge_answer="never-crosses-this-boundary")
    with pytest.raises(BridgeError, match="exact tool schema"):
        bridge.dispatch("continue_attention", extra)
    unconfirmed = dict(arguments, human_completed=False)
    with pytest.raises(BridgeError, match="explicitly true"):
        bridge.dispatch("continue_attention", unconfirmed)

    pending = paused_attention["store"].read_pending()
    paused_attention["store"].write_pending(pending.model_copy(update={"step_id": "different"}))
    refused = bridge.dispatch("continue_attention", dict(arguments))
    assert refused["status"] == "refused"
    assert refused["success"] is False
    assert executor.calls == 0
    assert not (paused_attention["run"] / "approval.json").exists()


def test_flow_refusal_details_do_not_cross_the_phi_safe_bridge(paused_attention):
    class ProtectedRefusalService:
        def execute(self, _run_dir, _request, *, operator, decided_by="unknown"):
            assert operator
            assert decided_by == "automation"
            from openadapt_flow.runtime.durable import AttendedActionRefused

            raise AttendedActionRefused("protected patient name and local workflow details")

    bridge = make_bridge(
        paused_attention,
        allow_actions=True,
        service=ProtectedRefusalService(),
    )
    _item, arguments = item_and_args(bridge)

    result = bridge.dispatch("continue_attention", arguments)
    serialized = json.dumps(result)
    assert result["status"] == "refused"
    assert "protected patient" not in serialized
    assert "local workflow" not in serialized
    assert "signed pause" in result["message"]


def test_uncertain_delivery_is_audited_and_never_automatically_retried(
    paused_attention,
):
    executor = FailingExecutor()
    bridge = make_bridge(
        paused_attention,
        allow_actions=True,
        service=DirectFlowService(executor),
    )
    _item, arguments = item_and_args(bridge)

    with pytest.raises(BridgeError, match="reconcile live state"):
        bridge.dispatch("continue_attention", dict(arguments))
    retry = bridge.dispatch("continue_attention", dict(arguments))

    assert retry["status"] == "refused"
    assert retry["success"] is False
    assert executor.calls == 1
    decisions = json.loads((paused_attention["run"] / "attended_decisions.json").read_text())[
        "decisions"
    ]
    assert [decision["status"] for decision in decisions] == [
        "prepared",
        "delivery_started",
        "delivery_uncertain",
    ]


def test_teach_and_escalate_are_audited_without_a_live_executor(paused_attention):
    bridge = make_bridge(paused_attention, allow_actions=True, service=None)
    specs = {spec.name for spec in bridge.list_tool_specs()}
    assert {"teach_attention", "escalate_attention"} <= specs
    assert "continue_attention" not in specs

    _item, teach_args = item_and_args(bridge, action="teach")
    teach = bridge.dispatch("teach_attention", teach_args)
    assert teach["status"] == "needs_demonstration"
    assert teach["success"] is False

    _item, escalate_args = item_and_args(bridge, action="escalate")
    escalated = bridge.dispatch("escalate_attention", escalate_args)
    assert escalated["status"] == "escalated"
    assert escalated["success"] is False
    assert paused_attention["store"].read_pending() is not None

    with pytest.raises(BridgeError, match="deployment-bound"):
        bridge.dispatch(
            "continue_attention",
            {
                **teach_args,
                "idempotency_key": "stable-request-continue-0002",
                "human_completed": True,
                "request_demonstration": True,
            },
        )


def test_public_service_wrapper_applies_explicit_deployment_overrides(
    monkeypatch,
):
    import openadapt_flow.deployment as deployment_mod
    import openadapt_flow.runtime.durable as durable_mod
    from openadapt_flow.deployment import DeploymentConfig

    captured = {}

    class FakeService:
        def __init__(self, deployment):
            captured["deployment"] = deployment

        def __enter__(self):
            captured["entered"] = True
            return self

        def __exit__(self, *_args):
            captured["closed"] = True

    monkeypatch.setattr(
        deployment_mod,
        "load_deployment",
        lambda _path: DeploymentConfig(),
    )
    monkeypatch.setattr(
        durable_mod,
        "AttendedActionService",
        FakeService,
        raising=False,
    )
    with open_attended_service(
        enabled=True,
        deployment_config="deployment.yaml",
        url="https://example.invalid/app",
        headed=True,
        allow_model_grounding=True,
    ) as service:
        assert isinstance(service, FakeService)

    deployment = captured["deployment"]
    assert deployment.backend.url == "https://example.invalid/app"
    assert deployment.backend.headed is True
    assert deployment.runtime.allow_model_grounding is True
    assert captured["entered"] is True
    assert captured["closed"] is True


def test_exact_flow_public_service_context_execute_and_close(
    monkeypatch,
    paused_attention,
):
    from openadapt_flow.deployment import DeploymentConfig
    from openadapt_flow.runtime.durable import (
        AttendedActionRequest,
        AttendedActionService,
    )

    closed = []

    class FakeBackend:
        def close(self):
            closed.append(True)

    monkeypatch.setattr(
        "openadapt_flow.backends.factory.build_backend",
        lambda _config: FakeBackend(),
    )
    deployment = DeploymentConfig(
        backend={
            "kind": "windows",
            "agent_url": "http://127.0.0.1:5001",
        }
    )
    request = AttendedActionRequest(
        capability_digest=paused_attention["capability"].digest,
        idempotency_key="public-service-contract-0001",
        action="teach",
        disposition="teach_requested",
    )

    with AttendedActionService(deployment) as service:
        decision = service.execute(
            paused_attention["run"],
            request,
            operator="local-staff",
        )
        assert decision.status == "needs_demonstration"
    service.close()
    assert closed == [True]


def test_attended_bridge_requires_local_operator_identity(paused_attention):
    from openadapt_agent.attended import AttendedBridge

    with pytest.raises(AttendedBridgeError, match="operator identity"):
        AttendedBridge(
            paused_attention["runs"],
            allow_actions=True,
            operator="",
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX effective-uid identity contract")
def test_operator_identity_ignores_spoofed_posix_environment(
    monkeypatch,
    paused_attention,
):
    import pwd

    from openadapt_agent.attended import AttendedBridge

    monkeypatch.setenv("USER", "spoofed-agent")
    monkeypatch.setenv("LOGNAME", "spoofed-agent")
    bridge = AttendedBridge(paused_attention["runs"], allow_actions=True)
    assert bridge.operator == pwd.getpwuid(os.geteuid()).pw_name
    assert bridge.operator != "spoofed-agent"


def test_windows_operator_identity_uses_token_api_not_environment(monkeypatch):
    import ctypes
    from types import SimpleNamespace

    from openadapt_agent.attended import _windows_operator_identity

    class Advapi:
        @staticmethod
        def GetUserNameW(buffer, _size):
            buffer.value = "token-backed-user"
            return 1

    monkeypatch.setenv("USERNAME", "spoofed-agent")
    monkeypatch.setattr(
        ctypes,
        "windll",
        SimpleNamespace(advapi32=Advapi()),
        raising=False,
    )
    assert _windows_operator_identity() == "token-backed-user"


def test_symlinked_runs_root_is_not_scanned(paused_attention, tmp_path):
    from openadapt_agent.attended import AttendedBridge

    alias = tmp_path / "runs-alias"
    alias.symlink_to(paused_attention["runs"], target_is_directory=True)
    bridge = AttendedBridge(alias)
    assert bridge.list()["items"] == []


# ---------------------------------------------------------------------------
# Reject, and saying which kind of decider answered
# ---------------------------------------------------------------------------


def _journal(runs_dir):
    """Flow's append-only attended decision records for the single paused run."""
    from openadapt_flow.runtime.durable.attended import AttendedActionStore

    run = next(p for p in Path(runs_dir).iterdir() if p.is_dir())
    return AttendedActionStore(run)._read_log().decisions


def test_reject_is_offered_without_a_live_executor_unlike_continue_and_skip(
    paused_attention,
):
    """The brake must not be gated on the machinery only the accelerator needs.

    ``live_actions_ready`` gates continue and skip because they need Flow's
    deployment-bound executor to re-read the application and act on it.
    Reject dispatches no new action and resumes nothing, so it has nothing to gate
    on. Gating it anyway would leave a configuration that can say "proceed" --
    which writes to the system of record -- but cannot say "stop", and the only
    thing that removes is the brake.
    """
    bridge = make_bridge(paused_attention, allow_actions=True, service=None)
    specs = {spec.name for spec in bridge.list_tool_specs()}

    assert "reject_attention" in specs
    assert {"continue_attention", "skip_attention"} & specs == set(), (
        "the premise is that no live executor is configured here"
    )

    # And it is still off entirely when actions are not enabled at all.
    assert "reject_attention" not in {
        spec.name for spec in make_bridge(paused_attention).list_tool_specs()
    }


def test_reject_ends_the_run_and_is_recorded_as_an_automated_decision(
    paused_attention,
):
    """Two properties in one path, because they are only meaningful together.

    The bridge can now end a run. That is precisely why the record must say a
    MODEL ended it: `operator` is derived from the same local OS identity a
    person's own console uses, so without this the two are indistinguishable
    and any agreement rate silently mixes them.
    """
    from openadapt_flow.ir import RunReport, StepResult

    # Model a later pause. A successful earlier step can have an external
    # effect even though rejection itself dispatches no new action.
    report_path = paused_attention["run"] / "report.json"
    report = RunReport.model_validate_json(report_path.read_text())
    report.results.insert(
        0,
        StepResult(
            step_id="prior",
            intent="complete an earlier consequential step",
            ok=True,
        ),
    )
    report.save(paused_attention["run"])

    bridge = make_bridge(paused_attention, allow_actions=True, service=None)
    item, arguments = item_and_args(bridge, action="reject")

    result = bridge.dispatch("reject_attention", dict(arguments))
    assert result["action"] == "reject"
    assert result["status"] == "rejected"
    assert result["success"] is False
    # The rejection copy must not read like the escalation copy: that one says
    # the pause remains, this one says the run is over.
    assert "terminal" in result["message"]
    assert "remains available" not in result["message"]
    assert "dispatched no new action" in result["message"]
    assert "Earlier run actions may have effects" in result["message"]

    # A reject at a later pause does not erase the completed work before it.
    # The public response must direct the caller to that protected evidence.
    report = json.loads(report_path.read_text())
    assert report["results"][0]["step_id"] == "prior"
    assert report["results"][0]["ok"] is True

    decision = _journal(paused_attention["runs"])[-1]
    assert decision.action == "reject"
    assert decision.decided_by == "automation", (
        "a model's answer must never be recorded as a person's"
    )


def test_every_bridge_decision_declares_itself_automated_including_continue(
    paused_attention,
):
    """Not only reject. The bias this fixes was always about `continue`.

    `continue` resumes the run and can write to the system of record, and it
    has been model-callable all along. If provenance were attached only to the
    action added last, the population that actually matters would stay
    unlabelled.
    """
    executor = ResultExecutor()
    service = DirectFlowService(executor)
    bridge = make_bridge(paused_attention, allow_actions=True, service=service)
    item, arguments = item_and_args(bridge, action="continue")

    bridge.dispatch("continue_attention", dict(arguments))

    assert service.deciders == ["automation"], service.deciders
    assert _journal(paused_attention["runs"])[-1].decided_by == "automation"


def test_the_reject_tool_schema_is_closed_and_needs_explicit_confirmation(
    paused_attention,
):
    """Ending a run is not something a model may do by omission."""
    bridge = make_bridge(paused_attention, allow_actions=True, service=None)
    schema = next(
        spec.input_schema for spec in bridge.list_tool_specs() if spec.name == "reject_attention"
    )
    assert schema["additionalProperties"] is False
    assert schema["properties"]["confirm_run_must_not_proceed"]["const"] is True
    assert "confirm_run_must_not_proceed" in schema["required"]
    # No free text may reach a governed decision from a model.
    assert not any(
        prop.get("type") == "string" and "pattern" not in prop and "const" not in prop
        for name, prop in schema["properties"].items()
    ), schema["properties"]

    item, arguments = item_and_args(bridge, action="reject")
    arguments["confirm_run_must_not_proceed"] = False
    # `dispatch` re-raises the bridge's refusal as the public BridgeError.
    with pytest.raises(BridgeError, match="must be explicitly true"):
        bridge.dispatch("reject_attention", arguments)


def test_reject_is_exported_as_a_destructive_idempotent_local_mutation(
    paused_attention,
):
    bridge = make_bridge(paused_attention, allow_actions=True, service=None)
    spec = next(spec for spec in bridge.list_tool_specs() if spec.name == "reject_attention")

    assert spec.annotations == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    }

"""Adversarial contracts for the target-state attended action path."""

from __future__ import annotations

import json
import os
import shutil
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from openadapt_flow.console.app import create_app
from openadapt_flow.deployment import DeploymentConfig
from openadapt_flow.ir import (
    ActionKind,
    Guard,
    HaltObservation,
    LoopSpec,
    Postcondition,
    PostconditionKind,
    Predicate,
    PredicateKind,
    ProgramGraph,
    Relation,
    RunReport,
    State,
    StateKind,
    Step,
    StepResult,
    Transition,
    Workflow,
)
from openadapt_flow.runtime.durable.approval import ApprovalRecord
from openadapt_flow.runtime.durable.attended import (
    AttendedActionRefused,
    AttendedActionRequest,
    AttendedActionStore,
    BoundAttendedExecutor,
    TransitionObservation,
    execute_attended_action,
    issue_attended_capability,
    validate_attended_program_receipt,
)
from openadapt_flow.runtime.durable.attended_service import AttendedActionService
from openadapt_flow.runtime.durable.checkpoint import (
    CheckpointStore,
    PendingEscalation,
    RunManifest,
)
from openadapt_flow.runtime.durable.program_checkpoint import ProgramCheckpoint
from openadapt_flow.runtime.effects import Effect, EffectKind, EffectState
from openadapt_flow.runtime.replayer import Replayer
from tests.test_replayer import FakeBackend, FakeVision, Match


def _step(step_id: str, key: str, *, expect: str | None = None) -> Step:
    return Step(
        id=step_id,
        intent=f"press {key}",
        action=ActionKind.KEY,
        key=key,
        expect=(
            [
                Postcondition(
                    kind=PostconditionKind.TEXT_PRESENT,
                    text=expect,
                    timeout_s=0.01,
                )
            ]
            if expect
            else []
        ),
    )


def _paused(
    tmp_path: Path,
    *,
    workflow: Workflow | None = None,
    result: StepResult | None = None,
    transition_observation: TransitionObservation | None = None,
):
    workflow = workflow or Workflow(
        name="attended",
        steps=[_step("human", "A", expect="DONE"), _step("next", "B")],
    )
    bundle = tmp_path / "bundle"
    run = tmp_path / "run"
    workflow.save(bundle)
    store = CheckpointStore(run)
    store.write_manifest(
        RunManifest(
            run_id="run-instance-a",
            workflow_name=workflow.name,
            bundle_dir=str(bundle),
            params={},
        )
    )
    pending = PendingEscalation(
        workflow_name=workflow.name,
        step_index=0,
        step_id=workflow.steps[0].id,
        intent=workflow.steps[0].intent,
        category="human_required",
        reason="Please verify you are human",
        proposed_options=["complete in live app", "resume"],
        resume_from_index=0,
    )
    store.write_pending(pending)
    RunReport(
        workflow_name=workflow.name,
        started_at="2026-07-18T12:00:00+00:00",
        success=False,
        results=[
            result
            or StepResult(
                step_id=workflow.steps[0].id,
                intent=workflow.steps[0].intent,
                ok=False,
                error="Please verify you are human",
            )
        ],
        halt=HaltObservation(
            state_id=workflow.steps[0].id,
            intent=workflow.steps[0].intent,
            reason="Please verify you are human",
        ),
    ).save(run)
    capability = issue_attended_capability(
        run,
        store=store,
        pending=pending,
        workflow=workflow,
        result=result
        or StepResult(
            step_id=workflow.steps[0].id,
            intent=workflow.steps[0].intent,
            ok=False,
            error="Please verify you are human",
        ),
        transition_observation=transition_observation,
    )
    return workflow, bundle, run, store, capability


def _request(capability, action="continue", key="request-key-0001"):
    return AttendedActionRequest(
        capability_digest=capability.digest,
        idempotency_key=key,
        action=action,
        disposition=(
            "completed_by_operator" if action == "continue" else "not_applicable"
        ),
    )


class _ResultExecutor:
    def __init__(self):
        self.calls = 0

    def continue_run(self, run_dir, capability, approval):
        from openadapt_flow.runtime.durable.attended import AttendedExecutionResult

        self.calls += 1
        return AttendedExecutionResult(
            status="completed",
            message="verified",
            report_success=True,
            next_transition=capability.expected_next_transition,
        )

    def skip_run(self, run_dir, capability, approval):
        return self.continue_run(run_dir, capability, approval)


def test_capability_binds_run_bundle_pause_and_transition(tmp_path):
    _workflow, _bundle, _run, store, capability = _paused(tmp_path)
    assert capability.run_id == "run-instance-a"
    assert capability.step_id == "human"
    assert capability.expected_next_transition == "next"
    assert capability.bundle_version.startswith("sha256:")
    assert capability.expected_transition_digest.startswith("sha256:")
    assert AttendedActionStore(store.run_dir).read() == capability

    pending = store.read_pending()
    assert pending is not None
    store.write_pending(pending.model_copy(update={"step_id": "other"}))
    with pytest.raises(AttendedActionRefused, match="pause changed"):
        execute_attended_action(
            store.run_dir,
            _request(capability),
            operator="staff",
            executor=_ResultExecutor(),
        )


def test_capability_derives_only_semantically_supported_actions(tmp_path):
    _workflow, _bundle, _run, _store, verified = _paused(tmp_path / "verified")
    assert verified.allowed_actions == ("continue", "teach", "escalate")

    unverified_workflow = Workflow(
        name="unverified",
        steps=[_step("human", "A")],
    )
    _workflow, _bundle, _run, _store, unverified = _paused(
        tmp_path / "unverified", workflow=unverified_workflow
    )
    assert unverified.allowed_actions == ("teach", "escalate")

    optional_workflow = Workflow(
        name="optional",
        steps=[
            Step(
                id="human",
                intent="optional dismissal",
                action=ActionKind.KEY,
                key="A",
                guard=Guard(
                    predicate=Predicate(
                        kind=PredicateKind.TEXT_PRESENT, text="OPTIONAL"
                    ),
                    on_unmet="skip",
                ),
            )
        ],
    )
    _workflow, _bundle, _run, _store, optional = _paused(
        tmp_path / "optional", workflow=optional_workflow
    )
    assert optional.allowed_actions == ("skip", "teach", "escalate")

    absolute_effect_workflow = Workflow(
        name="absolute-effect",
        steps=[
            Step(
                id="human",
                intent="save",
                action=ActionKind.KEY,
                key="A",
                effects=[
                    Effect(
                        kind=EffectKind.RECORD_WRITTEN,
                        match={"id": "row-1"},
                        forbid_collateral_loss=False,
                    )
                ],
            )
        ],
    )
    _workflow, _bundle, _run, _store, absolute = _paused(
        tmp_path / "absolute", workflow=absolute_effect_workflow
    )
    assert absolute.allowed_actions == ("continue", "teach", "escalate")

    delta_effect_workflow = absolute_effect_workflow.model_copy(deep=True)
    delta_effect_workflow.name = "delta-effect"
    delta_effect_workflow.steps[0].effects[0].count_new_only = True
    _workflow, _bundle, _run, _store, delta = _paused(
        tmp_path / "delta", workflow=delta_effect_workflow
    )
    assert delta.allowed_actions == ("teach", "escalate")


def test_transition_baseline_is_keyed_signed_and_contains_no_raw_phi(tmp_path):
    raw_url = "https://payer.example/eligibility?patient=Jane-Roe&member=ABC123"
    raw_title = "Jane Roe — Eligibility ABC123"
    workflow = Workflow(
        name="relative",
        steps=[
            Step(
                id="human",
                intent="complete login",
                action=ActionKind.KEY,
                key="A",
                expect=[Postcondition(kind=PostconditionKind.URL_CHANGED)],
            )
        ],
    )
    _workflow, _bundle, run, _store, capability = _paused(
        tmp_path,
        workflow=workflow,
        transition_observation=TransitionObservation(
            url=raw_url,
            page_title=raw_title,
            page_count=1,
        ),
    )
    serialized = (run / "attended_capability.json").read_text()
    assert raw_url not in serialized
    assert raw_title not in serialized
    assert "Jane Roe" not in serialized
    assert capability.transition_baseline.url_digest.startswith("hmac-sha256:")
    assert capability.transition_baseline.title_digest.startswith("hmac-sha256:")
    assert capability.transition_baseline.page_count == 1
    assert capability.allowed_actions == ("continue", "teach", "escalate")
    store = AttendedActionStore(run)
    assert store.transition_value_digest("url", raw_url) == (
        capability.transition_baseline.url_digest
    )
    assert store.read() == capability


@pytest.mark.parametrize(
    ("kind", "baseline", "attribute", "changed", "unchanged"),
    [
        (
            PostconditionKind.URL_CHANGED,
            TransitionObservation(url="https://payer.example/login"),
            "url",
            "https://payer.example/home",
            "https://payer.example/login",
        ),
        (
            PostconditionKind.TITLE_CHANGED,
            TransitionObservation(page_title="Sign in"),
            "page_title",
            "Eligibility",
            "Sign in",
        ),
        (
            PostconditionKind.NEW_TAB_OPENED,
            TransitionObservation(page_count=1),
            "page_count",
            2,
            1,
        ),
    ],
)
def test_signed_relative_transition_confirms_common_human_redirects(
    tmp_path, kind, baseline, attribute, changed, unchanged
):
    workflow = Workflow(
        name=f"relative-{kind.value}",
        steps=[
            Step(
                id="human",
                intent="complete human challenge",
                action=ActionKind.KEY,
                key="A",
                expect=[Postcondition(kind=kind)],
            )
        ],
    )
    _workflow, _bundle, run, _store, capability = _paused(
        tmp_path / "changed",
        workflow=workflow,
        transition_observation=baseline,
    )
    backend = FakeBackend()
    setattr(backend, attribute, changed)
    accepted = execute_attended_action(
        run,
        _request(capability, key=f"relative-{kind.value}-changed"),
        operator="staff",
        executor=BoundAttendedExecutor(
            lambda _manifest: Replayer(
                backend, vision=FakeVision(), poll_interval_s=0.0
            )
        ),
    )
    assert accepted.status == "completed"
    assert not backend.actions

    _workflow, _bundle, run, store, capability = _paused(
        tmp_path / "unchanged",
        workflow=workflow,
        transition_observation=baseline,
    )
    backend = FakeBackend()
    setattr(backend, attribute, unchanged)
    refused = execute_attended_action(
        run,
        _request(capability, key=f"relative-{kind.value}-unchanged"),
        operator="staff",
        executor=BoundAttendedExecutor(
            lambda _manifest: Replayer(
                backend, vision=FakeVision(), poll_interval_s=0.0
            )
        ),
    )
    assert refused.status == "refused"
    assert "unchanged" in refused.message
    assert store.read_pending() is not None
    assert not backend.actions


def test_relative_continue_is_not_advertised_without_signed_baseline(tmp_path):
    workflow = Workflow(
        name="relative-no-baseline",
        steps=[
            Step(
                id="human",
                intent="complete login",
                action=ActionKind.KEY,
                key="A",
                expect=[Postcondition(kind=PostconditionKind.URL_CHANGED)],
            )
        ],
    )
    _workflow, _bundle, run, _store, capability = _paused(tmp_path, workflow=workflow)
    assert capability.allowed_actions == ("teach", "escalate")
    with pytest.raises(AttendedActionRefused, match="does not allow"):
        execute_attended_action(
            run,
            _request(capability, key="relative-missing-baseline"),
            operator="staff",
            executor=_ResultExecutor(),
        )


def test_durable_halt_automatically_captures_protected_transition_baseline(tmp_path):
    raw_url = "https://payer.example/member/Jane-Roe-ABC123"
    workflow = Workflow(
        name="auto-baseline",
        steps=[
            Step(
                id="human",
                intent="complete challenge",
                action=ActionKind.KEY,
                key="A",
                expect=[
                    Postcondition(
                        kind=PostconditionKind.URL_CHANGED,
                        timeout_s=0.01,
                    )
                ],
            )
        ],
    )
    bundle = tmp_path / "bundle"
    run = tmp_path / "run"
    workflow.save(bundle)
    backend = FakeBackend()
    backend.url = raw_url
    report = Replayer(
        backend,
        vision=FakeVision(),
        durable=True,
        poll_interval_s=0.0,
    ).run(workflow, bundle_dir=bundle, run_dir=run)
    assert report.success is False
    capability = AttendedActionStore(run).read()
    assert capability.transition_baseline.url_digest is not None
    assert "continue" in capability.allowed_actions
    assert raw_url not in (run / "attended_capability.json").read_text()


def test_program_pause_never_advertises_generic_continue_or_skip(tmp_path):
    workflow, _bundle, run, store, _first = _paused(tmp_path)
    pending = store.read_pending()
    assert pending is not None
    program_pending = pending.model_copy(
        update={
            "program": True,
            "state_id": "challenge-state",
            "created_at": "2026-07-18T13:30:00+00:00",
        }
    )
    store.write_pending(program_pending)
    capability = issue_attended_capability(
        run,
        store=store,
        pending=program_pending,
        workflow=workflow,
        result=StepResult(
            step_id="challenge-state",
            intent="complete challenge",
            ok=False,
            error="MFA required",
        ),
        transition_observation=TransitionObservation(url="https://payer.example/mfa"),
    )
    assert capability.allowed_actions == ("teach", "escalate")


def _attended_program(*, guarded_transition: bool = False, skippable: bool = False):
    transitions = (
        [
            Transition(
                guard=Predicate(
                    kind=PredicateKind.TEXT_PRESENT,
                    text="ROUTE_A",
                ),
                target="route-a",
            ),
            Transition(target="route-b"),
        ]
        if guarded_transition
        else [Transition(target="next")]
    )
    human = Step(
        id="human-step",
        intent="complete challenge",
        action=ActionKind.KEY,
        key="A",
        expect=[
            Postcondition(
                kind=PostconditionKind.TEXT_PRESENT,
                text="DONE",
                timeout_s=0.01,
            )
        ],
        guard=(
            Guard(
                predicate=Predicate(
                    kind=PredicateKind.TEXT_PRESENT,
                    text="OPTIONAL",
                ),
                on_unmet="skip",
            )
            if skippable
            else None
        ),
    )
    states = {
        "human": State(
            id="human",
            kind=StateKind.ACTION,
            step=human,
            transitions=transitions,
        ),
        "next": State(
            id="next",
            kind=StateKind.ACTION,
            step=_step("next-step", "B"),
            transitions=[Transition(target="done")],
        ),
        "route-a": State(
            id="route-a",
            kind=StateKind.ACTION,
            step=_step("route-a-step", "X"),
            transitions=[Transition(target="done")],
        ),
        "route-b": State(
            id="route-b",
            kind=StateKind.ACTION,
            step=_step("route-b-step", "Y"),
            transitions=[Transition(target="done")],
        ),
        "done": State(id="done", kind=StateKind.TERMINAL, outcome="success"),
    }
    return Workflow(
        name="attended-program",
        program=ProgramGraph(entry="human", states=states),
    )


def _run_attended_program_to_pause(tmp_path, workflow, *, optional_visible=False):
    bundle = tmp_path / "bundle"
    run = tmp_path / "run"
    workflow.save(bundle)
    vision = FakeVision()
    if optional_visible:
        vision.text_results["OPTIONAL"] = Match(
            point=(10, 10), region=(0, 0, 20, 20), confidence=1.0
        )
    initial_backend = FakeBackend()
    report = Replayer(
        initial_backend,
        vision=vision,
        durable=True,
        poll_interval_s=0.0,
    ).run(workflow, bundle_dir=bundle, run_dir=run)
    assert report.success is False
    return (
        bundle,
        run,
        initial_backend,
        CheckpointStore(run),
        AttendedActionStore(run).read(),
    )


def test_program_continue_commits_exact_receipt_without_reactuating_source(tmp_path):
    workflow = _attended_program()
    _bundle, run, initial_backend, store, capability = _run_attended_program_to_pause(
        tmp_path, workflow
    )
    assert initial_backend.actions == [("press", "A")]
    assert capability.allowed_actions == ("continue", "teach", "escalate")
    pending = store.read_pending()
    assert pending is not None
    assert [frame.state_id for frame in pending.program_frames] == ["human"]
    assert capability.program_cursor_digest is not None

    backend = FakeBackend()
    vision = FakeVision()
    vision.text_results["DONE"] = Match(
        point=(10, 10), region=(0, 0, 20, 20), confidence=1.0
    )
    decision = execute_attended_action(
        run,
        _request(capability, key="program-continue-request"),
        operator="staff",
        executor=BoundAttendedExecutor(
            lambda _manifest: Replayer(backend, vision=vision, poll_interval_s=0.0)
        ),
    )
    assert decision.status == "completed"
    assert decision.next_transition == "next"
    assert decision.transition_receipt_digest is not None
    assert backend.actions == [("press", "B")]
    checkpoint = store.program_checkpoints()[0]
    assert checkpoint is not None
    assert checkpoint.verified_state_id == "human"
    assert checkpoint.attended_transition is not None
    assert checkpoint.attended_transition.source_state_id == "human"
    assert checkpoint.attended_transition.target_state_id == "next"
    assert checkpoint.attended_transition.action == "continue"
    assert checkpoint.attended_transition.run_id == capability.run_id
    assert checkpoint.attended_transition.workflow_name == workflow.name
    assert checkpoint.attended_transition.bundle_version == capability.bundle_version
    assert checkpoint.attended_transition.pause_id == capability.pause_id
    assert checkpoint.attended_transition.pause_digest == capability.pause_digest
    assert checkpoint.attended_transition.signature.startswith("hmac-sha256:")
    receipt_path = (
        run
        / ".attended_program_receipts"
        / f"{checkpoint.attended_transition.pause_id}.json"
    )
    receipt_bytes = receipt_path.read_bytes()
    assert json.loads(receipt_bytes) == checkpoint.attended_transition.model_dump(
        mode="json"
    )
    assert AttendedActionStore(run).read_program_receipt(capability.pause_id) == (
        checkpoint.attended_transition
    )
    assert not any(
        sensitive in receipt_bytes.lower()
        for sensitive in (b"url", b"title", b"observed_text", b"done")
    )
    if os.name != "nt":
        assert receipt_path.parent.stat().st_mode & 0o077 == 0
        assert receipt_path.stat().st_mode & 0o077 == 0


def test_program_continue_rebinds_exact_pause_before_transition_commit(tmp_path):
    workflow = _attended_program()
    _bundle, run, _initial, store, capability = _run_attended_program_to_pause(
        tmp_path, workflow
    )
    backend = FakeBackend()
    vision = FakeVision()
    vision.text_results["DONE"] = Match(
        point=(10, 10), region=(0, 0, 20, 20), confidence=1.0
    )

    def factory(_manifest):
        replayer = Replayer(backend, vision=vision, poll_interval_s=0.0)
        original = replayer.revalidate_attended_program_completion

        def replace_pause_after_live_verification(*args, **kwargs):
            result = original(*args, **kwargs)
            pending = store.read_pending()
            assert pending is not None
            store.write_pending(
                pending.model_copy(
                    update={
                        "step_id": "independently-replaced-program-pause",
                        "created_at": "2026-07-18T13:00:00+00:00",
                    }
                )
            )
            return result

        replayer.revalidate_attended_program_completion = (
            replace_pause_after_live_verification
        )
        return replayer

    decision = execute_attended_action(
        run,
        _request(capability, key="program-pause-race-request"),
        operator="staff",
        executor=BoundAttendedExecutor(factory),
    )
    assert decision.status == "refused"
    assert "program pause changed before transition commit" in decision.message
    assert not backend.actions
    assert store.program_checkpoints() == []
    assert store.read_approval() is None
    assert not (run / ".attended_program_receipts").exists()
    pending = store.read_pending()
    assert pending is not None
    assert pending.step_id == "independently-replaced-program-pause"


def test_program_guarded_edge_is_selected_once_and_receipted(tmp_path):
    workflow = _attended_program(guarded_transition=True)
    _bundle, run, _initial, store, capability = _run_attended_program_to_pause(
        tmp_path, workflow
    )
    backend = FakeBackend()
    vision = FakeVision()
    match = Match(point=(10, 10), region=(0, 0, 20, 20), confidence=1.0)
    vision.text_results["DONE"] = match
    # The guarded edge sees ROUTE_A once. Resume must consume the receipt
    # rather than evaluating the guard a second time (which would now fail).
    vision.text_results["ROUTE_A"] = [match, None]
    decision = execute_attended_action(
        run,
        _request(capability, key="program-guarded-request"),
        operator="staff",
        executor=BoundAttendedExecutor(
            lambda _manifest: Replayer(backend, vision=vision, poll_interval_s=0.0)
        ),
    )
    assert decision.status == "completed"
    assert decision.next_transition == "route-a"
    assert backend.actions == [("press", "X")]
    checkpoint = store.program_checkpoints()[0]
    assert checkpoint is not None and checkpoint.attended_transition is not None
    assert checkpoint.attended_transition.target_state_id == "route-a"


def test_program_skip_uses_declared_guard_and_exact_receipt(tmp_path):
    workflow = _attended_program(skippable=True)
    _bundle, run, initial_backend, store, capability = _run_attended_program_to_pause(
        tmp_path,
        workflow,
        optional_visible=True,
    )
    assert initial_backend.actions == [("press", "A")]
    assert "skip" in capability.allowed_actions
    backend = FakeBackend()
    decision = execute_attended_action(
        run,
        _request(capability, action="skip", key="program-skip-request"),
        operator="staff",
        executor=BoundAttendedExecutor(
            lambda _manifest: Replayer(
                backend, vision=FakeVision(), poll_interval_s=0.0
            )
        ),
    )
    assert decision.status == "completed"
    assert backend.actions == [("press", "B")]
    checkpoint = store.program_checkpoints()[0]
    assert checkpoint is not None and checkpoint.attended_transition is not None
    assert checkpoint.attended_transition.action == "skip"
    assert checkpoint.attended_transition.target_state_id == "next"


def test_program_cursor_tamper_refuses_before_executor(tmp_path):
    workflow = _attended_program()
    _bundle, run, _initial, store, capability = _run_attended_program_to_pause(
        tmp_path, workflow
    )
    pending = store.read_pending()
    assert pending is not None
    frames = list(pending.program_frames)
    frames[-1] = frames[-1].model_copy(update={"state_id": "next"})
    store.write_pending(pending.model_copy(update={"program_frames": frames}))
    executor = _ResultExecutor()
    with pytest.raises(AttendedActionRefused, match="pause changed"):
        execute_attended_action(
            run,
            _request(capability, key="program-cursor-tamper"),
            operator="staff",
            executor=executor,
        )
    assert executor.calls == 0


def test_program_receipt_preserves_nested_loop_cursor_and_remaining_rows(tmp_path):
    body = ProgramGraph(
        entry="human",
        states={
            "human": State(
                id="human",
                kind=StateKind.ACTION,
                step=Step(
                    id="human-step",
                    intent="complete row",
                    action=ActionKind.KEY,
                    key="A",
                    expect=[
                        Postcondition(
                            kind=PostconditionKind.TEXT_PRESENT,
                            text="DONE",
                            timeout_s=0.01,
                        )
                    ],
                ),
                transitions=[Transition(target="body-done")],
            ),
            "body-done": State(
                id="body-done",
                kind=StateKind.TERMINAL,
                outcome="success",
            ),
        },
    )
    workflow = Workflow(
        name="attended-loop",
        program=ProgramGraph(
            entry="loop",
            states={
                "loop": State(
                    id="loop",
                    kind=StateKind.LOOP,
                    loop=LoopSpec(relation="queue", body="body"),
                    transitions=[Transition(target="done")],
                ),
                "done": State(id="done", kind=StateKind.TERMINAL, outcome="success"),
            },
        ),
        subflows={"body": body},
        data_sources=(
            {
                "queue": Relation(
                    name="queue",
                    rows=[{"row": "one"}, {"row": "two"}],
                )
            }
        ),
    )
    _bundle, run, initial_backend, store, capability = _run_attended_program_to_pause(
        tmp_path, workflow
    )
    assert initial_backend.actions == [("press", "A")]
    pending = store.read_pending()
    assert pending is not None
    assert [frame.graph_id for frame in pending.program_frames] == [
        "__program__",
        "body",
    ]
    assert pending.program_frames[-1].loop is not None
    assert pending.program_frames[-1].loop.row_index == 0

    backend = FakeBackend()
    vision = FakeVision()
    vision.text_results["DONE"] = Match(
        point=(10, 10), region=(0, 0, 20, 20), confidence=1.0
    )
    decision = execute_attended_action(
        run,
        _request(capability, key="program-loop-receipt"),
        operator="staff",
        executor=BoundAttendedExecutor(
            lambda _manifest: Replayer(backend, vision=vision, poll_interval_s=0.0)
        ),
    )
    assert decision.status == "completed"
    # Row one was completed by the person. Only row two is actuated by resume.
    assert backend.actions == [("press", "A")]
    receipt_checkpoint = store.program_checkpoints()[0]
    assert receipt_checkpoint.attended_transition is not None
    assert receipt_checkpoint.frames[-1].loop is not None
    assert receipt_checkpoint.frames[-1].loop.row_index == 0
    assert receipt_checkpoint.attended_transition.target_state_id == "body-done"


def test_program_transition_refuses_a_different_checkpoint_at_reserved_sequence(
    tmp_path,
):
    workflow = _attended_program()
    _bundle, run, _initial, store, capability = _run_attended_program_to_pause(
        tmp_path, workflow
    )
    pending = store.read_pending()
    assert pending is not None
    store.write_program_checkpoint(
        ProgramCheckpoint(
            workflow_name=workflow.name,
            seq=1,
            verified_state_id="unrelated",
            frames=list(pending.program_frames),
            bound_params={},
            bundle_version=capability.bundle_version,
        )
    )
    backend = FakeBackend()
    vision = FakeVision()
    vision.text_results["DONE"] = Match(
        point=(10, 10), region=(0, 0, 20, 20), confidence=1.0
    )
    decision = execute_attended_action(
        run,
        _request(capability, key="program-sequence-conflict"),
        operator="staff",
        executor=BoundAttendedExecutor(
            lambda _manifest: Replayer(backend, vision=vision, poll_interval_s=0.0)
        ),
    )
    assert decision.status == "refused"
    assert "sequence advanced differently" in decision.message
    assert not backend.actions
    assert store.read_pending() is not None


def test_program_resume_refuses_tampered_receipt_target(tmp_path):
    workflow = _attended_program(guarded_transition=True)
    _bundle, run, _initial, store, capability = _run_attended_program_to_pause(
        tmp_path, workflow
    )
    pending = store.read_pending()
    assert pending is not None
    vision = FakeVision()
    vision.text_results["DONE"] = Match(
        point=(10, 10), region=(0, 0, 20, 20), confidence=1.0
    )
    vision.text_results["ROUTE_A"] = Match(
        point=(10, 10), region=(0, 0, 20, 20), confidence=1.0
    )
    decision = execute_attended_action(
        run,
        _request(capability, key="program-receipt-before-tamper"),
        operator="staff",
        executor=BoundAttendedExecutor(
            lambda _manifest: Replayer(
                FakeBackend(), vision=vision, poll_interval_s=0.0
            )
        ),
    )
    assert decision.status == "completed"
    checkpoint = store.program_checkpoints()[0]
    assert checkpoint is not None and checkpoint.attended_transition is not None
    assert checkpoint.attended_transition.target_state_id == "route-a"
    tampered_receipt = checkpoint.attended_transition.model_copy(
        # route-b is also declared, so structure-only validation would accept
        # it. The signed atomic receipt must still reject the substitution.
        update={"target_state_id": "route-b"}
    )
    manifest = store.read_manifest()
    assert manifest is not None
    with pytest.raises(AttendedActionRefused, match="atomic transition receipt"):
        validate_attended_program_receipt(
            run,
            checkpoint=checkpoint.model_copy(
                update={"attended_transition": tampered_receipt}
            ),
            pending=pending.model_copy(update={"status": "approved"}),
            manifest=manifest,
            live_bundle_version=capability.bundle_version,
        )


def test_program_receipt_cannot_replay_across_run_identity(tmp_path):
    workflow = _attended_program()
    _bundle, run, _initial, store, capability = _run_attended_program_to_pause(
        tmp_path / "source", workflow
    )
    pending = store.read_pending()
    assert pending is not None
    vision = FakeVision()
    vision.text_results["DONE"] = Match(
        point=(10, 10), region=(0, 0, 20, 20), confidence=1.0
    )
    decision = execute_attended_action(
        run,
        _request(capability, key="program-cross-run-source"),
        operator="staff",
        executor=BoundAttendedExecutor(
            lambda _manifest: Replayer(
                FakeBackend(), vision=vision, poll_interval_s=0.0
            )
        ),
    )
    assert decision.status == "completed"

    copied = tmp_path / "copied-run"
    shutil.copytree(run, copied)
    copied_store = CheckpointStore(copied)
    manifest = copied_store.read_manifest()
    assert manifest is not None
    copied_store.write_manifest(manifest.model_copy(update={"run_id": "other-run"}))
    copied_store.write_pending(pending.model_copy(update={"status": "approved"}))
    copied_manifest = copied_store.read_manifest()
    receipt_checkpoint = copied_store.program_checkpoints()[0]
    copied_pending = copied_store.read_pending()
    assert copied_manifest is not None and copied_pending is not None
    with pytest.raises(AttendedActionRefused, match="run/bundle/pause/state/frame"):
        validate_attended_program_receipt(
            copied,
            checkpoint=receipt_checkpoint,
            pending=copied_pending,
            manifest=copied_manifest,
            live_bundle_version=capability.bundle_version,
        )


def test_bound_executor_serializes_its_shared_live_session(tmp_path):
    _workflow, _bundle, run, _store, capability = _paused(tmp_path)
    executor = BoundAttendedExecutor(lambda _manifest: pytest.fail("factory called"))
    approval = ApprovalRecord(
        approver="staff",
        resolution="completed by operator",
        bundle_version=capability.bundle_version,
        workflow_name=capability.workflow_name,
        run_dir=str(run),
    )
    assert executor._live_session_lock.acquire(blocking=False)
    try:
        result = executor.continue_run(run, capability, approval)
    finally:
        executor._live_session_lock.release()
    assert result.status == "refused"
    assert "serving another attended action" in result.message


def test_repeated_halt_on_same_step_gets_a_new_exact_pause_capability(tmp_path):
    workflow, _bundle, run, store, first = _paused(tmp_path)
    pending = store.read_pending()
    assert pending is not None
    repeated = pending.model_copy(update={"created_at": "2026-07-18T13:00:00+00:00"})
    store.write_pending(repeated)
    second = issue_attended_capability(
        run,
        store=store,
        pending=repeated,
        workflow=workflow,
        result=StepResult(
            step_id="human",
            intent="press A",
            ok=False,
            error="Please verify you are human",
        ),
    )
    assert second.pause_id != first.pause_id
    assert second.pause_digest != first.pause_digest
    history = json.loads((run / "attended_capability_history.json").read_text())
    assert [item["pause_id"] for item in history] == [first.pause_id]
    with pytest.raises(AttendedActionRefused, match="stale"):
        execute_attended_action(
            run,
            _request(first),
            operator="staff",
            executor=_ResultExecutor(),
        )


def test_tampered_capability_and_stale_page_refuse(tmp_path):
    _workflow, _bundle, run, _store, capability = _paused(tmp_path)
    path = run / "attended_capability.json"
    raw = json.loads(path.read_text())
    raw["step_id"] = "attacker"
    path.write_text(json.dumps(raw))
    with pytest.raises(AttendedActionRefused, match="signature"):
        AttendedActionStore(run).read()

    # Rebuild and present a stale UI digest.
    other = tmp_path / "other"
    _workflow, _bundle, run, _store, capability = _paused(other)
    stale = _request(capability).model_copy(
        update={"capability_digest": "sha256:" + "0" * 64}
    )
    with pytest.raises(AttendedActionRefused, match="stale"):
        execute_attended_action(
            run, stale, operator="staff", executor=_ResultExecutor()
        )


def test_capability_cannot_be_replayed_into_another_run(tmp_path):
    _workflow, _bundle, run, _store, capability = _paused(tmp_path / "one")
    copied = tmp_path / "two" / "run"
    shutil.copytree(run, copied)
    copied_store = CheckpointStore(copied)
    manifest = copied_store.read_manifest()
    assert manifest is not None
    copied_store.write_manifest(manifest.model_copy(update={"run_id": "other-run"}))
    with pytest.raises(AttendedActionRefused, match="transition binding"):
        execute_attended_action(
            copied,
            _request(capability),
            operator="staff",
            executor=_ResultExecutor(),
        )


def test_bundle_revision_change_refuses_before_executor(tmp_path):
    workflow, bundle, run, _store, capability = _paused(tmp_path)
    workflow.steps.append(_step("changed", "C"))
    workflow.save(bundle)
    executor = _ResultExecutor()
    with pytest.raises(Exception, match="bundle"):
        execute_attended_action(
            run,
            _request(capability),
            operator="staff",
            executor=executor,
        )
    assert executor.calls == 0


def test_expired_capability_refuses_before_executor(tmp_path):
    _workflow, _bundle, run, _store, capability = _paused(tmp_path)
    executor = _ResultExecutor()
    after_expiry = datetime.fromisoformat(capability.expires_at) + timedelta(seconds=1)
    with pytest.raises(Exception, match="expired"):
        execute_attended_action(
            run,
            _request(capability),
            operator="staff",
            executor=executor,
            now=after_expiry,
        )
    assert executor.calls == 0


def test_same_request_is_idempotent_and_conflicting_reuse_refuses(tmp_path):
    _workflow, _bundle, run, _store, capability = _paused(tmp_path)
    executor = _ResultExecutor()
    request = _request(capability)
    first = execute_attended_action(run, request, operator="staff", executor=executor)
    second = execute_attended_action(run, request, operator="staff", executor=executor)
    assert first == second
    assert executor.calls == 1
    conflict = request.model_copy(
        update={"action": "skip", "disposition": "not_applicable"}
    )
    with pytest.raises(AttendedActionRefused, match="different request"):
        execute_attended_action(run, conflict, operator="staff", executor=executor)


def test_crash_after_delivery_started_becomes_uncertain_and_never_retries(tmp_path):
    _workflow, _bundle, run, _store, capability = _paused(tmp_path)

    class Explodes(_ResultExecutor):
        def continue_run(self, run_dir, capability, approval):
            self.calls += 1
            raise RuntimeError("worker died after delivery boundary")

    executor = Explodes()
    request = _request(capability)
    with pytest.raises(RuntimeError):
        execute_attended_action(run, request, operator="staff", executor=executor)
    statuses = [
        item["status"]
        for item in json.loads((run / "attended_decisions.json").read_text())[
            "decisions"
        ]
    ]
    assert statuses == ["prepared", "delivery_started", "delivery_uncertain"]
    with pytest.raises(AttendedActionRefused, match="automatic retry"):
        execute_attended_action(run, request, operator="staff", executor=executor)
    with pytest.raises(AttendedActionRefused, match="another request"):
        execute_attended_action(
            run,
            request.model_copy(update={"idempotency_key": "request-key-0002"}),
            operator="staff",
            executor=executor,
        )
    assert executor.calls == 1


def test_challenge_payload_has_no_answer_code_or_raw_path_surface():
    with pytest.raises(ValidationError):
        AttendedActionRequest.model_validate(
            {
                "capability_digest": "sha256:" + "0" * 64,
                "idempotency_key": "request-key-0001",
                "action": "continue",
                "captcha_answer": "solve-me",
            }
        )
    with pytest.raises(ValidationError):
        AttendedActionRequest.model_validate(
            {
                "capability_digest": "sha256:" + "0" * 64,
                "idempotency_key": "request-key-0001",
                "action": "teach",
                "fix_path": "../../secret.json",
            }
        )


def test_all_actions_require_operator_identity_and_matching_disposition(tmp_path):
    _workflow, _bundle, run, _store, capability = _paused(tmp_path)
    with pytest.raises(Exception, match="authenticated operator"):
        execute_attended_action(
            run,
            _request(capability),
            operator="",
            executor=_ResultExecutor(),
        )
    request = _request(capability).model_copy(update={"disposition": "cannot_complete"})
    with pytest.raises(AttendedActionRefused, match="disposition"):
        execute_attended_action(
            run,
            request,
            operator="staff",
            executor=_ResultExecutor(),
        )


def test_missing_or_insecure_capability_secret_never_recreates_authority(tmp_path):
    _workflow, _bundle, run, _store, _capability = _paused(tmp_path / "missing")
    secret = run / ".attended_capability.key"
    secret.unlink()
    with pytest.raises(AttendedActionRefused, match="key is missing"):
        AttendedActionStore(run).read()
    assert not secret.exists()

    if os.name != "nt":
        _workflow, _bundle, run, _store, _capability = _paused(tmp_path / "permissions")
        secret = run / ".attended_capability.key"
        secret.chmod(0o644)
        with pytest.raises(AttendedActionRefused, match="permissions"):
            AttendedActionStore(run).read()


def test_bound_continue_verifies_then_resumes_after_human_step(tmp_path):
    _workflow, _bundle, run, store, capability = _paused(tmp_path)
    backends: list[FakeBackend] = []

    def factory(_manifest):
        backend = FakeBackend()
        backends.append(backend)
        vision = FakeVision()
        vision.text_results = {
            "DONE": Match(point=(10, 10), region=(0, 0, 20, 20), confidence=1.0)
        }
        return Replayer(backend, vision=vision, poll_interval_s=0.0)

    decision = execute_attended_action(
        run,
        _request(capability),
        operator="front-desk",
        executor=BoundAttendedExecutor(factory),
    )
    assert decision.status == "completed"
    assert decision.report_success is True
    assert len(backends) == 1  # verify and continue the exact live session
    assert all(("press", "A") not in backend.actions for backend in backends)
    assert ("press", "B") in backends[0].actions
    checkpoints = store.checkpoints()
    assert [checkpoint.step_id for checkpoint in checkpoints] == ["human", "next"]
    assert checkpoints[0].actuation == "human_attended"
    assert store.read_pending() is None
    manifest = store.read_manifest()
    assert manifest is not None and manifest.run_id == "run-instance-a"


def test_linear_continue_rebinds_exact_pause_before_checkpoint_commit(tmp_path):
    _workflow, _bundle, run, store, capability = _paused(tmp_path)
    backend = FakeBackend()
    vision = FakeVision()
    vision.text_results = {
        "DONE": Match(point=(10, 10), region=(0, 0, 20, 20), confidence=1.0)
    }

    def factory(_manifest):
        replayer = Replayer(backend, vision=vision, poll_interval_s=0.0)
        original = replayer.revalidate_attended_completion

        def replace_pause_after_live_verification(*args, **kwargs):
            result = original(*args, **kwargs)
            pending = store.read_pending()
            assert pending is not None
            store.write_pending(
                pending.model_copy(
                    update={
                        "step_id": "independently-replaced-pause",
                        "created_at": "2026-07-18T13:00:00+00:00",
                    }
                )
            )
            return result

        replayer.revalidate_attended_completion = replace_pause_after_live_verification
        return replayer

    decision = execute_attended_action(
        run,
        _request(capability, key="request-key-pause-race"),
        operator="front-desk",
        executor=BoundAttendedExecutor(factory),
    )
    assert decision.status == "refused"
    assert "pause changed before checkpoint commit" in decision.message
    assert not backend.actions
    assert store.checkpoints() == []
    assert store.read_approval() is None
    pending = store.read_pending()
    assert pending is not None
    assert pending.step_id == "independently-replaced-pause"


def test_continue_refuses_live_postcondition_failure_without_actuation(tmp_path):
    _workflow, _bundle, run, store, capability = _paused(tmp_path)
    backends: list[FakeBackend] = []

    def factory(_manifest):
        backend = FakeBackend()
        backends.append(backend)
        return Replayer(backend, vision=FakeVision(), poll_interval_s=0.0)

    decision = execute_attended_action(
        run,
        _request(capability, key="request-key-refuse1"),
        operator="staff",
        executor=BoundAttendedExecutor(factory),
    )
    assert decision.status == "refused"
    assert decision.report_success is False
    assert all(not backend.actions for backend in backends)
    assert store.read_pending() is not None


def test_continue_that_halts_later_rotates_to_the_new_exact_pause(tmp_path):
    workflow = Workflow(
        name="attended-chain",
        steps=[
            _step("human", "A", expect="DONE"),
            _step("next", "B", expect="FINISHED"),
        ],
    )
    _workflow, _bundle, run, store, first = _paused(tmp_path, workflow=workflow)
    backend = FakeBackend()
    vision = FakeVision()
    vision.text_results = {
        "DONE": Match(point=(10, 10), region=(0, 0, 20, 20), confidence=1.0)
    }
    decision = execute_attended_action(
        run,
        _request(first, key="request-key-chain01"),
        operator="staff",
        executor=BoundAttendedExecutor(
            lambda _manifest: Replayer(
                backend,
                vision=vision,
                poll_interval_s=0.0,
            )
        ),
    )
    assert decision.status == "halted"
    assert ("press", "A") not in backend.actions
    assert ("press", "B") in backend.actions
    pending = store.read_pending()
    assert pending is not None and pending.step_id == "next"
    second = AttendedActionStore(run).read()
    assert second.pause_id != first.pause_id
    assert second.step_id == "next"
    history = json.loads((run / "attended_capability_history.json").read_text())
    assert [item["pause_id"] for item in history] == [first.pause_id]


def test_continue_refuses_effect_that_needs_missing_delivery_baseline(tmp_path):
    effectful = Workflow(
        name="effectful",
        steps=[
            Step(
                id="human",
                intent="human save",
                action=ActionKind.KEY,
                key="A",
                effects=[
                    Effect(
                        kind=EffectKind.RECORD_WRITTEN,
                        match={"id": "row-1"},
                        count_new_only=True,
                    )
                ],
            )
        ],
    )
    _workflow, _bundle, run, store, capability = _paused(tmp_path, workflow=effectful)

    class CurrentRecords:
        substrate = "fake"

        def capture_pre_state(self, context=None):
            return EffectState(
                substrate="fake",
                reachable=True,
                records=[{"id": "row-1"}],
            )

        def verify(self, expected, before, context=None):
            raise AssertionError("attended readback must not reuse delivery verify")

    def factory(_manifest):
        return Replayer(
            FakeBackend(),
            vision=FakeVision(),
            effect_verifier=CurrentRecords(),
            poll_interval_s=0.0,
        )

    assert "continue" not in capability.allowed_actions
    with pytest.raises(AttendedActionRefused, match="does not allow"):
        execute_attended_action(
            run,
            _request(capability, key="request-key-effect1"),
            operator="staff",
            executor=BoundAttendedExecutor(factory),
        )
    assert store.read_pending() is not None


def test_continue_confirms_absolute_effect_from_current_record_readback(tmp_path):
    effectful = Workflow(
        name="absolute-effect",
        steps=[
            Step(
                id="human",
                intent="human save",
                action=ActionKind.KEY,
                key="A",
                effects=[
                    Effect(
                        kind=EffectKind.RECORD_WRITTEN,
                        match={"id": "row-1"},
                        forbid_collateral_loss=False,
                    )
                ],
            )
        ],
    )
    _workflow, _bundle, run, store, capability = _paused(tmp_path, workflow=effectful)
    assert "continue" in capability.allowed_actions

    class CurrentRecords:
        substrate = "fake"

        def capture_pre_state(self, context=None):
            return EffectState(
                substrate="fake",
                reachable=True,
                records=[{"id": "row-1"}],
            )

        def verify(self, expected, before, context=None):
            raise AssertionError("attended readback must not reuse delivery verify")

    backend = FakeBackend()
    decision = execute_attended_action(
        run,
        _request(capability, key="request-key-absolute-effect"),
        operator="staff",
        executor=BoundAttendedExecutor(
            lambda _manifest: Replayer(
                backend,
                vision=FakeVision(),
                effect_verifier=CurrentRecords(),
                poll_interval_s=0.0,
            )
        ),
    )
    assert decision.status == "completed"
    assert store.checkpoints()[0].effect_verified is True
    assert not backend.actions


def test_skip_requires_declared_nonconsequential_skip_semantics(tmp_path):
    generic, _bundle, run, store, capability = _paused(tmp_path / "generic")
    executor = BoundAttendedExecutor(
        lambda _manifest: Replayer(
            FakeBackend(), vision=FakeVision(), poll_interval_s=0.0
        )
    )
    assert "skip" not in capability.allowed_actions
    with pytest.raises(AttendedActionRefused, match="does not allow"):
        execute_attended_action(
            run,
            _request(capability, action="skip", key="request-key-skip1"),
            operator="staff",
            executor=executor,
        )
    assert store.read_pending() is not None
    assert generic.steps[0].guard is None

    optional = Workflow(
        name="optional",
        steps=[
            Step(
                id="optional",
                intent="optional dismissal",
                action=ActionKind.KEY,
                key="A",
                guard=Guard(
                    predicate=Predicate(
                        kind=PredicateKind.TEXT_PRESENT, text="OPTIONAL"
                    ),
                    on_unmet="skip",
                ),
            ),
            _step("next", "B"),
        ],
    )
    _workflow, _bundle, run, store, capability = _paused(
        tmp_path / "optional", workflow=optional
    )
    decision = execute_attended_action(
        run,
        _request(capability, action="skip", key="request-key-skip2"),
        operator="staff",
        executor=executor,
    )
    assert decision.status == "completed"
    assert store.checkpoints()[0].skipped is True


def test_teach_and_escalate_are_audited_without_actuation(tmp_path):
    _workflow, _bundle, run, store, capability = _paused(tmp_path)
    teach = execute_attended_action(
        run,
        AttendedActionRequest(
            capability_digest=capability.digest,
            idempotency_key="request-key-teach",
            action="teach",
            disposition="teach_requested",
        ),
        operator="staff",
    )
    assert teach.status == "needs_demonstration"
    assert "identity-evidence" in teach.message
    escalated = execute_attended_action(
        run,
        AttendedActionRequest(
            capability_digest=capability.digest,
            idempotency_key="request-key-escalate",
            action="escalate",
            disposition="needs_assistance",
        ),
        operator="staff",
    )
    assert escalated.status == "escalated"
    assert store.read_pending() is not None


def test_encrypted_pause_uses_environment_key_and_protected_capability_secret(
    tmp_path, monkeypatch
):
    key = "correct horse battery staple"
    workflow = Workflow(
        name="sealed",
        steps=[_step("human", "A", expect="DONE")],
    )
    bundle = tmp_path / "bundle"
    run = tmp_path / "run"
    workflow.save(bundle, encrypt=True, key=key)
    store = CheckpointStore(run, key=key)
    store.write_manifest(
        RunManifest(
            run_id="sealed-run",
            workflow_name=workflow.name,
            bundle_dir=str(bundle),
        )
    )
    pending = PendingEscalation(
        workflow_name=workflow.name,
        step_index=0,
        step_id="human",
        category="human_required",
        reason="MFA required",
    )
    store.write_pending(pending)
    capability = issue_attended_capability(
        run,
        store=store,
        pending=pending,
        workflow=workflow,
        result=StepResult(
            step_id="human", intent="press A", ok=False, error="MFA required"
        ),
    )
    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", key)
    decision = execute_attended_action(
        run,
        AttendedActionRequest(
            capability_digest=capability.digest,
            idempotency_key="request-key-sealed",
            action="escalate",
            disposition="needs_assistance",
        ),
        operator="staff",
    )
    assert decision.status == "escalated"
    assert (run / "pending_escalation.json.enc").is_file()
    assert not (run / "pending_escalation.json").exists()
    assert (run / ".attended_capability.key").stat().st_mode & 0o077 == 0


def test_lease_refuses_concurrent_or_crashed_delivery(tmp_path):
    _workflow, _bundle, run, _store, capability = _paused(tmp_path)
    request = _request(capability)
    store = AttendedActionStore(run)
    with store.lease(request):
        with pytest.raises(AttendedActionRefused, match="already in progress"):
            with store.lease(request):
                pass

    expired = {
        "request_digest": "sha256:" + "0" * 64,
        "idempotency_key": "old-request-key",
        "acquired_at": "2020-01-01T00:00:00+00:00",
        "expires_at": "2020-01-01T00:01:00+00:00",
    }
    store.lease_path.write_text(json.dumps(expired))
    with pytest.raises(AttendedActionRefused, match="delivery is uncertain"):
        with store.lease(
            request,
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ):
            pass


def test_attended_http_action_requires_auth_csrf_and_exact_capability(
    tmp_path, monkeypatch
):
    _workflow, bundle, run, _store, capability = _paused(tmp_path)
    monkeypatch.setattr(
        "openadapt_flow.console.app._local_operator_identity", lambda: "staff"
    )
    executor = _ResultExecutor()

    class Service:
        def execute(self, run_dir, request, *, operator):
            return execute_attended_action(
                run_dir,
                request,
                operator=operator,
                executor=executor,
            )

    app = create_app(
        bundle.parent,
        run.parent,
        allow_actions=True,
        attend=True,
        attended_service=Service(),
    )
    unauthenticated = TestClient(app, base_url="http://127.0.0.1")
    assert unauthenticated.get("/api/attention").status_code == 401
    client = TestClient(
        app,
        base_url="http://127.0.0.1",
        headers={
            "Authorization": f"Bearer {app.state.console_access_token}",
            "Origin": "http://127.0.0.1",
            "X-OpenAdapt-CSRF": app.state.console_csrf_token,
        },
    )
    item = client.get("/api/attention").json()[0]
    health = client.get("/api/health").json()
    assert health["attended_decisions_ready"] is True
    assert health["attended_actions_ready"] is True
    assert item["capability"]["digest"] == capability.digest
    assert "expected_next_transition" not in item["capability"]
    payload = _request(capability, key="request-key-http1").model_dump()
    response = client.post(
        f"/api/attention/{item['id']}/actions/continue",
        json=payload,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert set(response.json()) == {
        "action",
        "status",
        "message",
        "report_success",
    }
    assert executor.calls == 1

    wrong_path = client.post(
        f"/api/attention/{item['id']}/actions/skip",
        json={**payload, "idempotency_key": "request-key-http2"},
    )
    assert wrong_path.status_code == 400


def test_public_attended_service_executes_exact_request_on_owner(tmp_path, monkeypatch):
    _workflow, _bundle, run, _store, capability = _paused(tmp_path)
    executor = _ResultExecutor()
    monkeypatch.setattr(
        "openadapt_flow.runtime.durable.attended_service._deployment_executor",
        lambda _deployment, *, key: nullcontext(executor),
    )

    with AttendedActionService(DeploymentConfig()) as service:
        decision = service.execute(
            run,
            _request(capability, key="request-key-public-service"),
            operator="staff",
        )
        owner_thread = service._owner.owner_thread_id

    assert decision.status == "completed"
    assert executor.calls == 1
    assert owner_thread is not None


def test_attended_http_can_teach_or_escalate_without_live_executor(
    tmp_path, monkeypatch
):
    _workflow, bundle, run, _store, capability = _paused(tmp_path)
    monkeypatch.setattr(
        "openadapt_flow.console.app._local_operator_identity", lambda: "staff"
    )
    app = create_app(
        bundle.parent,
        run.parent,
        allow_actions=True,
        attend=True,
    )
    client = TestClient(
        app,
        base_url="http://127.0.0.1",
        headers={
            "Authorization": f"Bearer {app.state.console_access_token}",
            "Origin": "http://127.0.0.1",
            "X-OpenAdapt-CSRF": app.state.console_csrf_token,
        },
    )
    health = client.get("/api/health").json()
    assert health["attended_decisions_ready"] is True
    assert health["attended_actions_ready"] is False
    item = client.get("/api/attention").json()[0]
    response = client.post(
        f"/api/attention/{item['id']}/actions/teach",
        json={
            "capability_digest": capability.digest,
            "idempotency_key": "request-key-http-teach",
            "action": "teach",
            "disposition": "teach_requested",
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "needs_demonstration"

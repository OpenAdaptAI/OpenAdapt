"""Source-identity authority for attended linear and program continuation."""

from __future__ import annotations

import json

import pytest

from openadapt_flow.ir import (
    ActionKind,
    Guard,
    IdentityCheck,
    Postcondition,
    PostconditionKind,
    Predicate,
    PredicateKind,
    Step,
    StepResult,
    Workflow,
)
from openadapt_flow.runtime.durable.approval import approval_pause_digest
from openadapt_flow.runtime.durable.attended import (
    AttendedActionRefused,
    AttendedActionStore,
    BoundAttendedExecutor,
    TransitionObservation,
    execute_attended_action,
    issue_attended_capability,
    validate_attended_checkpoint_identity,
)
from openadapt_flow.runtime.durable.authority import DurableAuthority
from openadapt_flow.runtime.durable.checkpoint import RunCheckpoint
from openadapt_flow.runtime.durable.program_checkpoint import ProgramCheckpoint
from openadapt_flow.runtime.replayer import Replayer
from tests.test_attended_actions import (
    _attended_program,
    _paused,
    _request,
    _run_attended_program_to_pause,
)
from tests.test_replayer import FakeBackend, FakeVision, Match

SOURCE_IDENTITY = IdentityCheck(
    status="verified",
    mode="structured",
    coverage=1.0,
    expected="source-record",
    observed="source-record",
)
NEXT_IDENTITY = IdentityCheck(
    status="verified",
    mode="structured",
    coverage=1.0,
    expected="next-record",
    observed="next-record",
)
MISMATCH_IDENTITY = IdentityCheck(
    status="mismatch",
    mode="structured",
    coverage=0.0,
    expected="source-record",
    observed="different-record",
)


def _identity_step(*, skippable: bool = False) -> Step:
    return Step(
        id="human",
        intent="complete the attended action",
        action=ActionKind.KEY,
        key="A",
        identity_armed=True,
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


def _halt_result(identity: IdentityCheck | None) -> StepResult:
    return StepResult(
        step_id="human",
        intent="complete the attended action",
        ok=False,
        error="operator action required",
        identity=identity,
    )


@pytest.mark.parametrize("identity", [None, MISMATCH_IDENTITY])
def test_required_source_identity_does_not_issue_continue(tmp_path, identity):
    workflow = Workflow(name="identity-required", steps=[_identity_step()])
    *_unused, capability = _paused(
        tmp_path,
        workflow=workflow,
        result=_halt_result(identity),
    )

    assert capability.source_identity_required is True
    assert "continue" not in capability.allowed_actions


def test_linear_resume_copies_signed_source_identity_not_live_next_identity(tmp_path):
    workflow = Workflow(
        name="linear-source-identity",
        steps=[
            _identity_step(),
            Step(id="next", intent="continue", action=ActionKind.KEY, key="B"),
        ],
    )
    _workflow, _bundle, run_dir, store, capability = _paused(
        tmp_path,
        workflow=workflow,
        result=_halt_result(SOURCE_IDENTITY),
    )

    def factory(_manifest):
        vision = FakeVision()
        vision.text_results["DONE"] = Match(
            point=(10, 10), region=(0, 0, 20, 20), confidence=1.0
        )
        replayer = Replayer(FakeBackend(), vision=vision, poll_interval_s=0.0)
        replayer.revalidate_attended_completion = lambda *args, **kwargs: StepResult(
            step_id="human",
            intent="complete the attended action",
            ok=True,
            identity=NEXT_IDENTITY,
            postconditions_ok=True,
            actuation="human_attended",
        )
        return replayer

    decision = execute_attended_action(
        run_dir,
        _request(capability, key="source-identity-linear"),
        operator="staff",
        executor=BoundAttendedExecutor(factory),
    )

    assert decision.status == "completed", decision.message
    checkpoint = store.checkpoints()[0]
    assert checkpoint.identity == SOURCE_IDENTITY
    assert checkpoint.identity != NEXT_IDENTITY
    assert checkpoint.attended_capability_digest == capability.digest


def test_linear_skip_carries_no_source_identity(tmp_path):
    workflow = Workflow(
        name="linear-source-skip",
        steps=[_identity_step(skippable=True)],
    )
    _workflow, _bundle, run_dir, store, capability = _paused(
        tmp_path,
        workflow=workflow,
        result=_halt_result(SOURCE_IDENTITY),
    )
    assert "skip" in capability.allowed_actions

    decision = execute_attended_action(
        run_dir,
        _request(capability, action="skip", key="source-identity-skip"),
        operator="staff",
        executor=BoundAttendedExecutor(
            lambda _manifest: Replayer(
                FakeBackend(), vision=FakeVision(), poll_interval_s=0.0
            )
        ),
    )

    assert decision.status == "completed", decision.message
    checkpoint = store.checkpoints()[0]
    assert checkpoint.skipped is True
    assert checkpoint.identity is None
    assert checkpoint.attended_capability_digest == capability.digest


def test_program_resume_copies_signed_source_identity_not_live_next_identity(tmp_path):
    workflow = _attended_program()
    source_step = workflow.program.states["human"].step
    assert source_step is not None
    source_step.identity_armed = True
    _bundle, run_dir, _initial, store, _missing_identity_capability = (
        _run_attended_program_to_pause(tmp_path, workflow)
    )
    pending = store.read_pending()
    manifest = store.read_manifest()
    assert pending is not None and manifest is not None
    authority = DurableAuthority(run_dir, store)
    prior_progress_digest = authority.validate(manifest).progress_digest
    capability = issue_attended_capability(
        run_dir,
        store=store,
        pending=pending,
        workflow=workflow,
        result=StepResult(
            step_id=source_step.id,
            intent=source_step.intent,
            ok=False,
            error="operator action required",
            identity=SOURCE_IDENTITY,
        ),
    )
    assert "continue" in capability.allowed_actions
    authority.advance(
        manifest,
        expected_progress_digest=prior_progress_digest,
        phase="paused",
        pause_binding_sha256=approval_pause_digest(pending),
    )

    def factory(_manifest):
        vision = FakeVision()
        vision.text_results["DONE"] = Match(
            point=(10, 10), region=(0, 0, 20, 20), confidence=1.0
        )
        replayer = Replayer(FakeBackend(), vision=vision, poll_interval_s=0.0)
        replayer.revalidate_attended_program_completion = lambda *args, **kwargs: (
            StepResult(
                step_id=source_step.id,
                intent=source_step.intent,
                ok=True,
                identity=NEXT_IDENTITY,
                postconditions_ok=True,
                actuation="human_attended",
            ),
            "next",
        )
        return replayer

    decision = execute_attended_action(
        run_dir,
        _request(capability, key="source-identity-program"),
        operator="staff",
        executor=BoundAttendedExecutor(factory),
    )

    assert decision.status == "completed", decision.message
    checkpoint = store.program_checkpoints()[0]
    assert checkpoint.identity == SOURCE_IDENTITY
    assert checkpoint.identity != NEXT_IDENTITY
    assert checkpoint.attended_capability_digest == capability.digest
    assert checkpoint.bundle_version == capability.bundle_version


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"attended_capability_digest": None}, "capability"),
        (
            {"attended_capability_digest": "sha256:" + "0" * 64},
            "capability",
        ),
        ({"identity": NEXT_IDENTITY}, "identity"),
    ],
)
def test_linear_checkpoint_capability_or_identity_tamper_refuses(
    tmp_path,
    update,
    message,
):
    workflow = Workflow(name="linear-tamper", steps=[_identity_step()])
    _workflow, _bundle, run_dir, store, capability = _paused(
        tmp_path,
        workflow=workflow,
        result=_halt_result(SOURCE_IDENTITY),
    )
    manifest = store.read_manifest()
    assert manifest is not None
    checkpoint = RunCheckpoint(
        run_id=manifest.run_id,
        workflow_name=workflow.name,
        bundle_version=capability.bundle_version,
        step_index=0,
        step_id="human",
        intent="complete the attended action",
        next_step_index=1,
        identity=SOURCE_IDENTITY,
        postconditions_ok=True,
        actuation="human_attended",
        attended_capability_digest=capability.digest,
    )
    assert (
        validate_attended_checkpoint_identity(
            run_dir,
            checkpoint=checkpoint,
            step=workflow.steps[0],
            manifest=manifest,
            live_bundle_version=capability.bundle_version,
        )
        == capability
    )

    with pytest.raises(AttendedActionRefused, match=message):
        validate_attended_checkpoint_identity(
            run_dir,
            checkpoint=checkpoint.model_copy(update=update),
            step=workflow.steps[0],
            manifest=manifest,
            live_bundle_version=capability.bundle_version,
        )


@pytest.mark.parametrize("identity_required", [False, True])
def test_pre_v3_capability_only_supports_non_identity_required_resume(
    tmp_path,
    identity_required,
):
    step = _identity_step().model_copy(update={"identity_armed": identity_required})
    workflow = Workflow(name="legacy-capability", steps=[step])
    result = _halt_result(SOURCE_IDENTITY if identity_required else None)
    _workflow, _bundle, run_dir, store, capability = _paused(
        tmp_path,
        workflow=workflow,
        result=result,
    )
    action_store = AttendedActionStore(run_dir)
    legacy = capability.model_copy(
        update={
            "schema_version": 2,
            "source_identity_required": False,
            "source_identity": None,
            "signature": "",
        }
    )
    legacy.signature = action_store._sign(legacy, create_key=False)
    action_store.capability_path.write_text(legacy.model_dump_json(indent=2))
    manifest = store.read_manifest()
    assert manifest is not None
    checkpoint = RunCheckpoint(
        run_id=manifest.run_id,
        workflow_name=workflow.name,
        bundle_version=legacy.bundle_version,
        step_index=0,
        step_id="human",
        intent=step.intent,
        next_step_index=1,
        identity=None,
        postconditions_ok=True,
        actuation="human_attended",
        attended_capability_digest=legacy.digest,
    )

    if identity_required:
        with pytest.raises(AttendedActionRefused, match="identity"):
            validate_attended_checkpoint_identity(
                run_dir,
                checkpoint=checkpoint,
                step=step,
                manifest=manifest,
                live_bundle_version=legacy.bundle_version,
            )
    else:
        assert (
            validate_attended_checkpoint_identity(
                run_dir,
                checkpoint=checkpoint,
                step=step,
                manifest=manifest,
                live_bundle_version=legacy.bundle_version,
            )
            == legacy
        )


def test_missing_historical_program_capability_refuses(tmp_path):
    workflow = _attended_program()
    _bundle, run_dir, _initial, store, original = _run_attended_program_to_pause(
        tmp_path, workflow
    )
    pending = store.read_pending()
    manifest = store.read_manifest()
    assert pending is not None and manifest is not None
    replacement = issue_attended_capability(
        run_dir,
        store=store,
        pending=pending,
        workflow=workflow,
        result=StepResult(
            step_id="human-step",
            intent="complete challenge",
            ok=False,
            error="MFA required",
        ),
        # A changed signed baseline moves the first capability into history.
        transition_observation=TransitionObservation(
            url="https://payer.example/challenge"
        ),
    )
    assert replacement.digest != original.digest
    step = workflow.program.states["human"].step
    assert step is not None
    checkpoint = ProgramCheckpoint(
        run_id=manifest.run_id,
        workflow_name=workflow.name,
        seq=1,
        verified_state_id="human",
        step_id=step.id,
        intent=step.intent,
        frames=list(pending.program_frames),
        bound_params={},
        identity=None,
        postconditions_ok=True,
        actuation="human_attended",
        attended_capability_digest=original.digest,
        bundle_version=original.bundle_version,
    )
    assert (
        validate_attended_checkpoint_identity(
            run_dir,
            checkpoint=checkpoint,
            step=step,
            manifest=manifest,
            live_bundle_version=original.bundle_version,
            state_id="human",
        )
        == original
    )

    AttendedActionStore(run_dir).capability_history_path.write_text(
        json.dumps([], indent=2)
    )
    with pytest.raises(AttendedActionRefused, match="capability"):
        validate_attended_checkpoint_identity(
            run_dir,
            checkpoint=checkpoint,
            step=step,
            manifest=manifest,
            live_bundle_version=original.bundle_version,
            state_id="human",
        )

"""Exact live-state and sealed-bundle binding for durable resume."""

from __future__ import annotations

import pytest

from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Postcondition,
    PostconditionKind,
    ProgramGraph,
    State,
    StateKind,
    Step,
    Transition,
    Workflow,
)
from openadapt_flow.runtime.durable import (
    ApprovalRequired,
    BundleMismatch,
    CheckpointStore,
    StateDiverged,
    bundle_version,
    resume,
)
from openadapt_flow.runtime.effects import Effect, EffectKind
from openadapt_flow.runtime.replayer import Replayer
from tests.test_durable_runtime import FakeSoRVerifier, _approval
from tests.test_replayer import FakeBackend, FakeVision, Match


def _effect(step_id: str) -> Effect:
    return Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"step": step_id},
        expected_count=1,
        timeout_s=0.1,
    )


def _absence_step(step_id: str, key: str) -> Step:
    return Step(
        id=step_id,
        intent=f"press {key}",
        action=ActionKind.KEY,
        key=key,
        risk="irreversible",
        expect=[
            Postcondition(
                kind=PostconditionKind.TEXT_ABSENT,
                text="Blocking error",
                timeout_s=0.0,
            )
        ],
        effects=[_effect(step_id)],
    )


def _linear_absence_workflow() -> Workflow:
    return Workflow(
        name="negative-state-linear",
        steps=[_absence_step("s0", "A"), _absence_step("s1", "B")],
    )


def _program_absence_workflow() -> Workflow:
    return Workflow(
        name="negative-state-program",
        program=ProgramGraph(
            entry="s0",
            states={
                "s0": State(
                    id="s0",
                    kind=StateKind.ACTION,
                    step=_absence_step("s0", "A"),
                    transitions=[Transition(target="s1")],
                ),
                "s1": State(
                    id="s1",
                    kind=StateKind.ACTION,
                    step=_absence_step("s1", "B"),
                    transitions=[Transition(target="done")],
                ),
                "done": State(
                    id="done",
                    kind=StateKind.TERMINAL,
                    outcome="success",
                ),
            },
        ),
    )


def _run_to_second_action_pause(tmp_path, workflow: Workflow):
    bundle = tmp_path / "bundle"
    run_dir = tmp_path / "run"
    workflow.save(bundle)
    verifier = FakeSoRVerifier()
    verifier.refute.add((("step", "s1"),))
    report = Replayer(
        FakeBackend(),
        vision=FakeVision(),
        effect_verifier=verifier,
        durable=True,
        poll_interval_s=0.0,
    ).run(workflow, bundle_dir=bundle, run_dir=run_dir)
    assert report.success is False
    return bundle, run_dir, verifier


def _vision_with_blocking_error() -> FakeVision:
    vision = FakeVision()
    vision.text_results["Blocking error"] = Match(
        point=(10, 10),
        region=(0, 0, 20, 20),
        confidence=1.0,
    )
    return vision


def test_program_resume_revalidates_retained_text_absent_before_input(tmp_path):
    bundle, run_dir, verifier = _run_to_second_action_pause(
        tmp_path, _program_absence_workflow()
    )
    verifier.refute.clear()
    backend = FakeBackend()

    with pytest.raises(StateDiverged, match="retained postcondition"):
        resume(
            run_dir,
            Replayer(
                backend,
                vision=_vision_with_blocking_error(),
                effect_verifier=verifier,
                poll_interval_s=0.0,
            ),
            approval=_approval(bundle),
        )

    assert backend.actions == []


def test_linear_resume_revalidates_retained_text_absent_before_input(tmp_path):
    bundle, run_dir, verifier = _run_to_second_action_pause(
        tmp_path, _linear_absence_workflow()
    )
    verifier.refute.clear()
    backend = FakeBackend()

    with pytest.raises(StateDiverged, match="retained postcondition"):
        resume(
            run_dir,
            Replayer(
                backend,
                vision=_vision_with_blocking_error(),
                effect_verifier=verifier,
                poll_interval_s=0.0,
            ),
            approval=_approval(bundle),
        )

    assert backend.actions == []


def test_resume_rechecks_bundle_inside_admission_before_input(tmp_path):
    bundle, run_dir, verifier = _run_to_second_action_pause(
        tmp_path, _linear_absence_workflow()
    )
    verifier.refute.clear()
    backend = FakeBackend()
    replayer = Replayer(
        backend,
        vision=FakeVision(),
        effect_verifier=verifier,
        poll_interval_s=0.0,
    )
    approved = _approval(bundle)
    admit = replayer._admit_durable_resume

    def mutate_bundle_then_admit(*args, **kwargs):
        changed = Workflow.load(bundle)
        changed.steps[1].intent = "changed after the outer resume check"
        changed.save(bundle)
        return admit(*args, **kwargs)

    replayer._admit_durable_resume = mutate_bundle_then_admit

    with pytest.raises(ApprovalRequired, match="changed after resume approval"):
        resume(run_dir, replayer, approval=approved)

    assert backend.actions == []


@pytest.mark.parametrize("asset_kind", ["locator", "identity", "postcondition"])
def test_replacing_sealed_template_invalidates_exact_resume_approval(
    tmp_path, asset_kind
):
    workflow = _linear_absence_workflow()
    bundle = tmp_path / "bundle"
    run_dir = tmp_path / "run"
    templates = bundle / "templates"
    templates.mkdir(parents=True)

    if asset_kind == "locator":
        relative = "templates/target.png"
        workflow.steps[1].anchor = Anchor(
            template=relative,
            region=(0, 0, 20, 20),
            click_point=(10, 10),
        )
    elif asset_kind == "identity":
        relative = "templates/identity.png"
        workflow.steps[1].anchor = Anchor(
            template="templates/target.png",
            region=(0, 0, 20, 20),
            click_point=(10, 10),
            identifier_crop=relative,
            identifier_region=(0, 0, 20, 20),
        )
        (bundle / "templates/target.png").write_bytes(b"stable target")
    else:
        relative = "templates/postcondition.png"
        workflow.steps[1].expect[0].template = relative
    asset = bundle / relative
    asset.write_bytes(b"qualified template")

    workflow.save(bundle)
    verifier = FakeSoRVerifier()
    verifier.refute.add((("step", "s1"),))
    report = Replayer(
        FakeBackend(),
        vision=FakeVision(),
        effect_verifier=verifier,
        durable=True,
        poll_interval_s=0.0,
    ).run(workflow, bundle_dir=bundle, run_dir=run_dir)
    assert report.success is False
    assert CheckpointStore(run_dir).checkpoints()
    approval = _approval(bundle)
    approved_version = approval.bundle_version

    asset.write_bytes(b"replacement template")
    workflow.save(bundle)
    assert bundle_version(bundle) != approved_version

    backend = FakeBackend()
    with pytest.raises(BundleMismatch, match="program changed"):
        resume(
            run_dir,
            Replayer(
                backend,
                vision=FakeVision(),
                effect_verifier=verifier,
                poll_interval_s=0.0,
            ),
            approval=approval,
        )
    assert backend.actions == []

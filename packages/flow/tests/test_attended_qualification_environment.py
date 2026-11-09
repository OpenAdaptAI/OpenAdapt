"""Qualification continuation contracts; fixture vision is unit-test-only."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from openadapt_flow.ir import (
    ActionKind,
    Postcondition,
    PostconditionKind,
    ProgramGraph,
    RunReport,
    State,
    StateKind,
    Step,
    Transition,
    Workflow,
)
from openadapt_flow.qualification import (
    ActionRiskClassification,
    EnvironmentBoundary,
    QualificationActionTarget,
    QualificationCase,
    QualificationCaseKind,
    QualificationOutcome,
    add_case,
    init_project,
    set_action_classification,
)
from openadapt_flow.runtime.authorization import (
    GovernedRunAuthorization,
    runtime_inputs_digest,
)
from openadapt_flow.runtime.durable.attended import (
    AttendedActionRefused,
    AttendedActionRequest,
    AttendedActionStore,
    BoundAttendedExecutor,
    execute_attended_action,
)
from openadapt_flow.runtime.replayer import Replayer
from tests.test_qualification_fault_contract import (
    _ExternalObserver,
    _ObservedBackend,
    _SettleResult,
)
from tests.test_replayer import FakeVision, Match


class _SettledVision(FakeVision):
    def wait_settled_result(self, backend, **_kwargs):
        return _SettleResult(backend.screenshot())


@pytest.mark.parametrize("program", [False, True], ids=["linear", "program"])
@pytest.mark.parametrize("changed_surface", [False, True], ids=["same", "changed"])
def test_attended_qualification_resume_keeps_target_and_rechecks_live_surface(
    tmp_path: Path, program: bool, changed_surface: bool
) -> None:
    observer = _ExternalObserver(target_kind="linux")
    steps = [
        Step(
            id=step_id,
            intent=f"Observe {text}",
            action=ActionKind.WAIT,
            wait_s=0,
            expect=[
                Postcondition(
                    kind=PostconditionKind.TEXT_PRESENT, text=text, timeout_s=0.01
                )
            ],
        )
        for step_id, text in [("operator", "DONE"), ("next", "NEXT")]
    ]
    workflow = Workflow(name="attended-environment", steps=[] if program else steps)
    if program:
        workflow.program = ProgramGraph(
            entry="operator-state",
            states={
                "operator-state": State(
                    id="operator-state",
                    kind=StateKind.ACTION,
                    step=steps[0],
                    transitions=[Transition(target="next-state")],
                ),
                "next-state": State(
                    id="next-state",
                    kind=StateKind.ACTION,
                    step=steps[1],
                    transitions=[Transition(target="done")],
                ),
                "done": State(id="done", kind=StateKind.TERMINAL, outcome="success"),
            },
        )
    init_project(
        workflow,
        environment=EnvironmentBoundary(
            target_kind="linux",
            application="Attended qualification fixture",
            application_identity="third-party-app",
            application_version="2026.7",
            environment_observer_id=observer.observer_id,
            environment_observer_contract_sha256=observer.contract_sha256,
            environment_digest="7" * 64,
            runtime_version="test",
        ),
    )
    for step in steps:
        set_action_classification(
            workflow,
            ActionRiskClassification(
                step_id=step.id,
                classification="read_only",
                explanation="Observe a declared screen condition without input",
                operator_confirmed=True,
            ),
        )
    input_digest = runtime_inputs_digest(workflow, None, None)
    add_case(
        workflow,
        QualificationCase(
            id="representative",
            kind=QualificationCaseKind.REPRESENTATIVE,
            expected_outcome=QualificationOutcome.VERIFIED,
            runtime_input_sha256=input_digest,
            action_targets=[
                QualificationActionTarget(step_id=s.id, actuation_path="gui")
                for s in steps
            ],
        ),
    )
    bundle = tmp_path / "bundle"
    workflow.save(bundle)
    workflow = Workflow.load(bundle)
    assert workflow.manifest is not None and workflow.qualification is not None
    project = workflow.qualification
    run_id = "attended-environment-run"
    authorization = GovernedRunAuthorization(
        bundle_content_digest=workflow.manifest.content_digest,
        runtime_inputs_digest=input_digest,
        admitted_policy_name="synthetic-observation",
        admitted_policy_contract_sha256="5" * 64,
        execution_profile="standard",
        minimum_effect_tier=3,
        approval_source="qualification-campaign",
        qualification_project_id=project.project_id,
        qualification_project_revision=project.revision,
        qualification_project_contract_sha256=project.contract_sha256(),
        qualification_case_id="representative",
        qualification_campaign_id_sha256="1" * 64,
        qualification_case_input_sha256=input_digest,
        qualification_run_id_sha256=hashlib.sha256(run_id.encode()).hexdigest(),
        qualification_case_kind="representative",
        qualification_case_action_paths={s.id: "gui" for s in steps},
    )
    backend = _ObservedBackend()
    vision = _SettledVision()

    def replayer_for(_manifest=None):
        return Replayer(
            backend,
            vision=vision,
            governed_authorization=authorization,
            qualification_environment_observer=observer,
            durable=True,
            require_settled=True,
            poll_interval_s=0,
        )

    run = tmp_path / "run"
    initial = replayer_for().run(
        workflow,
        bundle_dir=bundle,
        run_dir=run,
        run_id=run_id,
        execution_target_kind="linux",
    )
    assert not initial.success
    assert initial.observed_environment_digest == "7" * 64
    capability = AttendedActionStore(run).read()
    assert capability is not None and "continue" in capability.allowed_actions
    vision.text_results.update(
        {
            text: Match(point=(10, 10), region=(0, 0, 20, 20), confidence=1)
            for text in ("DONE", "NEXT")
        }
    )
    observer.target_kind = "windows" if changed_surface else "linux"
    request = AttendedActionRequest(
        capability_digest=capability.digest,
        idempotency_key="attended-target-binding",
        action="continue",
        disposition="completed_by_operator",
    )
    if changed_surface:
        with pytest.raises(AttendedActionRefused):
            execute_attended_action(
                run,
                request,
                operator="synthetic-operator",
                decided_by="automation",
                executor=BoundAttendedExecutor(replayer_for),
            )
    else:
        decision = execute_attended_action(
            run,
            request,
            operator="synthetic-operator",
            decided_by="automation",
            executor=BoundAttendedExecutor(replayer_for),
        )
        assert decision.status == "completed"
    final = RunReport.model_validate_json((run / "report.json").read_text())
    assert final.execution_target_kind == "linux"
    if changed_surface:
        assert not final.execution_completed
        assert "observer returned the wrong surface" in final.results[-1].error
    else:
        # These observation-only steps have no independent effect contract.
        # Completion must preserve that limit rather than claim VERIFIED.
        assert final.execution_completed
        assert final.transaction_outcome == "COMPLETED_UNVERIFIED"
        assert all(result.ok for result in final.results)
        assert final.observed_environment_digest == "7" * 64
    assert backend.actions == []

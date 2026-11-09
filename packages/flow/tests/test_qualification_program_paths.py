"""Representative Program evidence must prove one exact runtime graph walk."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from openadapt_flow.execution_profiles import build_outcome_envelope
from openadapt_flow.ir import (
    ActionKind,
    LoopSpec,
    Postcondition,
    PostconditionKind,
    ProgramExecutionScopeFrame,
    ProgramGraph,
    ProgramTransitionEvidence,
    Relation,
    RunReport,
    State,
    StateKind,
    Step,
    StepResult,
    Transition,
    Workflow,
    predicate_contract_sha256,
)
from openadapt_flow.qualification import (
    ActionRiskClass,
    ActionRiskClassification,
    EnvironmentBoundary,
    EvidenceRef,
    QualificationActionTarget,
    QualificationCase,
    QualificationCaseKind,
    QualificationCaseResult,
    QualificationOutcome,
    QualificationRefusalCode,
    _case_run_report_integrity_error,
    init_project,
    set_action_classification,
    workflow_contract_sha256,
)
from openadapt_flow.qualification_environment import (
    qualification_environment_binding_sha256,
)
from openadapt_flow.runtime.authorization import (
    effective_runtime_params,
    runtime_inputs_bytes,
)


def _program_workflow() -> Workflow:
    write = Step(
        id="write",
        intent="Record one row",
        action=ActionKind.KEY,
        key="enter",
        expect=[Postcondition(kind=PostconditionKind.TITLE_CHANGED)],
    )
    return Workflow(
        name="qualified-program",
        program=ProgramGraph(
            entry="start",
            states={
                "start": State(
                    id="start",
                    kind=StateKind.BRANCH,
                    transitions=[Transition(target="rows")],
                ),
                "rows": State(
                    id="rows",
                    kind=StateKind.LOOP,
                    loop=LoopSpec(relation="cases", body="row-body"),
                    transitions=[Transition(target="done")],
                ),
                "done": State(
                    id="done",
                    kind=StateKind.TERMINAL,
                    outcome="success",
                ),
            },
        ),
        subflows={
            "row-body": ProgramGraph(
                entry="write-state",
                states={
                    "write-state": State(
                        id="write-state",
                        kind=StateKind.ACTION,
                        step=write,
                        transitions=[Transition(target="row-done")],
                    ),
                    "row-done": State(
                        id="row-done",
                        kind=StateKind.TERMINAL,
                        outcome="success",
                    ),
                },
            )
        },
        # The representative input supplies the rows. The bundle does not.
        data_sources={"cases": Relation(name="cases")},
    )


def _program_case_evidence(
    tmp_path: Path,
) -> tuple[
    Workflow,
    QualificationCase,
    QualificationCaseResult,
    Path,
    Path,
]:
    workflow = _program_workflow()
    environment = EnvironmentBoundary(
        target_kind="citrix",
        application="Qualified application",
        application_identity="qualified-app",
        application_version="1",
        environment_observer_id="fixture-observer",
        environment_observer_contract_sha256="c" * 64,
        environment_digest="b" * 64,
        runtime_version="1.26.0",
        required_capabilities=["pixel_observation"],
    )
    project = init_project(workflow, environment=environment)
    set_action_classification(
        workflow,
        ActionRiskClassification(
            step_id="write",
            classification=ActionRiskClass.READ_ONLY,
            explanation="The fixture uses a key delivery without a business write.",
            operator_confirmed=True,
        ),
    )
    project = workflow.qualification
    assert project is not None
    worklists = {"cases": [{"record_id": "one"}, {"record_id": "two"}]}
    input_bytes = runtime_inputs_bytes(workflow, None, worklists)
    input_sha256 = hashlib.sha256(input_bytes).hexdigest()
    case = QualificationCase(
        id="program-representative",
        kind=QualificationCaseKind.REPRESENTATIVE,
        input_ref="program-input.json",
        runtime_input_sha256=input_sha256,
        action_targets=[
            QualificationActionTarget(step_id="write", actuation_path="gui")
        ],
        expected_outcome=QualificationOutcome.VERIFIED,
    )
    project.cases = [case]

    run_id_sha256 = hashlib.sha256(b"program-run").hexdigest()
    application_sha256 = hashlib.sha256(b"qualified-app").hexdigest()
    version_sha256 = hashlib.sha256(b"1").hexdigest()
    session_sha256 = "3" * 64
    observed_binding = qualification_environment_binding_sha256(
        target_kind="citrix",
        observer_id="fixture-observer",
        observer_contract_sha256="c" * 64,
        application_identity_sha256=application_sha256,
        application_version_sha256=version_sha256,
        environment_digest=environment.environment_digest,
        session_identity_sha256=session_sha256,
    )
    root_scope = ProgramExecutionScopeFrame(graph_id="__program__")

    def row_result(index: int) -> StepResult:
        return StepResult(
            step_id="write",
            intent="Record one row",
            ok=True,
            starting_state_settled=True,
            delivery_attempted=True,
            actuation="guarded_keyboard",
            postconditions_ok=True,
            program_scope=[
                root_scope,
                ProgramExecutionScopeFrame(
                    graph_id="row-body",
                    loop_state_id="rows",
                    relation="cases",
                    row_index=index,
                ),
            ],
        )

    report = RunReport(
        workflow_name=workflow.name,
        workflow_contract_sha256=workflow_contract_sha256(workflow),
        run_id_sha256=run_id_sha256,
        started_at="2026-07-28T00:00:00Z",
        execution_profile="standard",
        execution_outcome="VERIFIED",
        execution_completed=True,
        terminal_outcome="success",
        production_eligible=False,
        success=True,
        visited_states=[
            "start",
            "rows",
            "write-state",
            "row-done",
            "write-state",
            "row-done",
            "done",
        ],
        governed_authorization_id="qualification-program",
        governed_runtime_inputs_digest=input_sha256,
        governed_minimum_effect_tier=int(project.minimum_effect_tier),
        governed_qualification_project_id=project.project_id,
        governed_qualification_project_revision=project.revision,
        governed_qualification_project_contract_sha256=project.contract_sha256(),
        governed_qualification_campaign_id_sha256="4" * 64,
        governed_qualification_case_id_sha256=hashlib.sha256(
            case.id.encode()
        ).hexdigest(),
        governed_qualification_case_input_sha256=input_sha256,
        governed_qualification_run_id_sha256=run_id_sha256,
        governed_qualification_case_kind=case.kind.value,
        governed_qualification_case_action_paths={"write": "gui"},
        params=effective_runtime_params(workflow, None),
        qualification_evidence_only=True,
        execution_target_kind="citrix",
        observed_application_sha256=application_sha256,
        observed_application_version_sha256=version_sha256,
        observed_session_sha256=session_sha256,
        observed_environment_digest=environment.environment_digest,
        observed_environment_binding_sha256=observed_binding,
        qualification_environment_observer_id="fixture-observer",
        qualification_environment_observer_contract_sha256="c" * 64,
        results=[row_result(0), row_result(1)],
    )
    assert workflow.program is not None
    row_state = workflow.subflows["row-body"].states["write-state"]

    def transition_evidence(
        decision_index: int,
        state: State,
        scope: list[ProgramExecutionScopeFrame],
        target: str,
    ) -> ProgramTransitionEvidence:
        return ProgramTransitionEvidence(
            decision_index=decision_index,
            graph_id=scope[-1].graph_id,
            state_id=state.id,
            program_scope=scope,
            transition_index=0,
            guard_contract_sha256=predicate_contract_sha256(None),
            guard_verdict=True,
            selected=True,
            selected_target=target,
            guard_evidence_kind="unconditional",
            governed_runtime_inputs_digest=input_sha256,
        )

    report.program_transition_evidence = [
        transition_evidence(
            0,
            workflow.program.states["start"],
            [root_scope],
            "rows",
        ),
        transition_evidence(
            1,
            row_state,
            [
                root_scope,
                ProgramExecutionScopeFrame(
                    graph_id="row-body",
                    loop_state_id="rows",
                    relation="cases",
                    row_index=0,
                ),
            ],
            "row-done",
        ),
        transition_evidence(
            2,
            row_state,
            [
                root_scope,
                ProgramExecutionScopeFrame(
                    graph_id="row-body",
                    loop_state_id="rows",
                    relation="cases",
                    row_index=1,
                ),
            ],
            "row-done",
        ),
        transition_evidence(
            3,
            workflow.program.states["rows"],
            [root_scope],
            "done",
        ),
    ]
    report.outcome_envelope = build_outcome_envelope(
        report,
        workflow,
        runtime_worklists=worklists,
    )
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    report_path = evidence_root / "program-report.json"
    input_path = evidence_root / "program-input.json"
    report_bytes = report.model_dump_json().encode()
    report_path.write_bytes(report_bytes)
    input_path.write_bytes(input_bytes)
    result = QualificationCaseResult(
        case_id=case.id,
        project_id=project.project_id,
        project_revision=project.revision,
        project_contract_sha256=project.contract_sha256(),
        workflow_contract_sha256=workflow_contract_sha256(workflow),
        environment_contract_sha256=environment.contract_sha256(),
        environment_digest=environment.environment_digest,
        runtime_version=environment.runtime_version,
        runner_id="fixture-runner",
        runner_capabilities=["pixel_observation"],
        status="passed",
        observed_outcome=QualificationOutcome.VERIFIED,
        campaign_id_sha256="4" * 64,
        case_input_sha256=input_sha256,
        run_id_sha256=run_id_sha256,
        evidence=[
            EvidenceRef(
                kind="run_report",
                sha256=hashlib.sha256(report_bytes).hexdigest(),
                relative_path=report_path.name,
            ),
            EvidenceRef(
                kind="case_input",
                sha256=input_sha256,
                relative_path=input_path.name,
            ),
        ],
        attestation_key_id="fixture-runner",
    )
    return workflow, case, result, evidence_root, report_path


def _replace_report(
    *, result: QualificationCaseResult, path: Path, report: RunReport
) -> QualificationCaseResult:
    payload = report.model_dump_json().encode()
    path.write_bytes(payload)
    refs = [
        ref.model_copy(update={"sha256": hashlib.sha256(payload).hexdigest()})
        if ref.kind == "run_report"
        else ref
        for ref in result.evidence
    ]
    return result.model_copy(update={"evidence": refs})


def _integrity_error(
    workflow: Workflow,
    case: QualificationCase,
    result: QualificationCaseResult,
    evidence_root: Path,
) -> tuple[QualificationRefusalCode, str] | None:
    assert workflow.qualification is not None
    return _case_run_report_integrity_error(
        workflow=workflow,
        project=workflow.qualification,
        case=case,
        result=result,
        evidence_root=evidence_root,
    )


def test_representative_program_accepts_exact_runtime_worklist_trace(
    tmp_path: Path,
) -> None:
    workflow, case, result, evidence_root, _path = _program_case_evidence(tmp_path)

    assert _integrity_error(workflow, case, result, evidence_root) is None


@pytest.mark.parametrize(
    "mutation",
    ["missing", "weakened"],
)
def test_representative_program_refuses_unbound_outcome_envelope(
    tmp_path: Path,
    mutation: str,
) -> None:
    workflow, case, result, evidence_root, path = _program_case_evidence(tmp_path)
    report = RunReport.model_validate_json(path.read_bytes())
    if mutation == "missing":
        changed = report.model_copy(update={"outcome_envelope": None})
    else:
        assert report.outcome_envelope is not None
        weakened_counts = report.outcome_envelope.required_contracts.model_copy(
            update={"postcondition": 0, "effect": 0}
        )
        changed = report.model_copy(
            update={
                "outcome_envelope": report.outcome_envelope.model_copy(
                    update={
                        "required_contracts": weakened_counts,
                        "passed_contracts": weakened_counts,
                        "postcondition_evidence": [],
                        "evidence_classes": ["authorization"],
                    }
                )
            }
        )
    result = _replace_report(result=result, path=path, report=changed)

    error = _integrity_error(workflow, case, result, evidence_root)

    assert error is not None
    assert error[0] is QualificationRefusalCode.CASE_ATTESTATION_INVALID


@pytest.mark.parametrize(
    "visited_states",
    [
        [
            "rows",
            "write-state",
            "row-done",
            "write-state",
            "row-done",
            "done",
        ],
        [
            "start",
            "done",
            "write-state",
            "row-done",
            "write-state",
            "row-done",
        ],
    ],
    ids=["invalid-entry", "impossible-edge"],
)
def test_representative_program_refuses_invalid_graph_path(
    tmp_path: Path,
    visited_states: list[str],
) -> None:
    workflow, case, result, evidence_root, path = _program_case_evidence(tmp_path)
    report = RunReport.model_validate_json(path.read_bytes())
    result = _replace_report(
        result=result,
        path=path,
        report=report.model_copy(update={"visited_states": visited_states}),
    )

    error = _integrity_error(workflow, case, result, evidence_root)

    assert error is not None
    assert error[0] is QualificationRefusalCode.CASE_ATTESTATION_INVALID


def test_representative_program_refuses_reused_loop_row_scope(tmp_path: Path) -> None:
    workflow, case, result, evidence_root, path = _program_case_evidence(tmp_path)
    report = RunReport.model_validate_json(path.read_bytes())
    repeated_row = report.results[0].program_scope
    changed_results = [
        report.results[0],
        report.results[1].model_copy(update={"program_scope": repeated_row}),
    ]
    result = _replace_report(
        result=result,
        path=path,
        report=report.model_copy(update={"results": changed_results}),
    )

    error = _integrity_error(workflow, case, result, evidence_root)

    assert error is not None
    assert error[0] is QualificationRefusalCode.CASE_ATTESTATION_INVALID

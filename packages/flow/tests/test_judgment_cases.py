"""Invariant tests for local reviewed business-judgment qualification cases."""

from __future__ import annotations

import hashlib

import pytest

from openadapt_flow.ir import (
    ActionKind,
    BusinessDecisionOption,
    BusinessDecisionSpec,
    ParamKind,
    ParamSpec,
    Predicate,
    PredicateKind,
    ProgramGraph,
    State,
    StateKind,
    Step,
    Transition,
    Workflow,
    business_decision_transitions,
)
from openadapt_flow.judgment_cases import (
    JudgmentCaseProvenanceV1,
    JudgmentCaseV1,
    JudgmentDecisionBindingV1,
    JudgmentDisposition,
    JudgmentFactFieldV1,
    JudgmentFactSchemaBindingV1,
    JudgmentFactSchemaV1,
    JudgmentFactType,
    LocalEvidenceRefV1,
)
from openadapt_flow.qualification import (
    EnvironmentBoundary,
    QualificationError,
    QualificationRefusalCode,
    evaluate_judgment_case_qualification,
    evaluate_qualification,
    init_project,
    set_judgment_cases,
    workflow_contract_sha256,
)


def _workflow() -> Workflow:
    decision = BusinessDecisionSpec(
        question="Which reviewed path should continue?",
        authorized_roles=("operator",),
        output_param="review_outcome",
        options=(
            BusinessDecisionOption(
                id="priority", label="Priority", value="priority", target="done"
            ),
            BusinessDecisionOption(
                id="standard", label="Standard", value="standard", target="done"
            ),
        ),
        revalidation=(Predicate(kind=PredicateKind.TEXT_PRESENT, text="Ready"),),
    )
    return Workflow(
        name="judgment-cases",
        param_specs={
            "review_outcome": ParamSpec(
                name="review_outcome",
                type=ParamKind.ENUM,
                required=False,
                choices=["priority", "standard"],
            )
        },
        program=ProgramGraph(
            entry="review",
            states={
                "review": State(
                    id="review",
                    kind=StateKind.BUSINESS_DECISION,
                    decision=decision,
                    transitions=business_decision_transitions(decision),
                ),
                "done": State(
                    id="done",
                    kind=StateKind.ACTION,
                    step=Step(id="done", intent="finish", action=ActionKind.WAIT),
                    transitions=[Transition(target="end")],
                ),
                "end": State(id="end", kind=StateKind.TERMINAL, outcome="success"),
            },
        ),
    )


def _environment() -> EnvironmentBoundary:
    return EnvironmentBoundary(
        target_kind="web",
        application="Synthetic app",
        application_version="1",
        environment_digest="a" * 64,
        runtime_version="test",
    )


def _schema() -> JudgmentFactSchemaV1:
    return JudgmentFactSchemaV1(
        fields={
            "urgency": JudgmentFactFieldV1(
                type=JudgmentFactType.ENUM, allowed_values=("high", "normal")
            ),
            "complete": JudgmentFactFieldV1(type=JudgmentFactType.BOOLEAN),
        }
    )


def _case(
    workflow: Workflow,
    schema: JudgmentFactSchemaV1,
    *,
    case_id: str,
    facts: dict[str, bool | str],
    disposition: JudgmentDisposition = JudgmentDisposition.HUMAN_NODE,
    option_id: str | None = None,
    contrast_case_ids: tuple[str, ...] = (),
) -> JudgmentCaseV1:
    assert workflow.program is not None
    decision = workflow.program.states["review"].decision
    assert decision is not None
    return JudgmentCaseV1(
        id=case_id,
        decision=JudgmentDecisionBindingV1(
            graph_id="__program__",
            state_id="review",
            workflow_contract_sha256=workflow_contract_sha256(workflow),
            decision_contract_sha256=decision.contract_sha256(),
        ),
        fact_schema_sha256=schema.contract_sha256(),
        facts=facts,
        local_evidence=(
            LocalEvidenceRefV1(
                relative_path=f"cases/{case_id}.json", sha256="b" * 64, kind="report"
            ),
        ),
        provenance=JudgmentCaseProvenanceV1(
            source="policy_review",
            source_ref_sha256="c" * 64,
            reviewer_role="operator",
            reviewer_principal_ref_sha256="d" * 64,
        ),
        disposition=disposition,
        reviewed_rule_id="urgency_rule"
        if disposition is JudgmentDisposition.AUTOMATIC_RULE
        else None,
        option_id=option_id,
        contrast_case_ids=contrast_case_ids,
    )


def test_cases_bind_current_contract_and_retained_human_authority_is_not_a_rule():
    workflow = _workflow()
    init_project(workflow, environment=_environment())
    schema = _schema()
    case = _case(
        workflow, schema, case_id="human-1", facts={"urgency": "high", "complete": True}
    )
    set_judgment_cases(
        workflow,
        schemas=(
            JudgmentFactSchemaBindingV1(
                graph_id="__program__", state_id="review", fact_schema=schema
            ),
        ),
        cases=(case,),
    )
    assert workflow.qualification is not None
    assert workflow.qualification.last_certification is None
    report = evaluate_judgment_case_qualification(workflow)
    assert report.passed
    assert report.retained_human_authority_count == 1
    assert {finding.code.value for finding in report.findings} == {
        "retained_human_authority"
    }


def test_automatic_case_requires_reciprocal_counterfactual_and_never_infers_a_rule():
    workflow = _workflow()
    init_project(workflow, environment=_environment())
    schema = _schema()
    first = _case(
        workflow,
        schema,
        case_id="high",
        facts={"urgency": "high", "complete": True},
        disposition=JudgmentDisposition.AUTOMATIC_RULE,
        option_id="priority",
        contrast_case_ids=("normal",),
    )
    second = _case(
        workflow,
        schema,
        case_id="normal",
        facts={"urgency": "normal", "complete": True},
        disposition=JudgmentDisposition.AUTOMATIC_RULE,
        option_id="standard",
        contrast_case_ids=("high",),
    )
    set_judgment_cases(
        workflow,
        schemas=(
            JudgmentFactSchemaBindingV1(
                graph_id="__program__", state_id="review", fact_schema=schema
            ),
        ),
        cases=(first, second),
    )
    assert evaluate_judgment_case_qualification(workflow).passed
    assert (
        workflow.program.states["review"].decision
        == _workflow().program.states["review"].decision
    )


def test_stale_decision_binding_is_rejected_before_storage():
    workflow = _workflow()
    init_project(workflow, environment=_environment())
    schema = _schema()
    case = _case(
        workflow, schema, case_id="stale", facts={"urgency": "high", "complete": False}
    ).model_copy(
        update={
            "decision": JudgmentDecisionBindingV1(
                graph_id="__program__",
                state_id="review",
                workflow_contract_sha256="0" * 64,
                decision_contract_sha256="0" * 64,
            )
        }
    )
    with pytest.raises(QualificationError, match="invalid judgment case binding"):
        set_judgment_cases(
            workflow,
            schemas=(
                JudgmentFactSchemaBindingV1(
                    graph_id="__program__", state_id="review", fact_schema=schema
                ),
            ),
            cases=(case,),
        )


def test_more_evidence_required_refuses_certification_but_human_node_does_not():
    workflow = _workflow()
    init_project(workflow, environment=_environment())
    schema = _schema()
    case = _case(
        workflow,
        schema,
        case_id="needs-evidence",
        facts={"urgency": "high", "complete": False},
        disposition=JudgmentDisposition.MORE_EVIDENCE_REQUIRED,
    )
    set_judgment_cases(
        workflow,
        schemas=(
            JudgmentFactSchemaBindingV1(
                graph_id="__program__", state_id="review", fact_schema=schema
            ),
        ),
        cases=(case,),
    )
    report = evaluate_qualification(workflow)
    assert report.passed is False
    assert QualificationRefusalCode.JUDGMENT_CASE_MORE_EVIDENCE_REQUIRED in {
        refusal.code for refusal in report.refusals
    }


@pytest.mark.parametrize(
    "path",
    ("C:/cases/a.json", r"\\server\share\a.json", r"cases\..\..\a.json"),
)
def test_local_evidence_refuses_windows_absolute_paths(path: str):
    with pytest.raises(ValueError, match="local relative path"):
        LocalEvidenceRefV1(sha256="a" * 64, relative_path=path, kind="report")


def test_judgment_evidence_is_verified_under_local_evidence_root(tmp_path):
    workflow = _workflow()
    init_project(workflow, environment=_environment())
    schema = _schema()
    payload = b"reviewed local evidence"
    evidence_path = tmp_path / "cases"
    evidence_path.mkdir()
    (evidence_path / "human-1.json").write_bytes(payload)
    case = _case(
        workflow, schema, case_id="human-1", facts={"urgency": "high", "complete": True}
    ).model_copy(
        update={
            "local_evidence": (
                LocalEvidenceRefV1(
                    relative_path="cases/human-1.json",
                    sha256=hashlib.sha256(payload).hexdigest(),
                    kind="report",
                ),
            )
        }
    )
    set_judgment_cases(
        workflow,
        schemas=(
            JudgmentFactSchemaBindingV1(
                graph_id="__program__", state_id="review", fact_schema=schema
            ),
        ),
        cases=(case,),
    )
    verified = evaluate_qualification(workflow, evidence_root=tmp_path)
    assert QualificationRefusalCode.JUDGMENT_CASE_EVIDENCE_UNVERIFIED not in {
        refusal.code for refusal in verified.refusals
    }
    (evidence_path / "human-1.json").write_bytes(b"changed")
    changed = evaluate_qualification(workflow, evidence_root=tmp_path)
    assert QualificationRefusalCode.JUDGMENT_CASE_EVIDENCE_UNVERIFIED in {
        refusal.code for refusal in changed.refusals
    }

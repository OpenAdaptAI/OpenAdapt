"""Typed, durable human business decisions inside ProgramGraph workflows.

These tests protect control authority and durable behavior. They do not pin
operator-facing copy or component markup.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from datetime import datetime, timedelta

import pytest

from openadapt_flow.bundle_validation import validate_workflow
from openadapt_flow.execution_profiles import _program_action_trace
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    BusinessDecisionEvidenceRequirement,
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
    StepResult,
    Transition,
    Workflow,
    business_decision_transitions,
)
from openadapt_flow.learning.gate import program_regression_gate
from openadapt_flow.runtime.durable import CheckpointStore, resume
from openadapt_flow.runtime.durable.attended import issue_attended_capability
from openadapt_flow.runtime.durable.business_decision import (
    BusinessDecisionPrincipal,
    BusinessDecisionRefused,
    BusinessDecisionStore,
    BusinessDecisionSubmission,
)
from openadapt_flow.runtime.replayer import Replayer
from tests.test_replayer import FakeBackend, FakeVision


def _decision_spec(*, required_evidence: bool = True) -> BusinessDecisionSpec:
    requirement = BusinessDecisionEvidenceRequirement(
        id="reviewed_frame",
        label="Retained application frame",
    )
    evidence = (requirement,) if required_evidence else ()
    option_evidence = (requirement.id,) if required_evidence else ()
    return BusinessDecisionSpec(
        question="Which declared path should continue?",
        authorized_roles=("operator", "supervisor"),
        output_param="review_outcome",
        options=(
            BusinessDecisionOption(
                id="accept",
                label="Accept",
                value="accepted",
                target="accepted_action",
                required_evidence=option_evidence,
            ),
            BusinessDecisionOption(
                id="reject",
                label="Reject",
                value="rejected",
                target="rejected_action",
                required_evidence=option_evidence,
            ),
        ),
        evidence_requirements=evidence,
        expires_after_s=300,
        revalidation=(
            Predicate(
                kind=PredicateKind.TEXT_PRESENT,
                text="Ready for reviewed action",
                intent="the reviewed application state remains ready",
            ),
        ),
    )


def _decision_workflow(*, required_evidence: bool = True) -> Workflow:
    spec = _decision_spec(required_evidence=required_evidence)
    decision = State(
        id="review",
        kind=StateKind.BUSINESS_DECISION,
        decision=spec,
        transitions=business_decision_transitions(spec),
    )
    return Workflow(
        name="typed-business-decision",
        param_specs={
            "review_outcome": ParamSpec(
                name="review_outcome",
                type=ParamKind.ENUM,
                required=False,
                choices=["accepted", "rejected"],
            )
        },
        program=ProgramGraph(
            entry="review",
            states={
                "review": decision,
                "accepted_action": State(
                    id="accepted_action",
                    kind=StateKind.ACTION,
                    step=Step(
                        id="accepted_action",
                        intent="perform accepted path",
                        action=ActionKind.KEY,
                        key="A",
                    ),
                    transitions=[Transition(target="done")],
                ),
                "rejected_action": State(
                    id="rejected_action",
                    kind=StateKind.ACTION,
                    step=Step(
                        id="rejected_action",
                        intent="perform rejected path",
                        action=ActionKind.KEY,
                        key="R",
                    ),
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


def _decision_program_with_predecessor() -> ProgramGraph:
    workflow = _decision_workflow()
    assert workflow.program is not None
    program = workflow.program.model_copy(deep=True)
    program.states["prepare"] = State(
        id="prepare",
        kind=StateKind.ACTION,
        step=Step(
            id="prepare",
            intent="prepare the reviewed decision",
            action=ActionKind.WAIT,
        ),
        transitions=[Transition(target="review")],
    )
    program.entry = "prepare"
    return program


def _principal(*roles: str) -> BusinessDecisionPrincipal:
    return BusinessDecisionPrincipal(
        operator_ref="operator:alice",
        roles=roles,
        authenticated_by="test-aal2-route",
        authentication_context_sha256="a" * 64,
    )


@pytest.mark.parametrize(
    "principal_update",
    [
        {"operator_ref": "   "},
        {"operator_ref": " operator:alice "},
        {"authenticated_by": "   "},
        {"authenticated_by": " test-aal2-route "},
    ],
)
def test_business_decision_principal_requires_exact_attribution(principal_update):
    payload = _principal("operator").model_dump(mode="json")
    payload.update(principal_update)
    with pytest.raises(ValueError, match="principal attribution"):
        BusinessDecisionPrincipal.model_validate(payload)


def _pause(
    tmp_path,
    *,
    required_evidence: bool = True,
) -> tuple[Workflow, BusinessDecisionStore, FakeBackend, object]:
    workflow = _decision_workflow(required_evidence=required_evidence)
    bundle_dir = tmp_path / "bundle"
    run_dir = tmp_path / "run"
    workflow.save(bundle_dir)
    backend = FakeBackend()
    report = Replayer(
        backend,
        vision=FakeVision(),
        durable=True,
        poll_interval_s=0.0,
    ).run(workflow, bundle_dir=bundle_dir, run_dir=run_dir)
    assert report.success is False
    assert backend.actions == []
    store = BusinessDecisionStore(run_dir)
    pending = CheckpointStore(run_dir).read_pending()
    assert pending is not None
    assert pending.category == "business_decision"
    assert not (run_dir / "attended_capability.json").exists()
    request, _ = store.read_active_request()
    return workflow, store, backend, request


def _submission(store: BusinessDecisionStore, request, option_id: str = "accept"):
    evidence = {}
    option = next(item for item in request.decision.options if item.id == option_id)
    if option.required_evidence:
        evidence[option.required_evidence[0]] = store.retain_evidence(
            b"retained application evidence"
        )
    return BusinessDecisionSubmission(
        request_digest=request.digest,
        idempotency_key=f"answer-{option_id}-0001",
        option_id=option_id,
        evidence_artifact_sha256s=evidence,
    )


def test_business_decision_contract_is_closed_and_legacy_state_stays_compatible():
    with pytest.raises(ValueError, match="option values must be unique"):
        BusinessDecisionSpec(
            question="Choose",
            authorized_roles=("operator",),
            output_param="choice",
            options=(
                BusinessDecisionOption(id="one", label="One", value="same", target="a"),
                BusinessDecisionOption(id="two", label="Two", value="same", target="b"),
            ),
            revalidation=(
                Predicate(
                    kind=PredicateKind.PARAM_EQUALS,
                    param="ready",
                    value="yes",
                ),
            ),
        )

    legacy = State(id="done", kind=StateKind.TERMINAL, outcome="success")
    assert "decision" not in legacy.model_dump(mode="json")


@pytest.mark.parametrize(
    "contract_update",
    [
        {"question": "   "},
        {
            "options": (
                BusinessDecisionOption(
                    id="one", label="Approve", value="one", target="a"
                ),
                BusinessDecisionOption(
                    id="two", label=" approve ", value="two", target="b"
                ),
            )
        },
        {
            "options": (
                BusinessDecisionOption(
                    id="one", label="Approve", value=" ", target="a"
                ),
                BusinessDecisionOption(
                    id="two", label="Reject", value="two", target="b"
                ),
            )
        },
    ],
)
def test_decision_contract_refuses_blank_or_indistinguishable_choices(
    contract_update,
):
    payload = _decision_spec(required_evidence=False).model_dump(mode="json")
    payload.update(contract_update)
    with pytest.raises(ValueError):
        BusinessDecisionSpec.model_validate(payload)


def test_decision_contract_refuses_role_that_cannot_fit_signed_receipt():
    payload = _decision_spec(required_evidence=False).model_dump(mode="json")
    payload["authorized_roles"] = ["r" * 129]
    with pytest.raises(ValueError, match="at most 128"):
        BusinessDecisionSpec.model_validate(payload)


@pytest.mark.parametrize(
    "weak_revalidation",
    [
        Predicate(
            kind=PredicateKind.PARAM_EQUALS,
            param="ready",
            value="yes",
        ),
        Predicate(
            kind=PredicateKind.PARAM_EQUALS,
            param="review_outcome",
            value="accepted",
        ),
        Predicate(
            kind=PredicateKind.OR,
            operands=[
                Predicate(kind=PredicateKind.TEXT_PRESENT, text="Ready"),
                Predicate(kind=PredicateKind.TEXT_ABSENT, text="Ready"),
            ],
        ),
    ],
)
def test_decision_contract_refuses_non_affirmative_live_revalidation(
    weak_revalidation,
):
    payload = _decision_spec().model_dump(mode="json")
    payload["revalidation"] = [weak_revalidation.model_dump(mode="json")]
    with pytest.raises(ValueError, match="affirmative live frame predicate"):
        BusinessDecisionSpec.model_validate(payload)


def test_bundle_requires_declared_output_and_exact_option_branch_mapping():
    workflow = _decision_workflow()
    report = validate_workflow(workflow)
    assert report.ok

    assert workflow.program is not None
    workflow.param_specs.clear()
    workflow.program.states["review"].transitions.reverse()
    issue_codes = {issue.code for issue in validate_workflow(workflow).issues}
    assert "business_decision_undeclared_output" in issue_codes
    assert "business_decision_transition_mismatch" in issue_codes

    mismatched = _decision_workflow()
    mismatched.param_specs["review_outcome"] = ParamSpec(
        name="review_outcome",
        type=ParamKind.ENUM,
        required=False,
        choices=["accepted", "different"],
    )
    mismatch_codes = {issue.code for issue in validate_workflow(mismatched).issues}
    assert "business_decision_output_choices_mismatch" in mismatch_codes


def test_bundle_rejects_decision_ids_that_cannot_fit_runtime_request():
    workflow = _decision_workflow()
    assert workflow.program is not None
    long_id = "d" * 129
    decision = workflow.program.states.pop("review")
    decision.id = long_id
    workflow.program.states[long_id] = decision
    workflow.program.entry = long_id

    issue_codes = {issue.code for issue in validate_workflow(workflow).issues}

    assert "business_decision_state_id_too_long" in issue_codes


def test_bundle_rejects_subflow_id_that_cannot_fit_decision_scope():
    workflow = _decision_workflow()
    assert workflow.program is not None
    long_name = "g" * 129
    workflow.subflows[long_name] = workflow.program

    issue_codes = {issue.code for issue in validate_workflow(workflow).issues}

    assert "business_decision_graph_id_too_long" in issue_codes


def test_learned_repair_cannot_change_certified_decision_contract():
    workflow = _decision_workflow()
    assert workflow.program is not None
    candidate = workflow.program.model_copy(deep=True)
    decision = candidate.states["review"].decision
    assert decision is not None
    candidate.states["review"].decision = decision.model_copy(
        update={"authorized_roles": ("administrator",)}
    )

    report = program_regression_gate(workflow.program, candidate)
    assert report.passed is False
    assert any(
        "business decision contract changed" in failure
        for failure in report.semantic_failures
    )


def test_learned_repair_cannot_replace_decision_entry_with_answer_branch():
    active = _decision_program_with_predecessor()
    candidate = active.model_copy(deep=True)
    candidate.entry = "accepted_action"

    report = program_regression_gate(active, candidate)
    assert report.passed is False
    assert any(
        "certified decision is no longer reachable" in failure
        for failure in report.semantic_failures
    )


@pytest.mark.parametrize(
    ("bypass_target", "failure_fragment"),
    [
        ("accepted_action", "incoming path can bypass the signed answer"),
        ("done", "protected state"),
    ],
)
def test_learned_repair_cannot_add_bypass_edge_into_decision_region(
    bypass_target,
    failure_fragment,
):
    active = _decision_program_with_predecessor()
    candidate = active.model_copy(deep=True)
    candidate.states["prepare"].transitions.append(
        Transition(
            target=bypass_target,
            guard=Predicate(
                kind=PredicateKind.PARAM_EQUALS,
                param="unsafe_bypass",
                value="yes",
            ),
        )
    )

    report = program_regression_gate(active, candidate)
    assert report.passed is False
    assert any(failure_fragment in failure for failure in report.semantic_failures)


def test_learned_repair_cannot_rewrite_decision_predecessor_to_answer_branch():
    active = _decision_program_with_predecessor()
    candidate = active.model_copy(deep=True)
    candidate.states["prepare"].transitions = [Transition(target="rejected_action")]

    report = program_regression_gate(active, candidate)
    assert report.passed is False
    assert any(
        "certified decision is no longer reachable" in failure
        for failure in report.semantic_failures
    )


def test_learned_repair_cannot_rewrite_signed_option_transition():
    active = _decision_program_with_predecessor()
    candidate = active.model_copy(deep=True)
    candidate.states["review"].transitions[0].target = "rejected_action"

    report = program_regression_gate(active, candidate)
    assert report.passed is False
    assert any(
        "signed option-to-branch mapping changed" in failure
        for failure in report.semantic_failures
    )


def test_learned_repair_cannot_add_success_exit_before_required_decision():
    active = _decision_program_with_predecessor()
    candidate = active.model_copy(deep=True)
    candidate.states["shortcut_done"] = State(
        id="shortcut_done",
        kind=StateKind.TERMINAL,
        outcome="success",
    )
    candidate.states["prepare"].transitions.append(
        Transition(
            target="shortcut_done",
            guard=Predicate(
                kind=PredicateKind.PARAM_EQUALS,
                param="unsafe_shortcut",
                value="yes",
            ),
        )
    )

    report = program_regression_gate(active, candidate)
    assert report.passed is False
    assert any(
        "successful terminal" in failure and "without the signed decision" in failure
        for failure in report.semantic_failures
    )


def test_learned_repair_cannot_invent_new_business_decision_policy():
    candidate = _decision_program_with_predecessor()
    active = candidate.model_copy(deep=True)
    active.entry = "prepare"
    active.states["prepare"].transitions = [Transition(target="accepted_action")]
    del active.states["review"]

    report = program_regression_gate(active, candidate)

    assert report.passed is False
    assert any(
        "learned repair cannot invent new normative human authority" in failure
        for failure in report.semantic_failures
    )


def test_learned_repair_cannot_bypass_decision_inside_called_subflow():
    workflow = _decision_workflow()
    assert workflow.program is not None
    main = ProgramGraph(
        entry="prepare",
        states={
            "prepare": State(
                id="prepare",
                kind=StateKind.ACTION,
                step=Step(
                    id="prepare",
                    intent="prepare called review",
                    action=ActionKind.WAIT,
                ),
                transitions=[Transition(target="call_review")],
            ),
            "call_review": State(
                id="call_review",
                kind=StateKind.SUBFLOW_CALL,
                subflow="review_flow",
                transitions=[Transition(target="commit")],
            ),
            "commit": State(
                id="commit",
                kind=StateKind.ACTION,
                step=Step(
                    id="commit",
                    intent="commit selected path",
                    action=ActionKind.KEY,
                    key="ENTER",
                ),
                transitions=[Transition(target="done")],
            ),
            "done": State(id="done", kind=StateKind.TERMINAL, outcome="success"),
        },
    )
    candidate = main.model_copy(deep=True)
    candidate.states["prepare"].transitions.append(
        Transition(
            target="commit",
            guard=Predicate(
                kind=PredicateKind.PARAM_EQUALS,
                param="bypass",
                value="yes",
            ),
        )
    )

    report = program_regression_gate(
        main,
        candidate,
        active_subflows={"review_flow": workflow.program},
        candidate_subflows={"review_flow": workflow.program.model_copy(deep=True)},
    )

    assert report.passed is False
    assert any("protected state" in failure for failure in report.semantic_failures)


def test_learned_repair_cannot_change_action_authorized_by_decision():
    active = _decision_program_with_predecessor()
    candidate = active.model_copy(deep=True)
    step = candidate.states["accepted_action"].step
    assert step is not None
    candidate.states["accepted_action"].step = step.model_copy(update={"key": "DELETE"})

    report = program_regression_gate(active, candidate)

    assert report.passed is False
    assert any(
        "business decision semantics changed" in failure
        for failure in report.semantic_failures
    )


def test_learned_repair_cannot_replace_authorized_action_payload():
    active = _decision_program_with_predecessor()
    candidate = active.model_copy(deep=True)
    step = candidate.states["accepted_action"].step
    assert step is not None
    candidate.states["accepted_action"].step = step.model_copy(
        update={"action": ActionKind.TYPE, "key": None, "text": "changed payload"}
    )

    report = program_regression_gate(active, candidate)

    assert report.passed is False
    assert any(
        "business decision semantics changed" in failure
        for failure in report.semantic_failures
    )


def test_learned_repair_cannot_reroute_authorized_branch_after_action():
    active = _decision_program_with_predecessor()
    candidate = active.model_copy(deep=True)
    candidate.states["accepted_action"].transitions = [
        Transition(target="rejected_action")
    ]

    report = program_regression_gate(active, candidate)

    assert report.passed is False
    assert any(
        "business decision semantics changed" in failure
        for failure in report.semantic_failures
    )


def test_learned_repair_cannot_change_drag_destination_identity():
    active = _decision_program_with_predecessor()
    source = Anchor(
        template="templates/source.png",
        region=(10, 10, 20, 20),
        click_point=(20, 20),
        ocr_text="Source record",
        context_text="Source record",
    )
    destination = Anchor(
        template="templates/approved.png",
        region=(100, 10, 20, 20),
        click_point=(110, 20),
        ocr_text="Approved queue",
        context_text="Approved queue",
    )
    old_step = active.states["accepted_action"].step
    assert old_step is not None
    active.states["accepted_action"].step = old_step.model_copy(
        update={
            "action": ActionKind.DRAG,
            "key": None,
            "anchor": source,
            "drag_end_anchor": destination,
        }
    )
    candidate = active.model_copy(deep=True)
    candidate_step = candidate.states["accepted_action"].step
    assert candidate_step is not None
    assert candidate_step.drag_end_anchor is not None
    candidate.states["accepted_action"].step = candidate_step.model_copy(
        update={
            "drag_end_anchor": candidate_step.drag_end_anchor.model_copy(
                update={
                    "ocr_text": "Delete area",
                    "context_text": "Delete area",
                }
            )
        }
    )

    report = program_regression_gate(active, candidate)

    assert report.passed is False
    assert any("drag destination" in failure for failure in report.failures)


def test_learned_repair_can_relocate_same_drag_destination():
    active = _decision_program_with_predecessor()
    source = Anchor(
        template="templates/source.png",
        region=(10, 10, 20, 20),
        click_point=(20, 20),
        context_text="Source record",
    )
    destination = Anchor(
        template="templates/approved.png",
        region=(100, 10, 20, 20),
        click_point=(110, 20),
        context_text="Approved queue",
    )
    old_step = active.states["accepted_action"].step
    assert old_step is not None
    active.states["accepted_action"].step = old_step.model_copy(
        update={
            "action": ActionKind.DRAG,
            "key": None,
            "anchor": source,
            "drag_end_anchor": destination,
        }
    )
    candidate = active.model_copy(deep=True)
    candidate_step = candidate.states["accepted_action"].step
    assert candidate_step is not None
    assert candidate_step.drag_end_anchor is not None
    candidate.states["accepted_action"].step = candidate_step.model_copy(
        update={
            "drag_end_anchor": candidate_step.drag_end_anchor.model_copy(
                update={
                    "template": "templates/approved-v2.png",
                    "region": (140, 30, 20, 20),
                    "click_point": (150, 40),
                }
            )
        }
    )

    report = program_regression_gate(active, candidate)

    assert report.passed is True


def test_decision_refuses_wrong_role_and_non_exact_evidence(tmp_path):
    _workflow, store, _backend, request = _pause(tmp_path)
    submission = _submission(store, request)

    with pytest.raises(BusinessDecisionRefused, match="SHA-256 is invalid"):
        store.authenticate_request("../../outside-the-run")

    with pytest.raises(BusinessDecisionRefused, match="no authorized decision role"):
        store.submit(submission, principal=_principal("viewer"))

    missing = submission.model_copy(update={"evidence_artifact_sha256s": {}})
    with pytest.raises(BusinessDecisionRefused, match="exact required evidence set"):
        store.submit(missing, principal=_principal("operator"))

    unknown = submission.model_copy(
        update={
            "evidence_artifact_sha256s": {
                **submission.evidence_artifact_sha256s,
                "extra": "b" * 64,
            }
        }
    )
    with pytest.raises(BusinessDecisionRefused, match="exact required evidence set"):
        store.submit(unknown, principal=_principal("operator"))


def test_decision_refuses_expiry_and_missing_local_evidence(tmp_path):
    _workflow, store, _backend, request = _pause(tmp_path)
    submission = _submission(store, request)
    expired_at = datetime.fromisoformat(request.expires_at) + timedelta(seconds=1)
    with pytest.raises(BusinessDecisionRefused, match="expired"):
        store.submit(
            submission,
            principal=_principal("operator"),
            now=expired_at,
        )

    digest = next(iter(submission.evidence_artifact_sha256s.values()))
    (tmp_path / "run" / ".business_decisions" / "evidence" / f"{digest}.bin").unlink()
    with pytest.raises(BusinessDecisionRefused, match="unavailable locally"):
        store.submit(submission, principal=_principal("operator"))


def test_expired_unanswered_request_is_renewed_for_same_durable_pause(tmp_path):
    workflow, store, _backend, request = _pause(tmp_path)
    checkpoint_store = CheckpointStore(tmp_path / "run")
    pending = checkpoint_store.read_pending()
    manifest = checkpoint_store.read_manifest()
    assert pending is not None
    assert manifest is not None
    assert workflow.program is not None
    spec = workflow.program.states["review"].decision
    assert spec is not None
    old_request_path = store._request_path(store.read_active_request()[1])
    renewed_at = datetime.fromisoformat(request.expires_at) + timedelta(seconds=1)

    renewed, renewed_sha256 = store.issue(
        pending=pending,
        manifest=manifest,
        workflow=workflow,
        graph_id="__program__",
        state_id="review",
        frames=list(pending.program_frames),
        params=dict(pending.params),
        spec=spec,
        governed_runtime_inputs_digest=None,
        now=renewed_at,
    )

    assert renewed.digest != request.digest
    assert renewed.supersedes_request_digest == request.digest
    assert renewed.supersedes_request_sha256 == old_request_path.stem
    assert old_request_path.is_file()
    assert store.authenticate_request(old_request_path.stem) == request
    assert store.read_active_request() == (renewed, renewed_sha256)

    with pytest.raises(BusinessDecisionRefused, match="another decision request"):
        store.submit(
            _submission(store, request),
            principal=_principal("operator"),
            now=renewed_at + timedelta(seconds=1),
        )

    receipt = store.submit(
        _submission(store, renewed),
        principal=_principal("operator"),
        now=renewed_at + timedelta(seconds=1),
    )
    assert receipt.request_digest == renewed.digest

    old_request_path.unlink()
    with pytest.raises(BusinessDecisionRefused, match="missing or invalid"):
        store.authenticate_request(renewed_sha256)


def test_answer_expires_before_delayed_resume(tmp_path):
    workflow, store, _backend, request = _pause(tmp_path)
    submission = _submission(store, request)
    issued_at = datetime.fromisoformat(request.issued_at)
    store.submit(
        submission,
        principal=_principal("operator"),
        now=issued_at + timedelta(seconds=1),
    )
    checkpoint_store = CheckpointStore(tmp_path / "run")
    pending = checkpoint_store.read_pending()
    manifest = checkpoint_store.read_manifest()
    assert pending is not None
    assert manifest is not None
    assert workflow.program is not None
    spec = workflow.program.states["review"].decision
    assert spec is not None

    with pytest.raises(BusinessDecisionRefused, match="expired before resume"):
        store.consume(
            pending=pending,
            manifest=manifest,
            workflow=workflow,
            graph_id="__program__",
            state_id="review",
            frames=list(pending.program_frames),
            params=dict(pending.params),
            spec=spec,
            governed_runtime_inputs_digest=None,
            now=datetime.fromisoformat(request.expires_at) + timedelta(seconds=1),
        )


def test_decision_submission_is_idempotent_and_conflicts_fail_closed(tmp_path):
    _workflow, store, _backend, request = _pause(tmp_path)
    submission = _submission(store, request)
    receipt = store.submit(submission, principal=_principal("operator"))
    retried = store.submit(submission, principal=_principal("operator"))
    assert retried == receipt

    conflicting = submission.model_copy(update={"option_id": "reject"})
    with pytest.raises(BusinessDecisionRefused, match="idempotency key"):
        store.submit(conflicting, principal=_principal("operator"))


def test_submission_recovers_after_answer_pointer_write(tmp_path, monkeypatch):
    _workflow, store, _backend, request = _pause(tmp_path)
    submission = _submission(store, request)
    original_write = store._atomic_write

    def interrupt_before_idempotency(path, payload):
        if path.parent == store.idempotency_dir:
            raise RuntimeError("simulated process interruption")
        original_write(path, payload)

    monkeypatch.setattr(store, "_atomic_write", interrupt_before_idempotency)
    with pytest.raises(RuntimeError, match="simulated process interruption"):
        store.submit(submission, principal=_principal("operator"))

    monkeypatch.setattr(store, "_atomic_write", original_write)
    receipt = store.submit(submission, principal=_principal("operator"))
    assert receipt.option_id == "accept"
    assert store.read_receipt(request.digest) is not None


def test_signed_receipt_prevents_conflicting_answer_after_pointer_write_crash(
    tmp_path, monkeypatch
):
    _workflow, store, _backend, request = _pause(tmp_path)
    accepted = _submission(store, request, "accept")
    original_write = store._atomic_write

    def interrupt_before_answer_pointer(path, payload):
        if path.parent == store.answers_dir:
            raise RuntimeError("simulated process termination before pointer")
        original_write(path, payload)

    monkeypatch.setattr(store, "_atomic_write", interrupt_before_answer_pointer)
    with pytest.raises(RuntimeError, match="before pointer"):
        store.submit(accepted, principal=_principal("operator"))

    assert len(list(store.receipts_dir.glob("*.json"))) == 1
    assert not store._answer_path(request.digest).exists()
    monkeypatch.setattr(store, "_atomic_write", original_write)

    rejected = _submission(store, request, "reject")
    with pytest.raises(BusinessDecisionRefused, match="different answer"):
        store.submit(rejected, principal=_principal("operator"))
    assert len(list(store.receipts_dir.glob("*.json"))) == 1
    assert not store._answer_path(request.digest).exists()

    recovered = store.submit(accepted, principal=_principal("operator"))
    assert recovered.option_id == "accept"
    assert len(list(store.receipts_dir.glob("*.json"))) == 1
    assert store.read_receipt(request.digest) is not None


def test_submission_recovers_after_receipt_before_approval(tmp_path, monkeypatch):
    _workflow, store, _backend, request = _pause(tmp_path)
    submission = _submission(store, request)
    original_commit = CheckpointStore.commit_approval_transition

    def interrupt_before_approval(self, **kwargs):
        raise RuntimeError("simulated process interruption")

    monkeypatch.setattr(
        CheckpointStore,
        "commit_approval_transition",
        interrupt_before_approval,
    )
    with pytest.raises(RuntimeError, match="simulated process interruption"):
        store.submit(submission, principal=_principal("operator"))

    monkeypatch.setattr(
        CheckpointStore,
        "commit_approval_transition",
        original_commit,
    )
    receipt = store.submit(submission, principal=_principal("operator"))
    assert receipt.option_id == "accept"
    pending = CheckpointStore(tmp_path / "run").read_pending()
    assert pending is not None
    assert pending.status == "approved"


def test_submission_reuses_stale_lock_file_from_interrupted_legacy_writer(tmp_path):
    _workflow, store, _backend, request = _pause(tmp_path)
    store.lock_path.write_bytes(b"")

    receipt = store.submit(
        _submission(store, request),
        principal=_principal("operator"),
    )

    assert receipt.option_id == "accept"
    assert store.lock_path.is_file()


@pytest.mark.skipif(
    os.name == "nt" or not hasattr(signal, "SIGKILL"),
    reason="SIGKILL and POSIX flock are required for this restart test",
)
def test_submission_recovers_advisory_lock_after_sigkill(tmp_path):
    _workflow, store, _backend, request = _pause(tmp_path)
    store.lock_path.parent.mkdir(parents=True, exist_ok=True)
    child_code = """
import fcntl
import os
import sys
import time

descriptor = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(descriptor, fcntl.LOCK_EX)
print("locked", flush=True)
while True:
    time.sleep(60)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", child_code, str(store.lock_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(BusinessDecisionRefused, match="submission is active"):
            with store._lock(timeout_s=0.05):
                pass

        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=5)

        receipt = store.submit(
            _submission(store, request),
            principal=_principal("operator"),
        )
        assert receipt.option_id == "accept"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_signed_decision_selects_one_branch_then_normal_action_gates_run(tmp_path):
    workflow, store, _initial_backend, request = _pause(tmp_path)
    submission = _submission(store, request, "accept")
    receipt = store.submit(submission, principal=_principal("operator"))

    resumed_backend = FakeBackend()
    resumed_vision = FakeVision()
    resumed_vision.text_results["Ready for reviewed action"] = (1, 1, 10, 10)
    report = resume(
        tmp_path / "run",
        Replayer(
            resumed_backend,
            vision=resumed_vision,
            poll_interval_s=0.0,
        ),
    )

    assert report.success is True
    assert resumed_backend.actions == [("press", "A")]
    assert report.params["review_outcome"] == "accepted"
    assert len(report.business_decision_evidence) == 1
    evidence = report.business_decision_evidence[0]
    assert evidence.receipt_digest == receipt.digest
    assert evidence.target_state_id == "accepted_action"
    assert "identity" not in type(evidence).model_fields
    assert "effect_verified" not in type(evidence).model_fields
    assert report.model_calls == 0
    assert workflow.program is not None
    trace = _program_action_trace(
        workflow,
        report.visited_states,
        runtime_params=report.params,
        transition_evidence=report.program_transition_evidence,
        business_decision_evidence=report.business_decision_evidence,
        transition_evidence_root=tmp_path / "run",
        transition_predicate_vision=resumed_vision,
        governed_runtime_inputs_digest=report.governed_runtime_inputs_digest,
        run_id_sha256=report.run_id_sha256,
        workflow_contract_digest=report.workflow_contract_sha256,
        reported_results=report.results,
    )
    assert trace is not None
    assert [item.state_id for item in trace] == ["accepted_action"]

    reported_params = dict(report.params)
    report.params["review_outcome"] = "rejected"
    assert (
        _program_action_trace(
            workflow,
            report.visited_states,
            runtime_params=report.params,
            transition_evidence=report.program_transition_evidence,
            business_decision_evidence=report.business_decision_evidence,
            transition_evidence_root=tmp_path / "run",
            transition_predicate_vision=resumed_vision,
            governed_runtime_inputs_digest=report.governed_runtime_inputs_digest,
            run_id_sha256=report.run_id_sha256,
            workflow_contract_digest=report.workflow_contract_sha256,
            reported_results=report.results,
        )
        is None
    )
    report.params = reported_params

    forged = report.business_decision_evidence[0].model_copy(
        update={"target_state_id": "rejected_action"}
    )
    assert (
        _program_action_trace(
            workflow,
            report.visited_states,
            runtime_params=report.params,
            transition_evidence=report.program_transition_evidence,
            business_decision_evidence=[forged],
            transition_evidence_root=tmp_path / "run",
            transition_predicate_vision=resumed_vision,
            governed_runtime_inputs_digest=report.governed_runtime_inputs_digest,
            run_id_sha256=report.run_id_sha256,
            workflow_contract_digest=report.workflow_contract_sha256,
            reported_results=report.results,
        )
        is None
    )


def test_live_revalidation_change_halts_before_selected_action(tmp_path):
    _workflow, store, _initial_backend, request = _pause(tmp_path)
    store.submit(_submission(store, request), principal=_principal("operator"))

    resumed_backend = FakeBackend()
    report = resume(
        tmp_path / "run",
        Replayer(
            resumed_backend,
            vision=FakeVision(),
            poll_interval_s=0.0,
        ),
    )

    assert report.success is False
    assert resumed_backend.actions == []
    assert report.results[-1].safety_halt is True
    assert "no longer satisfies" in (report.results[-1].error or "")
    assert report.business_decision_evidence == []
    assert report.program_transition_evidence == []

    replacement, _ = store.read_active_request()
    assert replacement.digest != request.digest
    replacement_submission = _submission(store, replacement).model_copy(
        update={"idempotency_key": "answer-accept-replacement-0002"}
    )
    store.submit(
        replacement_submission,
        principal=_principal("operator"),
    )
    corrected_backend = FakeBackend()
    corrected_vision = FakeVision()
    corrected_vision.text_results["Ready for reviewed action"] = (1, 1, 10, 10)
    corrected = resume(
        tmp_path / "run",
        Replayer(
            corrected_backend,
            vision=corrected_vision,
            poll_interval_s=0.0,
        ),
    )

    assert corrected.success is True
    assert corrected_backend.actions == [("press", "A")]
    assert len(corrected.business_decision_evidence) == 1
    assert corrected.business_decision_evidence[0].request_digest == replacement.digest


def test_obsolete_operational_capability_is_archived_not_exposed(tmp_path):
    workflow, _store, _backend, _request = _pause(tmp_path)
    run_dir = tmp_path / "run"
    checkpoint_store = CheckpointStore(run_dir)
    pending = checkpoint_store.read_pending()
    assert pending is not None
    result = StepResult(
        step_id="review",
        intent="obsolete operational pause",
        ok=False,
        error="obsolete operational halt",
    )
    first = issue_attended_capability(
        run_dir,
        store=checkpoint_store,
        pending=pending,
        workflow=workflow,
        result=result,
    )

    from openadapt_flow.runtime.durable.attended import AttendedActionStore

    actions = AttendedActionStore(run_dir)
    actions.retire_current()
    assert not (run_dir / "attended_capability.json").exists()
    assert (run_dir / "attended_capability_history.json").is_file()

    second = issue_attended_capability(
        run_dir,
        store=checkpoint_store,
        pending=pending,
        workflow=workflow,
        result=result,
    )
    assert second.event_sequence == first.event_sequence + 1


def test_tampered_retained_evidence_halts_before_selected_action(tmp_path):
    _workflow, store, _initial_backend, request = _pause(tmp_path)
    submission = _submission(store, request)
    store.submit(submission, principal=_principal("operator"))
    digest = next(iter(submission.evidence_artifact_sha256s.values()))
    (
        tmp_path / "run" / ".business_decisions" / "evidence" / f"{digest}.bin"
    ).write_bytes(b"changed")

    resumed_backend = FakeBackend()
    resumed_vision = FakeVision()
    resumed_vision.text_results["Ready for reviewed action"] = (1, 1, 10, 10)
    report = resume(
        tmp_path / "run",
        Replayer(
            resumed_backend,
            vision=resumed_vision,
            poll_interval_s=0.0,
        ),
    )

    assert report.success is False
    assert resumed_backend.actions == []
    assert "evidence content hash differs" in (report.results[-1].error or "")


def test_multiple_decisions_survive_checkpoint_and_resume_in_order(tmp_path):
    workflow = _decision_workflow(required_evidence=False)
    assert workflow.program is not None
    workflow.program.states["accepted_action"].transitions = [
        Transition(target="second_review")
    ]
    second_spec = BusinessDecisionSpec(
        question="Which second declared path should continue?",
        authorized_roles=("supervisor",),
        output_param="second_outcome",
        options=(
            BusinessDecisionOption(
                id="continue",
                label="Continue",
                value="continued",
                target="second_action",
            ),
            BusinessDecisionOption(
                id="stop",
                label="Stop",
                value="stopped",
                target="done",
            ),
        ),
        revalidation=(
            Predicate(
                kind=PredicateKind.TEXT_PRESENT,
                text="Ready for second reviewed action",
            ),
        ),
    )
    workflow.param_specs["second_outcome"] = ParamSpec(
        name="second_outcome",
        type=ParamKind.ENUM,
        required=False,
        choices=["continued", "stopped"],
    )
    workflow.program.states["second_review"] = State(
        id="second_review",
        kind=StateKind.BUSINESS_DECISION,
        decision=second_spec,
        transitions=business_decision_transitions(second_spec),
    )
    workflow.program.states["second_action"] = State(
        id="second_action",
        kind=StateKind.ACTION,
        step=Step(
            id="second_action",
            intent="perform second accepted path",
            action=ActionKind.KEY,
            key="B",
        ),
        transitions=[Transition(target="done")],
    )
    assert validate_workflow(workflow).ok
    bundle_dir = tmp_path / "bundle"
    run_dir = tmp_path / "run"
    workflow.save(bundle_dir)

    first = Replayer(FakeBackend(), vision=FakeVision(), durable=True).run(
        workflow,
        bundle_dir=bundle_dir,
        run_dir=run_dir,
    )
    assert first.success is False
    store = BusinessDecisionStore(run_dir)
    first_request, _ = store.read_active_request()
    store.submit(
        BusinessDecisionSubmission(
            request_digest=first_request.digest,
            idempotency_key="first-decision-0001",
            option_id="accept",
        ),
        principal=_principal("operator"),
    )

    first_resume_backend = FakeBackend()
    first_resume_vision = FakeVision()
    first_resume_vision.text_results["Ready for reviewed action"] = (1, 1, 10, 10)
    between = resume(
        run_dir,
        Replayer(first_resume_backend, vision=first_resume_vision),
    )
    assert between.success is False
    assert first_resume_backend.actions == [("press", "A")]

    second_request, _ = store.read_active_request()
    assert second_request.state_id == "second_review"
    store.submit(
        BusinessDecisionSubmission(
            request_digest=second_request.digest,
            idempotency_key="second-decision-0001",
            option_id="continue",
        ),
        principal=_principal("supervisor"),
    )
    second_resume_backend = FakeBackend()
    second_resume_vision = FakeVision()
    second_resume_vision.text_results["Ready for second reviewed action"] = (
        1,
        1,
        10,
        10,
    )
    completed = resume(
        run_dir,
        Replayer(second_resume_backend, vision=second_resume_vision),
    )

    assert completed.success is True
    assert second_resume_backend.actions == [("press", "B")]
    assert [item.state_id for item in completed.business_decision_evidence] == [
        "review",
        "second_review",
    ]

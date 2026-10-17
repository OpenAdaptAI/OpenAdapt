"""Interactive disambiguation: compile-time Socrates-style questions.

Covers the RFC ``docs/design/WORKFLOW_PROGRAM_IR.md`` §3 [3] induction stage:
an ambiguous demo yields the expected grounded multiple-choice questions;
applying answers produces the right Phase-1 IR (``ParamSpec`` / ``Guard``); an
UNANSWERED consequential ambiguity marks the skill NOT certified (refuse rather
than guess); a fully-answered workflow resolves clean.

Pure API tests -- no backend, no vision, no Playwright, ZERO model calls.
"""

from __future__ import annotations

import pytest

from openadapt_flow.compiler.disambiguation import (
    AmbiguityKind,
    OptionEffect,
    apply_answers,
    detect_ambiguities,
)
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    ParamKind,
    PredicateKind,
    Step,
    Workflow,
)


def _anchor(label: str, context: str | None) -> Anchor:
    return Anchor(
        template="templates/x.png",
        region=(0, 0, 10, 10),
        click_point=(5, 5),
        ocr_text=label,
        context_text=context,
    )


def _select_step(step_id: str, label: str, context: str) -> Step:
    """An identity-armed entity selection (a searched patient row)."""
    return Step(
        id=step_id,
        intent=f"click '{label}'",
        action=ActionKind.CLICK,
        anchor=_anchor(label, context),
        identity_armed=True,
    )


def ambiguous_workflow(*, with_irreversible: bool = True) -> Workflow:
    """A demo exercising all three ambiguity kinds.

    search (type) -> select patient (identity-armed click) -> type an untagged
    note -> dismiss a survey dialog -> Save (irreversible).
    """
    steps = [
        Step(
            id="step_000",
            intent="type 'Belford'",
            action=ActionKind.TYPE,
            text="Belford",  # a search query (untagged typed value too)
        ),
        _select_step("step_001", "Belford, Phil", "Belford, Phil 1980-02-03"),
        Step(
            id="step_002",
            intent="type 'Follow-up in 2 weeks'",
            action=ActionKind.TYPE,
            text="Follow-up in 2 weeks",  # untagged -> parameter candidate
        ),
        Step(
            id="step_003",
            intent="click 'Dismiss survey'",
            action=ActionKind.CLICK,
            anchor=_anchor("Dismiss survey", "We value your feedback"),
            identity_armed=False,
        ),
    ]
    if with_irreversible:
        steps.append(
            Step(
                id="step_004",
                intent="click 'Save Encounter'",
                action=ActionKind.CLICK,
                anchor=_anchor("Save Encounter", None),
                risk="irreversible",
            )
        )
    return Workflow(name="add-patient-note", steps=steps)


# -- detection ----------------------------------------------------------------


def test_detects_expected_question_set():
    questions = detect_ambiguities(ambiguous_workflow())
    kinds = {(q.kind, q.step_id) for q in questions}
    # Two untagged typed values -> two parameter candidates (search + note),
    # one absent-result on the identity-armed selection, one optional dialog.
    assert (AmbiguityKind.PARAMETER_CANDIDATE, "step_000") in kinds
    assert (AmbiguityKind.PARAMETER_CANDIDATE, "step_002") in kinds
    assert (AmbiguityKind.ABSENT_RESULT, "step_001") in kinds
    assert (AmbiguityKind.OPTIONAL_DIALOG, "step_003") in kinds
    assert len(questions) == 4


def test_all_questions_consequential_when_downstream_irreversible():
    for q in detect_ambiguities(ambiguous_workflow()):
        assert q.consequential, q.id


def test_questions_not_consequential_without_irreversible_step():
    wf = ambiguous_workflow(with_irreversible=False)
    for q in detect_ambiguities(wf):
        assert not q.consequential, q.id


def test_dialog_click_is_not_also_an_absent_result():
    # The survey-dismiss click is identity_armed=False, so it must NOT be
    # mistaken for an entity selection.
    questions = detect_ambiguities(ambiguous_workflow())
    absent = [q for q in questions if q.kind is AmbiguityKind.ABSENT_RESULT]
    assert [q.step_id for q in absent] == ["step_001"]


def test_no_questions_for_fully_specified_workflow():
    wf = Workflow(
        name="clean",
        params={"note": "hi"},
        steps=[
            Step(
                id="s0",
                intent="type <note>",
                action=ActionKind.TYPE,
                text="hi",
                param="note",
            ),
        ],
    )
    assert detect_ambiguities(wf) == []


# -- applying answers ---------------------------------------------------------


def test_parameter_answer_creates_paramspec_and_binding():
    wf = ambiguous_workflow()
    qs = detect_ambiguities(wf)
    note_q = next(q for q in qs if q.step_id == "step_002")
    result = apply_answers(wf, {note_q.id: "param"})

    name = note_q.param_name
    assert name in result.workflow.param_specs
    spec = result.workflow.param_specs[name]
    assert spec.type is ParamKind.STRING
    assert spec.example == "Follow-up in 2 weeks"
    assert result.workflow.params[name] == "Follow-up in 2 weeks"
    step = next(s for s in result.workflow.steps if s.id == "step_002")
    assert step.param == name
    assert step.text == "Follow-up in 2 weeks"  # demo value stays as default


def test_parameter_fixed_answer_is_a_noop():
    wf = ambiguous_workflow()
    qs = detect_ambiguities(wf)
    note_q = next(q for q in qs if q.step_id == "step_002")
    result = apply_answers(wf, {note_q.id: "fixed"})
    step = next(s for s in result.workflow.steps if s.id == "step_002")
    assert step.param is None
    assert result.workflow.param_specs == {}


def test_absent_result_answer_installs_halt_guard():
    wf = ambiguous_workflow()
    qs = detect_ambiguities(wf)
    q = next(q for q in qs if q.kind is AmbiguityKind.ABSENT_RESULT)
    result = apply_answers(wf, {q.id: "halt"})
    step = next(s for s in result.workflow.steps if s.id == "step_001")
    assert step.guard is not None
    assert step.guard.on_unmet == "halt"
    assert step.guard.predicate.kind is PredicateKind.ANCHOR_RESOLVES


def test_absent_result_strategy_recorded_as_policy_note():
    wf = ambiguous_workflow()
    qs = detect_ambiguities(wf)
    q = next(q for q in qs if q.kind is AmbiguityKind.ABSENT_RESULT)
    result = apply_answers(wf, {q.id: "compare"})
    step = next(s for s in result.workflow.steps if s.id == "step_001")
    # Phase-1 realization is the safe halt; the chosen run-time strategy is
    # recorded on the predicate intent for a later phase.
    assert step.guard.on_unmet == "halt"
    assert "compare_second_field" in step.guard.predicate.intent


def test_optional_dialog_answer_installs_skip_guard():
    wf = ambiguous_workflow()
    qs = detect_ambiguities(wf)
    q = next(q for q in qs if q.kind is AmbiguityKind.OPTIONAL_DIALOG)
    result = apply_answers(wf, {q.id: "sometimes"})
    step = next(s for s in result.workflow.steps if s.id == "step_003")
    assert step.guard is not None
    assert step.guard.on_unmet == "skip"
    assert step.guard.predicate.kind is PredicateKind.TEXT_PRESENT
    assert step.guard.predicate.text == "Dismiss survey"


def test_optional_dialog_always_answer_is_a_noop():
    wf = ambiguous_workflow()
    qs = detect_ambiguities(wf)
    q = next(q for q in qs if q.kind is AmbiguityKind.OPTIONAL_DIALOG)
    result = apply_answers(wf, {q.id: "always"})
    step = next(s for s in result.workflow.steps if s.id == "step_003")
    assert step.guard is None


# -- refuse rather than guess -------------------------------------------------


def test_unanswered_consequential_ambiguity_is_not_certified():
    wf = ambiguous_workflow()  # every question is consequential
    result = apply_answers(wf, {})  # answer nothing
    assert not result.certified
    assert len(result.unresolved_consequential) == 4
    # Nothing was silently defaulted onto a consequential step.
    assert result.defaulted == []
    # The original steps are untouched (no guessed guard/param).
    assert all(s.guard is None for s in result.workflow.steps)
    assert result.workflow.param_specs == {}


def test_partially_answered_still_not_certified():
    wf = ambiguous_workflow()
    qs = detect_ambiguities(wf)
    absent = next(q for q in qs if q.kind is AmbiguityKind.ABSENT_RESULT)
    result = apply_answers(wf, {absent.id: "halt"})
    assert not result.certified
    assert absent.id in result.applied
    assert len(result.unresolved_consequential) == 3


def test_fully_answered_workflow_certifies_clean():
    wf = ambiguous_workflow()
    qs = detect_ambiguities(wf)
    answers = {}
    for q in qs:
        if q.kind is AmbiguityKind.PARAMETER_CANDIDATE:
            answers[q.id] = "param"
        elif q.kind is AmbiguityKind.ABSENT_RESULT:
            answers[q.id] = "halt"
        else:
            answers[q.id] = "sometimes"
    result = apply_answers(wf, answers)
    assert result.certified
    assert result.unresolved_consequential == []
    assert len(result.applied) == 4


def test_non_consequential_unanswered_falls_back_to_default():
    wf = ambiguous_workflow(with_irreversible=False)
    result = apply_answers(wf, {})
    assert result.certified  # no consequential ambiguity to block
    assert set(result.defaulted) == {q.id for q in result.questions}
    # Param/dialog defaults are no-ops (keep the demo interpretation); the
    # absent-result default is the SAFE halt-guard, not a no-op.
    assert result.workflow.param_specs == {}
    select = next(s for s in result.workflow.steps if s.id == "step_001")
    assert select.guard is not None and select.guard.on_unmet == "halt"
    dialog = next(s for s in result.workflow.steps if s.id == "step_003")
    assert dialog.guard is None


# -- input validation + round-trip -------------------------------------------


def test_unknown_answer_key_raises():
    wf = ambiguous_workflow()
    qs = detect_ambiguities(wf)
    note_q = next(q for q in qs if q.step_id == "step_002")
    with pytest.raises(ValueError, match="unknown answer"):
        apply_answers(wf, {note_q.id: "bogus"})


def test_answer_for_unknown_question_raises():
    wf = ambiguous_workflow()
    with pytest.raises(ValueError, match="unknown question"):
        apply_answers(wf, {"parameter_candidate:does_not_exist": "param"})


def test_apply_does_not_mutate_the_input_workflow():
    wf = ambiguous_workflow()
    qs = detect_ambiguities(wf)
    note_q = next(q for q in qs if q.step_id == "step_002")
    apply_answers(wf, {note_q.id: "param"})
    # The caller's workflow is untouched (apply works on a deep copy).
    assert wf.param_specs == {}
    assert next(s for s in wf.steps if s.id == "step_002").param is None


def test_resolved_workflow_roundtrips_through_bundle(tmp_path):
    wf = ambiguous_workflow()
    qs = detect_ambiguities(wf)
    answers = {}
    for q in qs:
        answers[q.id] = {
            AmbiguityKind.PARAMETER_CANDIDATE: "param",
            AmbiguityKind.ABSENT_RESULT: "halt",
            AmbiguityKind.OPTIONAL_DIALOG: "sometimes",
        }[q.kind]
    result = apply_answers(wf, answers)
    bundle = tmp_path / "bundle"
    result.workflow.save(bundle)
    reloaded = Workflow.load(bundle)

    step1 = next(s for s in reloaded.steps if s.id == "step_001")
    assert step1.guard.predicate.kind is PredicateKind.ANCHOR_RESOLVES
    step3 = next(s for s in reloaded.steps if s.id == "step_003")
    assert step3.guard.on_unmet == "skip"
    assert reloaded.param_specs  # note + search bound as params


def test_option_effects_are_data_driven():
    # Every option's effect is a known enum member (guards apply generically).
    wf = ambiguous_workflow()
    for q in detect_ambiguities(wf):
        for opt in q.options:
            assert isinstance(opt.effect, OptionEffect)


def test_render_reports_certification_verdict():
    wf = ambiguous_workflow()
    assert "NOT CERTIFIED" in apply_answers(wf, {}).render()

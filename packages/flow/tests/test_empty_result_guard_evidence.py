"""A dialog's empty-result statement in any language, mined from evidence.

The vocabulary in ``compiler.empty_result_guard`` is English. A statement in
other words ("Keine Treffer gefunden.") is mined only when two or more
demonstrations with different searches show the same line: record data
changes with the search, the dialog's own words do not. A guard the
demonstrations disagree on is not copied from the first one.

The recordings in ``tests/fixtures/search_dialog`` were made with the real
recorder in Chromium (``record.py`` there) and are read with the real OCR.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow.compiler.empty_result_guard import (
    dialog_statement,
    looks_like_data,
)
from openadapt_flow.compiler.induction import InductionResult, induce_program
from openadapt_flow.ir import PostconditionKind, PredicateKind, Step, Workflow
from openadapt_flow.vision.ocr import OcrLine, ocr

FIXTURES = Path(__file__).parent / "fixtures" / "search_dialog"
GERMAN = "Keine Treffer gefunden."
RECORDINGS = (
    "de_marsh",
    "de_weber",
    "de_marsh_again",
    "archive_marsh",
    "archive_weber",
    "names_marsh",
    "en_weber",
    "comment_marsh_first",
    "comment_marsh_second",
)


@pytest.fixture(scope="module")
def compiled(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, tuple[Workflow, list[dict[str, Any]]]]:
    """Each recording compiled once: its workflow and its compile notes."""

    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.compiler.fixup_passes import read_compile_notes

    root = tmp_path_factory.mktemp("search-dialog")
    out = {}
    for name in RECORDINGS:
        bundle = root / name
        workflow = compile_recording(FIXTURES / name, bundle, name=name)
        out[name] = (workflow, read_compile_notes(bundle))
    return out


def _induce(compiled: dict, *names: str) -> InductionResult:
    return induce_program(
        [compiled[name][0] for name in names],
        compile_notes=[compiled[name][1] for name in names],
    )


def _steps(result: InductionResult) -> tuple[Step, Step]:
    assert result.workflow is not None and result.workflow.program is not None
    states = result.workflow.program.states
    steps = [state.step for state in states.values() if state.step is not None]
    opener = next(s for s in steps if s.intent and "Create" in s.intent)
    confirm = next(s for s in steps if s.intent and "Confirm" in s.intent)
    return opener, confirm


def _exact_texts(step: Step) -> list[str]:
    return [pc.text or "" for pc in step.expect if pc.exact]


def _frames(name: str) -> tuple[list[OcrLine], list[OcrLine], list[OcrLine]]:
    frames = FIXTURES / name / "frames"
    return (
        ocr((frames / "0002_before.png").read_bytes()),
        ocr((frames / "0002_after.png").read_bytes()),
        ocr((frames / "0003_before.png").read_bytes()),
    )


def _confirm_point(name: str) -> tuple[int, int]:
    """Where the recording clicked Confirm (its last event)."""

    lines = (FIXTURES / name / "events.jsonl").read_text().splitlines()
    event = json.loads(lines[-1])
    return int(event["x"]), int(event["y"])


CONFIRM = _confirm_point("de_marsh")


# -- the candidate line, one screen at a time ------------------------------------
def test_the_dialogs_one_line_is_found_in_any_language() -> None:
    line, why = dialog_statement(
        *_frames("de_marsh"), confirm_point=CONFIRM, title="Dublettensuche"
    )
    assert (line, why) == (GERMAN, "")


def test_an_unnamed_dialog_gives_no_line() -> None:
    """Without the dialog's name, its title could be the line."""

    line, why = dialog_statement(
        *_frames("de_marsh"), confirm_point=CONFIRM, title=None
    )
    assert line is None and "title" in why


def test_the_title_and_button_row_are_not_the_line() -> None:
    # Named something else, the title is a second line: nothing is chosen.
    line, why = dialog_statement(
        *_frames("de_marsh"), confirm_point=CONFIRM, title="Another title"
    )
    assert line is None and "2 lines" in why


def test_a_line_with_the_typed_value_is_refused() -> None:
    from openadapt_flow.compiler.compile import _contains_excluded

    line, why = dialog_statement(
        *_frames("names_marsh"),
        confirm_point=CONFIRM,
        title="Dublettensuche",
        reject=lambda text: _contains_excluded(text, ("Elena Marsh",)),
    )
    assert line is None and "run value" in why


def test_numbers_and_addresses_are_data() -> None:
    assert not looks_like_data("Keine Treffer gefunden.")
    assert not looks_like_data("0 Treffer")
    assert looks_like_data("3 Treffer")
    assert looks_like_data("Geboren 1958-03-27")
    assert looks_like_data("Kontakt: a@b.example")


# -- one demonstration ----------------------------------------------------------
def test_one_demonstration_keeps_the_line_only_as_a_candidate(
    compiled: dict,
) -> None:
    workflow, notes = compiled["de_marsh"]
    assert workflow.steps[3].guard is None
    assert not any(pc.exact for step in workflow.steps for pc in step.expect)
    (missed,) = [n for n in notes if n["change"] == "no_empty_result_guard"]
    assert missed["dialog_line"] == GERMAN
    assert missed["step"] == "step_002" and missed["confirm_step"] == "step_003"


def test_one_demonstration_alone_is_not_mined(compiled: dict) -> None:
    result = _induce(compiled, "de_marsh")
    _opener, confirm = _steps(result)
    assert confirm.guard is None
    (note,) = result.guard_notes
    assert note["change"] == "no_empty_result_guard"
    assert "one demonstration" in note["warning"]


# -- evidence across demonstrations ----------------------------------------------
def test_the_same_line_across_different_searches_is_mined(compiled: dict) -> None:
    result = _induce(compiled, "de_marsh", "de_weber")
    assert result.certified, result.uncertainties
    opener, confirm = _steps(result)
    assert _exact_texts(opener) == [GERMAN]
    assert confirm.guard is not None and confirm.guard.on_unmet == "halt"
    assert confirm.guard.predicate.kind is PredicateKind.TEXT_PRESENT
    assert confirm.guard.predicate.text == GERMAN and confirm.guard.predicate.exact
    (note,) = result.guard_notes
    assert note["change"] == "empty_result_guard" and "typed" in note["evidence"]


def test_recording_directories_carry_their_candidates() -> None:
    result = induce_program([FIXTURES / "de_marsh", FIXTURES / "de_weber"])
    _opener, confirm = _steps(result)
    assert confirm.guard is not None and confirm.guard.predicate.text == GERMAN


def test_a_line_that_varies_with_the_data_is_not_mined(compiled: dict) -> None:
    """ "Keine Treffer im Archiv Nord" / "... West": no value, no number, but
    it changes with the record searched, so it is data."""

    result = _induce(compiled, "archive_marsh", "archive_weber")
    opener, confirm = _steps(result)
    assert confirm.guard is None and _exact_texts(opener) == []
    (note,) = result.guard_notes
    assert "differs between demonstrations" in note["warning"]


def test_a_line_with_a_parameter_is_not_mined(compiled: dict) -> None:
    _workflow, notes = compiled["names_marsh"]
    (missed,) = [n for n in notes if n["change"] == "no_empty_result_guard"]
    assert "dialog_line" not in missed
    assert "run value" in missed["dialog_line_note"]
    result = _induce(compiled, "names_marsh", "de_weber")
    _opener, confirm = _steps(result)
    assert confirm.guard is None
    assert "not every demonstration" in result.guard_notes[0]["warning"]


def test_the_same_search_twice_proves_nothing(compiled: dict) -> None:
    result = _induce(compiled, "de_marsh", "de_marsh_again")
    _opener, confirm = _steps(result)
    assert confirm.guard is None
    assert "typed before the dialog is the same" in result.guard_notes[0]["warning"]


def test_a_later_field_that_differs_does_not_prove_the_search_differed(
    compiled: dict,
) -> None:
    """The same name both times, then a different comment: the archive line
    depends on the name, which did not change, so it proves nothing."""

    for name in ("comment_marsh_first", "comment_marsh_second"):
        (missed,) = [
            n for n in compiled[name][1] if n["change"] == "no_empty_result_guard"
        ]
        # Both compiles kept the line: the refusal is induction's.
        assert missed["dialog_line"] == "Keine Treffer im Archiv Nord"
    result = _induce(compiled, "comment_marsh_first", "comment_marsh_second")
    assert result.certified, result.uncertainties
    opener, confirm = _steps(result)
    assert confirm.guard is None and _exact_texts(opener) == []
    (note,) = result.guard_notes
    assert note["change"] == "no_empty_result_guard"
    assert "typed before the dialog is the same" in note["warning"]
    assert note["step"].startswith("step_") and note["confirm_step"].startswith("step_")


def test_a_guard_the_demonstrations_disagree_on_refuses_induction(
    compiled: dict,
) -> None:
    """The English recording's compile mined "No results found"; the German
    one mined nothing. Copying the first one's guard would pick one dialog,
    dropping it would click through a listed match: induction refuses."""

    workflow, _notes = compiled["en_weber"]
    assert workflow.steps[3].guard is not None
    for order in (("en_weber", "de_marsh"), ("de_marsh", "en_weber")):
        result = _induce(compiled, *order)
        assert not result.certified and result.workflow is None
        assert [u.kind for u in result.uncertainties] == ["ambiguous_guard"]
        assert result.uncertainties[0].consequential
        (note,) = result.guard_notes
        assert note["change"] == "no_empty_result_guard"
        assert "disagree" in note["warning"]


def test_a_question_is_not_a_search_result() -> None:
    line, why = dialog_statement(
        *_frames("question_marsh"),
        confirm_point=_confirm_point("question_marsh"),
        title="Dublettensuche",
    )
    assert line is None and "question" in why


def test_a_disagreeing_guard_off_the_straight_path_refuses() -> None:
    """An optional or loop column copies one trace's step: its mined check
    must be the same at that place in every trace and row."""

    from openadapt_flow.compiler.empty_result_guard import exact_checks
    from openadapt_flow.compiler.induction import (
        _Column,
        _refuse_unreconciled_guards,
        _SingleTok,
    )
    from openadapt_flow.ir import ActionKind

    def confirm(text: str | None) -> Step:
        step = Step(id="step_003", intent="click 'Confirm'", action=ActionKind.CLICK)
        if text:
            step.guard = exact_checks(text)[1]
        return step

    for texts, refused in (
        (("Keine Treffer gefunden.", None), True),
        (("Keine Treffer gefunden.", "No results found"), True),
        (("Keine Treffer gefunden.", "Keine Treffer gefunden."), False),
        ((None, None), False),
    ):
        col = _Column(("click", "confirm:select:button"), "single")
        for t, text in enumerate(texts):
            col.tokens[t] = _SingleTok(confirm(text))
        result = InductionResult(n_traces=2)
        _refuse_unreconciled_guards([col], set(), result)
        assert bool(result.uncertainties) is refused, texts

    # A Confirm off the straight path with no check is noted once.
    col = _Column(("click", "confirm:select:button"), "single")
    col.tokens[0] = _SingleTok(confirm(None))
    col.tokens[1] = _SingleTok(confirm(None))
    result = InductionResult(n_traces=2)
    pairs = [{("step_002", "step_003")}, {("step_002", "step_003")}]
    _refuse_unreconciled_guards([col], set(), result, pairs)
    assert not result.uncertainties
    (note,) = result.guard_notes
    assert note["change"] == "no_empty_result_guard"
    assert (note["step"], note["confirm_step"]) == ("step_002", "step_003")
    assert "straight path" in note["warning"]

    # The opener's exact postcondition is compared the same way.
    opener = Step(id="step_002", intent="click 'Create'", action=ActionKind.CLICK)
    col = _Column(("click", "select:button"), "single")
    col.tokens[0] = _SingleTok(opener)
    col.tokens[1] = _SingleTok(
        opener.model_copy(update={"expect": [exact_checks("No results found")[0]]})
    )
    result = InductionResult(n_traces=2)
    _refuse_unreconciled_guards([col], set(), result)
    assert [u.kind for u in result.uncertainties] == ["ambiguous_guard"]


def test_a_guard_every_demonstration_mined_is_kept(compiled: dict) -> None:
    result = _induce(compiled, "en_weber", "en_weber")
    opener, confirm = _steps(result)
    assert confirm.guard is not None
    assert confirm.guard.predicate.text == "No results found"
    assert _exact_texts(opener) == ["No results found"]
    assert result.guard_notes[0]["change"] == "empty_result_guard"


# -- qualification ----------------------------------------------------------------
def test_qualification_needs_a_run_that_halts_at_the_mined_line(
    compiled: dict,
) -> None:
    """On the induced program itself (a graph, no linear steps)."""

    from openadapt_flow.qualify.campaign import business_rule_problems, rule_halt_steps
    from openadapt_flow.qualify.config import BusinessRuleCase, VerifyConfig

    result = _induce(compiled, "de_marsh", "de_weber")
    workflow = result.workflow
    assert workflow is not None and workflow.steps == []
    opener, confirm = _steps(result)
    config = VerifyConfig.model_construct(business_rules=[])
    (problem,) = business_rule_problems(workflow, config, confirm, {})
    assert "business_rules case" in problem and confirm.id in problem
    rule = BusinessRuleCase.model_construct(
        id="duplicate", params={}, halt_at="empty_result_guard"
    )
    assert rule_halt_steps(workflow, rule, confirm.id) == {opener.id, confirm.id}
    config = VerifyConfig.model_construct(business_rules=[rule])
    assert business_rule_problems(workflow, config, confirm, {}) == []


def test_the_mined_check_is_a_text_check(compiled: dict) -> None:
    opener, _confirm = _steps(_induce(compiled, "de_marsh", "de_weber"))
    (pc,) = [pc for pc in opener.expect if pc.exact]
    assert pc.kind is PostconditionKind.TEXT_PRESENT

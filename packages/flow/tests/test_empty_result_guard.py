"""The compiler mines the empty result a person confirmed through (#1400).

OpenEMR's New Patient form opens a duplicate check whose dialog says "No
matches were found." above "Confirm Create New Patient". Generic mining kept
one other line of the dialog, so a run on a possible duplicate passed the
dialog and saved a second chart. The fixture is the last two events of a real
OpenEMR referral recording (``tests/fixtures/openemr_duplicate_dialog``).
"""

from __future__ import annotations

import importlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openadapt_flow.compiler.compile import lint_param_leakage
from openadapt_flow.compiler.empty_result_guard import (
    GUARD_TIMEOUT_S,
    carries_value,
    empty_result_statements,
    is_empty_result_statement,
    mine_empty_result_guard,
)
from openadapt_flow.ir import (
    ActionKind,
    Guard,
    Postcondition,
    PostconditionKind,
    Predicate,
    PredicateKind,
    Step,
    Workflow,
)
from openadapt_flow.vision.ocr import OcrLine

# The module, not the ``ocr`` function the vision package re-exports.
ocr_mod = importlib.import_module("openadapt_flow.vision.ocr")

FIXTURE = Path(__file__).parent / "fixtures" / "openemr_duplicate_dialog"
NO_MATCH = "No matches were found."


def _line(text: str, x: int, y: int, w: int, h: int, conf: float = 0.97) -> OcrLine:
    return OcrLine(text=text, region=(x, y, w, h), confidence=conf)


# The OCR of the real dialog frame (0000_after.png), around the dialog body:
# the statement wraps over two lines, and a form button behind the overlay is
# read between them.
DIALOG = [
    _line("1-0of 0", 993, 50, 65, 20),
    _line("Hits", 223, 129, 30, 18),
    _line("Name", 451, 126, 40, 23),
    _line("DOB", 766, 130, 30, 15),
    _line("City:", 106, 179, 43, 27),
    _line("No matches were", 224, 170, 162, 26),
    _line("Add", 1120, 186, 33, 19),
    _line("found.", 223, 196, 62, 24, 1.0),
    _line("Confirm Create New Patient", 497, 268, 207, 24),
    _line("Cancel", 728, 269, 55, 22),
]
FORM = [
    _line("City:", 106, 179, 43, 27),
    _line("Add", 1120, 186, 33, 19),
    _line("Create New Patient", 226, 763, 144, 20),
]
# The same dialog when OpenEMR finds a possible duplicate: a row, no statement.
POSSIBLE_DUPLICATE = [
    _line("1 - 1 of 1", 993, 50, 65, 20),
    _line("Hits", 223, 113, 30, 18),
    _line("2", 227, 158, 10, 18),
    _line("Delgado, Rosa", 274, 157, 107, 20),
    _line("1958-03-27", 684, 157, 82, 20),
    _line("Confirm Create New Patient", 497, 218, 207, 24),
]
# The dialog shell before its search returned (a later after-frame).
LOADING = [
    _line("Hits", 223, 129, 30, 18),
    _line("Confirm Create New Patient", 497, 268, 207, 24),
]


@pytest.mark.parametrize(
    "text",
    [
        "No matches were found.",
        "No matching records found",
        "No possible duplicates were found",
        "No results",
        "There are no matching patients.",
        "Nothing found",
        "0 results",
        "No results found for this search",
        "We found no matches",
        "No conflicts",
    ],
)
def test_empty_search_results_are_recognized(text: str) -> None:
    assert is_empty_result_statement(text)


@pytest.mark.parametrize(
    "text",
    [
        "No",
        "No, cancel",
        "Notes",
        "No matches for Delgado",  # a search term: run data, not a statement
        "No Delgado matches found",  # a name is not one of the fixed words
        "1 match found",
        "2 results",
        "No insurance on file",
        "Confirm Create New Patient",
        "Matches were found",
        # Bare empty lists: informational sections, not a search result.
        "No documents",
        "No records",
        "No records found",
        "No patients found",
        "No items",
    ],
)
def test_other_text_is_not_a_statement(text: str) -> None:
    assert not is_empty_result_statement(text)


def test_a_wrapped_statement_is_joined() -> None:
    assert empty_result_statements(DIALOG) == [NO_MATCH]


def test_unconfident_lines_are_not_used() -> None:
    lines = [
        _line("No matches were", 224, 170, 162, 26, 0.6),
        _line("found.", 223, 196, 62, 24),
    ]
    assert empty_result_statements(lines) == []


def test_the_dialogs_empty_result_is_mined_as_two_exact_checks() -> None:
    mined = mine_empty_result_guard(FORM, DIALOG, DIALOG)
    assert mined.postcondition == Postcondition(
        kind=PostconditionKind.TEXT_PRESENT,
        text=NO_MATCH,
        timeout_s=GUARD_TIMEOUT_S,
        exact=True,
    )
    assert GUARD_TIMEOUT_S == 10.0
    # The same statement, re-checked on the screen right before Confirm.
    assert mined.guard is not None and mined.guard.on_unmet == "halt"
    assert mined.guard.predicate.kind is PredicateKind.TEXT_PRESENT
    assert mined.guard.predicate.text == NO_MATCH and mined.guard.predicate.exact
    assert mined.note is None


def test_a_result_that_loads_after_the_dialog_is_still_mined() -> None:
    mined = mine_empty_result_guard(FORM, LOADING, DIALOG)
    assert mined.postcondition is not None and mined.postcondition.text == NO_MATCH


def _missed(mined: Any) -> None:
    assert mined.postcondition is None and mined.guard is None
    assert mined.note


def test_nothing_is_mined_from_a_dialog_that_lists_a_match() -> None:
    _missed(mine_empty_result_guard(FORM, POSSIBLE_DUPLICATE, POSSIBLE_DUPLICATE))


def test_a_statement_already_on_screen_is_not_the_dialogs() -> None:
    _missed(mine_empty_result_guard(DIALOG, DIALOG, DIALOG))


def test_a_statement_gone_before_confirm_is_not_mined() -> None:
    _missed(mine_empty_result_guard(FORM, DIALOG, POSSIBLE_DUPLICATE))


def test_two_result_sections_are_not_mined() -> None:
    """One check cannot tell which section is empty at run time."""

    twice = [
        *DIALOG,
        _line("No matches were found.", 224, 400, 210, 26),
    ]
    mined = mine_empty_result_guard(FORM, twice, twice)
    _missed(mined)
    assert "more than one result section" in (mined.note or "")
    two_kinds = [*DIALOG, _line("No possible duplicates", 224, 400, 200, 26)]
    _missed(mine_empty_result_guard(FORM, two_kinds, two_kinds))


def test_an_informational_empty_section_is_not_mined() -> None:
    delete_dialog = [
        _line("Delete patient?", 400, 100, 140, 24),
        _line("Linked documents:", 300, 160, 150, 22),
        _line("No documents", 470, 160, 110, 22),
        _line("Open orders:", 300, 200, 110, 22),
        _line("No records", 470, 200, 90, 22),
        _line("Confirm", 497, 268, 80, 24),
    ]
    _missed(mine_empty_result_guard(FORM, delete_dialog, delete_dialog))


def test_reject_refuses_a_statement() -> None:
    _missed(mine_empty_result_guard(FORM, DIALOG, DIALOG, reject=lambda _t: True))


def test_a_value_is_found_only_as_whole_words() -> None:
    # The compiler's fuzzy screen counts scattered letters ("Ms." in "No
    # matches were found."); a statement can carry a value only as words.
    assert not carries_value(NO_MATCH, ("Ms.", "Marsh", "Elena", "USA", "Mat"))
    assert carries_value("No possible duplicates found", ("Possible",))
    assert carries_value("No possible duplicates found", ("possible duplicates",))


def test_param_leak_lint_judges_a_statement_by_its_words() -> None:
    step = Step(
        id="step_000",
        intent="click 'Create New Patient'",
        action=ActionKind.CLICK,
        expect=[
            Postcondition(kind=PostconditionKind.TEXT_PRESENT, text=NO_MATCH),
        ],
    )
    workflow = Workflow(name="w", steps=[step])
    assert lint_param_leakage(workflow, ("Ms.", "Marsh")) == []
    # Any other text keeps the fuzzy screen.
    step.expect = [
        Postcondition(kind=PostconditionKind.TEXT_PRESENT, text="Welcome, Ms. Marsh")
    ]
    assert lint_param_leakage(workflow, ("Ms.", "Marsh"))


def test_exact_serializes_only_when_set() -> None:
    plain = Postcondition(kind=PostconditionKind.TEXT_PRESENT, text="x")
    assert "exact" not in plain.model_dump(mode="json")
    pred = Predicate(kind=PredicateKind.TEXT_PRESENT, text="x")
    assert "exact" not in pred.model_dump(mode="json")
    assert Predicate.model_validate(
        Predicate(kind=PredicateKind.TEXT_PRESENT, text="x", exact=True).model_dump()
    ).exact


# -- the exact check at run time ---------------------------------------------------
def _ocr_reads(monkeypatch: pytest.MonkeyPatch, lines: list[OcrLine]) -> None:
    monkeypatch.setattr(ocr_mod, "ocr", lambda _png, region=None: list(lines))
    monkeypatch.setattr(ocr_mod, "upscale_png", lambda png, factor=2: png)


@pytest.mark.parametrize(
    "reads",
    [
        "2 matches were found",
        "Matches were found",
        "Possible matches were found",
        "No matches were found for Smith",
    ],
)
def test_a_near_miss_passes_fuzzy_ocr_but_not_the_exact_check(
    monkeypatch: pytest.MonkeyPatch, reads: str
) -> None:
    _ocr_reads(monkeypatch, [_line(reads, 224, 170, 260, 26)])
    assert ocr_mod.text_present(b"png", NO_MATCH)  # the fuzzy check passes
    assert ocr_mod.exact_text_count(b"png", NO_MATCH) == 0


def test_the_exact_check_reads_a_wrapped_statement_and_counts_copies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ocr_reads(monkeypatch, DIALOG)
    assert ocr_mod.exact_text_count(b"png", NO_MATCH) == 1
    _ocr_reads(monkeypatch, [*DIALOG, _line(NO_MATCH, 224, 400, 210, 26)])
    assert ocr_mod.exact_text_count(b"png", NO_MATCH) == 2


def _replayer(count: Any, dom: bool = False) -> Any:
    from openadapt_flow.runtime.replayer import Replayer
    from tests.test_replayer import FakeVision

    backend = SimpleNamespace(
        url="http://x/",
        page_title="x",
        page_count=1,
        visible_text_present=lambda _text: dom or None,
    )
    replayer = Replayer(backend, vision=FakeVision())
    if count is None:
        replayer.vision = SimpleNamespace(text_present=lambda *_a, **_k: True)
    else:
        replayer.vision = SimpleNamespace(
            text_present=lambda *_a, **_k: True,
            exact_text_count=lambda _png, _text: count,
        )
    return replayer


@pytest.mark.parametrize(
    ("count", "dom", "holds"),
    [
        (1, False, True),
        (2, False, False),  # two copies: which section is empty is unknown
        (2, True, False),
        (0, False, False),  # "2 matches were found": the fuzzy match is never used
        (0, True, True),  # OCR misread; the DOM proves the exact text visible
        (None, True, False),  # no exact counter: fail closed
    ],
)
def test_the_runtime_exact_check(count: Any, dom: bool, holds: bool) -> None:
    replayer = _replayer(count, dom)
    pc = Postcondition(kind=PostconditionKind.TEXT_PRESENT, text=NO_MATCH, exact=True)
    assert replayer._postcondition_passes(pc, b"png", Path(".")) is holds
    pred = Predicate(kind=PredicateKind.TEXT_PRESENT, text=NO_MATCH, exact=True)
    assert replayer._predicate_holds(pred, b"png", Path("."), {}) is holds


def test_ordinary_text_checks_keep_the_fuzzy_match() -> None:
    replayer = _replayer(0)
    pc = Postcondition(kind=PostconditionKind.TEXT_PRESENT, text=NO_MATCH)
    assert replayer._postcondition_passes(pc, b"png", Path(".")) is True


def test_an_exact_guard_is_carried_by_the_api_steps_precheck(tmp_path: Path) -> None:
    from openadapt_flow.compiler.api_proposal import propose_api_step
    from tests.test_api_tier_web_form import _recording, _workflow

    def with_guard(workflow: Workflow, *, exact: bool = True) -> Workflow:
        steps = list(workflow.steps)
        steps[7] = steps[7].model_copy(
            update={
                "guard": Guard(
                    predicate=Predicate(
                        kind=PredicateKind.TEXT_PRESENT, text=NO_MATCH, exact=exact
                    )
                )
            }
        )
        return workflow.model_copy(update={"steps": steps})

    rec = _recording(tmp_path)
    _wf, step, reasons = propose_api_step(with_guard(_workflow()), rec)
    assert reasons == [] and step is not None
    # Not carried: no earlier check reads it, or the guard is a fuzzy one.
    for workflow in (
        with_guard(_workflow(dialog_expect=False)),
        with_guard(_workflow(), exact=False),
    ):
        _wf, step, reasons = propose_api_step(workflow, rec)
        assert step is None and "guard or wait rule" in reasons[0]


# -- the real recording ------------------------------------------------------------
@pytest.fixture(scope="module")
def compiled(tmp_path_factory: pytest.TempPathFactory) -> tuple[Workflow, Path]:
    from openadapt_flow.compiler import compile_recording

    root = tmp_path_factory.mktemp("openemr")
    recording = root / "recording"
    shutil.copytree(FIXTURE, recording)
    bundle = root / "bundle"
    return compile_recording(recording, bundle, name="openemr-dialog"), bundle


def test_compiling_the_openemr_recording_mines_the_no_match_checks(
    compiled: tuple[Workflow, Path],
) -> None:
    workflow, bundle = compiled
    opener, confirm = workflow.steps
    assert opener.anchor is not None and opener.anchor.structural is not None
    assert opener.anchor.structural.selector == "#create"
    assert opener.risk_explanation is not None
    assert "opens a confirmation dialog" in opener.risk_explanation
    guards = [p for p in opener.expect if p.text == NO_MATCH]
    assert guards == [
        Postcondition(
            kind=PostconditionKind.TEXT_PRESENT,
            text=NO_MATCH,
            timeout_s=10.0,
            exact=True,
        )
    ]
    # Checked again on the screen right before the Confirm click.
    assert confirm.guard is not None
    assert confirm.guard.predicate.text == NO_MATCH and confirm.guard.predicate.exact
    assert not any(p.text == NO_MATCH for p in confirm.expect)
    notes = json.loads((bundle / "compile_notes.json").read_text())
    assert {"change": "empty_result_guard", "step": "step_000"}.items() <= next(
        n for n in notes["changes"] if n["change"] == "empty_result_guard"
    ).items()


def test_a_dialog_without_a_mined_check_is_noted(tmp_path: Path) -> None:
    """The opener's after frame and the Confirm frame replaced by the form:
    the dialog showed no empty result, and the compile says so."""

    from openadapt_flow.compiler import compile_recording

    recording = tmp_path / "recording"
    shutil.copytree(FIXTURE, recording)
    form = (FIXTURE / "frames" / "0000_before.png").read_bytes()
    for name in ("0000_after.png", "0001_before.png"):
        (recording / "frames" / name).write_bytes(form)
    workflow = compile_recording(recording, tmp_path / "bundle", name="no-dialog")
    assert workflow.steps[1].guard is None
    notes = json.loads((tmp_path / "bundle" / "compile_notes.json").read_text())
    (missed,) = [n for n in notes["changes"] if n["change"] == "no_empty_result_guard"]
    assert missed["step"] == "step_000" and missed["confirm_step"] == "step_001"
    assert "no check that the dialog found nothing" in missed["warning"]


def test_the_exact_check_on_the_recorded_frames() -> None:
    frame = (FIXTURE / "frames" / "0001_before.png").read_bytes()
    assert ocr_mod.exact_text_count(frame, NO_MATCH) == 1
    # The form before the dialog opened never shows it.
    form = (FIXTURE / "frames" / "0000_before.png").read_bytes()
    assert ocr_mod.exact_text_count(form, NO_MATCH) == 0

"""The compiler mines the empty result a person confirmed through (#1400).

OpenEMR's New Patient form opens a duplicate check whose dialog says "No
matches were found." above "Confirm Create New Patient". Generic mining kept
one other line of the dialog, so a run on a possible duplicate passed the
dialog and saved a second chart. The fixture is the last two events of a real
OpenEMR referral recording (``tests/fixtures/openemr_duplicate_dialog``).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from openadapt_flow.compiler.compile import lint_param_leakage
from openadapt_flow.compiler.empty_result_guard import (
    GUARD_TIMEOUT_S,
    carries_value,
    empty_result_statements,
    is_empty_result_statement,
    mine_empty_result_guards,
)
from openadapt_flow.ir import (
    ActionKind,
    Postcondition,
    PostconditionKind,
    Step,
    Workflow,
)
from openadapt_flow.vision.ocr import OcrLine

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


@pytest.mark.parametrize(
    "text",
    [
        "No matches were found.",
        "No matching records found",
        "No possible duplicates were found",
        "No results",
        "No patients found.",
        "There are no matching patients.",
        "Nothing found",
        "0 results",
        "No results found for this search",
        "We found no matches",
    ],
)
def test_empty_result_statements_are_recognized(text: str) -> None:
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


def test_the_dialogs_empty_result_is_mined() -> None:
    guards = mine_empty_result_guards(FORM, DIALOG, DIALOG)
    assert guards == [
        Postcondition(
            kind=PostconditionKind.TEXT_PRESENT, text=NO_MATCH, timeout_s=10.0
        )
    ]
    assert GUARD_TIMEOUT_S == 10.0


def test_nothing_is_mined_from_a_dialog_that_lists_a_match() -> None:
    assert mine_empty_result_guards(FORM, POSSIBLE_DUPLICATE, POSSIBLE_DUPLICATE) == []


def test_a_statement_already_on_screen_is_not_the_dialogs() -> None:
    assert mine_empty_result_guards(DIALOG, DIALOG, DIALOG) == []


def test_a_statement_gone_before_confirm_is_not_mined() -> None:
    # Shown after the click, but not on the screen the person confirmed on
    # (a list still loading, a toast).
    assert mine_empty_result_guards(FORM, DIALOG, POSSIBLE_DUPLICATE) == []


def test_reject_refuses_a_statement() -> None:
    assert mine_empty_result_guards(FORM, DIALOG, DIALOG, reject=lambda _t: True) == []


def test_a_value_is_found_only_as_a_word() -> None:
    # The compiler's fuzzy screen counts scattered letters ("Ms." in "No
    # matches were found."); a statement can carry a value only as a word.
    assert not carries_value(NO_MATCH, ("Ms.", "Marsh", "Elena", "USA"))
    assert carries_value("No patients found", ("Patients",))


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


@pytest.fixture(scope="module")
def compiled(tmp_path_factory: pytest.TempPathFactory) -> Workflow:
    from openadapt_flow.compiler import compile_recording

    root = tmp_path_factory.mktemp("openemr")
    recording = root / "recording"
    shutil.copytree(FIXTURE, recording)
    return compile_recording(recording, root / "bundle", name="openemr-dialog")


def test_compiling_the_openemr_recording_mines_the_no_match_check(
    compiled: Workflow,
) -> None:
    opener, confirm = compiled.steps
    assert opener.anchor is not None and opener.anchor.structural is not None
    assert opener.anchor.structural.selector == "#create"
    assert opener.risk_explanation is not None
    assert "opens a confirmation dialog" in opener.risk_explanation
    guards = [p for p in opener.expect if p.text == NO_MATCH]
    assert guards == [
        Postcondition(
            kind=PostconditionKind.TEXT_PRESENT, text=NO_MATCH, timeout_s=10.0
        )
    ]
    # The guard is checked before the Confirm click, not after it.
    assert not any(p.text == NO_MATCH for p in confirm.expect)


def test_the_mined_check_passes_on_the_recorded_dialog() -> None:
    from openadapt_flow.vision.ocr import text_present

    frame = (FIXTURE / "frames" / "0001_before.png").read_bytes()
    assert text_present(frame, NO_MATCH)
    # The form before the dialog opened never shows it.
    form = (FIXTURE / "frames" / "0000_before.png").read_bytes()
    assert not text_present(form, NO_MATCH)

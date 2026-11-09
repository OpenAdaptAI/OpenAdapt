"""OCR coincidence cannot establish that a changed field is an automatic echo.

Real-RDP finding (benchmark/rdp_avc_real and the UTM lab re-measure): the
Patient Notes Search step compiled "Patient: Neil Sorenson (1984-03-12)", the
detail header the app fills in for the grid row it auto-selects. With a decoy
patient listed first the header named the decoy, and all 10 decoy runs
stopped though nothing was wrong. Identity banners, titles and save messages
must stay checks (review blocker on the first version of this rule).
"""

from __future__ import annotations

import json
from pathlib import Path

from openadapt_flow.compiler.compile import _new_text_postcondition
from openadapt_flow.vision.ocr import OcrLine

FIXTURE = Path(__file__).parent / "fixtures" / "selection_echo_patient_notes.json"


def _ocr(rows: list[dict]) -> list[OcrLine]:
    return [
        OcrLine(text=r["text"], region=tuple(r["region"]), confidence=r["confidence"])
        for r in rows
    ]


def _line(text: str, x: int, y: int, w: int = 200, h: int = 20) -> OcrLine:
    return OcrLine(text=text, region=(x, y, w, h), confidence=0.99)


def _compile_args(step: dict) -> dict:
    from datetime import date

    return {
        "exclude_texts": tuple(step["exclude_texts"]),
        "avoid_labels": tuple(step["avoid_labels"]),
        "next_lines": _ocr(step["next"]) if step["next"] is not None else None,
        "reference_date": date.fromisoformat(step["reference_date"]),
        "click_point": tuple(step["click"]),
    }


def test_real_recording_search_keeps_its_text_without_selection_proof():
    # The exact OCR lines and arguments compile_recording passed; on main
    # this compiled "Patient: Neil Sorenson (1984-03-12)".
    step = json.loads(FIXTURE.read_text())["search_step"]
    assert step["main_picked"] == "Patient: Neil Sorenson (1984-03-12)"
    pc = _new_text_postcondition(
        _ocr(step["before"]), _ocr(step["after"]), **_compile_args(step)
    )
    assert pc is not None and pc.text == step["main_picked"]


def test_real_recording_save_step_keeps_its_confirmation():
    step = json.loads(FIXTURE.read_text())["save_step"]
    pc = _new_text_postcondition(
        _ocr(step["before"]), _ocr(step["after"]), **_compile_args(step)
    )
    assert pc is not None and pc.text == "Saved note for patient 1"


GRID = [
    _line("Neil", 24, 136, w=60),
    _line("Sorenson", 215, 136, w=90),
    _line("1984-03-12", 405, 136, w=100),
]
HEADER_BEFORE = _line("Patient: James Alvarez (1981-05-21)", 232, 318)
HEADER_AFTER = _line("Patient: Neil Sorenson (1984-03-12)", 232, 318)


def _check_text(before, after, click=None):
    from datetime import date

    return _new_text_postcondition(
        before, after, click_point=click, reference_date=date(2026, 11, 3)
    )


def test_names_dates_unicode_and_save_text_remain_positive_checks() -> None:
    for text in (
        "Patient: Neil Sorenson (1984-03-12)",
        "José Müller",
        "Saved note for patient 1",
    ):
        check = _check_text([], [_line(text, 22, 500)])
        assert check is not None and check.text == text


def test_a_header_that_changed_to_an_unclicked_row_keeps_its_check() -> None:
    check = _check_text([HEADER_BEFORE], GRID + [HEADER_AFTER], (560, 56))
    assert check is not None and check.text == HEADER_AFTER.text


def test_a_header_restating_the_clicked_row_stays_a_check() -> None:
    check = _check_text([HEADER_BEFORE], GRID + [HEADER_AFTER], (100, 145))
    assert check is not None and check.text == HEADER_AFTER.text


def test_identity_banners_titles_and_save_messages_stay_checks() -> None:
    title = _line("Chart - Neil Sorenson", 10, 5, w=300)
    banner = _line("Neil Sorenson DOB 1984-03-12", 10, 60, w=300)
    saved = _line("Saved note for Neil Sorenson", 22, 500, w=300)
    submitted = _line("Order Submitted", 22, 520)
    for candidate in (title, banner, saved, submitted):
        check = _check_text([], [candidate], (560, 490))
        assert check is not None and check.text == candidate.text
    check = _check_text([], [title, banner], (100, 145))
    assert check is not None and check.text in {title.text, banner.text}
    check = _check_text([], GRID + [saved], (560, 490))
    assert check is not None and check.text == saved.text
    others = [_line("Order", 24, 100), _line("Submitted", 215, 160)]
    check = _check_text([], others + [submitted], (560, 490))
    # Preserve the existing proximity ranking in this ambiguous control.
    assert check is not None and check.text == "Submitted"
    check = _check_text(others, others + [submitted], (560, 490))
    assert check is not None and check.text == submitted.text


def test_a_header_echoing_two_lines_that_are_not_one_row_is_kept() -> None:
    scattered = [_line("Neil", 24, 136, w=60), _line("Sorenson", 215, 260, w=90)]
    check = _check_text([HEADER_BEFORE], scattered + [HEADER_AFTER], (560, 56))
    assert check is not None and check.text == HEADER_AFTER.text


def test_a_click_in_a_rows_padding_still_selects_that_row() -> None:
    rows = [
        _line("James", 24, 138, w=60),
        _line("Alvarez", 215, 138, w=90),
        _line("Neil", 24, 182, w=60),
        _line("Sorenson", 215, 182, w=90),
        _line("1984-03-12", 405, 182, w=100),
    ]
    for click in ((300, 202), (300, 170)):
        check = _check_text([HEADER_BEFORE], rows + [HEADER_AFTER], click)
        assert check is not None and check.text == HEADER_AFTER.text

"""A step's text check is never a detail field restating a row the app selected.

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

from openadapt_flow.compiler.compile import (
    _echoes_screen_rows,
    _new_text_postcondition,
    _value_tokens,
)
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


def test_real_recording_search_step_no_longer_asserts_the_selected_row_header():
    # The exact OCR lines and arguments compile_recording passed; on main
    # this compiled "Patient: Neil Sorenson (1984-03-12)".
    step = json.loads(FIXTURE.read_text())["search_step"]
    assert step["main_picked"] == "Patient: Neil Sorenson (1984-03-12)"
    pc = _new_text_postcondition(
        _ocr(step["before"]), _ocr(step["after"]), **_compile_args(step)
    )
    assert pc is None or "Patient:" not in (pc.text or "")


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


def test_value_tokens_keep_names_and_dates_not_labels() -> None:
    assert _value_tokens("Patient: Neil Sorenson (1984-03-12)") == {
        "neil",
        "sorenson",
        "1984-03-12",
    }
    assert _value_tokens("José Müller") == {"josé", "müller"}
    assert _value_tokens("Saved note for patient 1") == {"saved"}


def test_a_header_that_changed_to_an_unclicked_row_is_an_echo() -> None:
    after = GRID + [HEADER_AFTER]
    assert _echoes_screen_rows(HEADER_AFTER, [HEADER_BEFORE], after, (560, 56))


def test_a_header_restating_the_clicked_row_stays_a_check() -> None:
    after = GRID + [HEADER_AFTER]
    assert not _echoes_screen_rows(HEADER_AFTER, [HEADER_BEFORE], after, (100, 145))


def test_identity_banners_titles_and_save_messages_stay_checks() -> None:
    # A chart opens: title and banner repeat each other, and nothing at
    # their places carried the same label before.
    title = _line("Chart - Neil Sorenson", 10, 5, w=300)
    banner = _line("Neil Sorenson DOB 1984-03-12", 10, 60, w=300)
    assert not _echoes_screen_rows(banner, [], [title, banner], (100, 145))
    assert not _echoes_screen_rows(title, [], [title, banner], (100, 145))
    # A save banner naming the patient whose row is on screen.
    saved = _line("Saved note for Neil Sorenson", 22, 500, w=300)
    assert not _echoes_screen_rows(saved, [], GRID + [saved], (560, 490))
    # Ordinary capitalized UI words spread over different rows.
    submitted = _line("Order Submitted", 22, 520)
    others = [_line("Order", 24, 100), _line("Submitted", 215, 160)]
    assert not _echoes_screen_rows(submitted, [], others + [submitted], (560, 490))


def test_a_header_echoing_two_lines_that_are_not_one_row_is_kept() -> None:
    scattered = [_line("Neil", 24, 136, w=60), _line("Sorenson", 215, 260, w=90)]
    after = scattered + [HEADER_AFTER]
    assert not _echoes_screen_rows(HEADER_AFTER, [HEADER_BEFORE], after, (560, 56))

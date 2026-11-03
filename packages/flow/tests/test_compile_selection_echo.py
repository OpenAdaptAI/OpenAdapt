"""A step's text check is never a restatement of a row the app selected.

Real-RDP finding (benchmark/rdp_avc_real and the UTM lab re-measure): the
Patient Notes Search step compiled "Patient: Neil Sorenson (1984-03-12)", the
detail header the app fills in for the grid row it auto-selects. With a decoy
patient listed first, the header named the decoy and all 10 decoy runs
stopped though nothing was wrong.
"""

from __future__ import annotations

from openadapt_flow.compiler.compile import (
    _echoes_screen_rows,
    _new_text_postcondition,
    _value_tokens,
)
from openadapt_flow.vision.ocr import OcrLine


def _line(text: str, x: int = 10, y: int = 300, h: int = 20) -> OcrLine:
    return OcrLine(text=text, region=(x, y, 200, h), confidence=0.99)


GRID_AFTER_SEARCH = [
    _line("Neil", x=24, y=136),
    _line("Sorenson", x=215, y=136),
    _line("1984-03-12", x=405, y=136),
    _line("Clinical note:", x=22, y=318),
    _line("Patient: Neil Sorenson (1984-03-12)", x=232, y=318),
    _line("Loaded 1 patients", x=22, y=481),
]
SEARCH_BUTTON = (560, 55)
NEIL_ROW = (100, 145)


def test_value_tokens_keep_names_and_dates_not_labels() -> None:
    assert _value_tokens("Patient: Neil Sorenson (1984-03-12)") == {
        "neil",
        "sorenson",
        "1984-03-12",
    }
    assert _value_tokens("Saved note for patient 1") == {"saved"}


def test_the_auto_selected_row_header_is_not_the_search_steps_check() -> None:
    # Before the search the grid already listed every patient, Neil
    # Sorenson among them, under the header of the first row (James Alvarez).
    before = [
        _line("Search", x=530, y=45),
        _line("James", x=24, y=136),
        _line("Alvarez", x=215, y=136),
        _line("Neil", x=24, y=180),
        _line("Sorenson", x=215, y=180),
        _line("1984-03-12", x=405, y=180),
        _line("Clinical note:", x=22, y=318),
        _line("Patient: James Alvarez (1981-05-21)", x=232, y=318),
        _line("Loaded 10 patients", y=481),
    ]
    pc = _new_text_postcondition(before, GRID_AFTER_SEARCH, click_point=SEARCH_BUTTON)
    assert pc is None or "Patient:" not in (pc.text or "")


def test_a_header_restating_the_clicked_row_stays_a_check() -> None:
    header = GRID_AFTER_SEARCH[4]
    assert _echoes_screen_rows(header, GRID_AFTER_SEARCH, SEARCH_BUTTON) is True
    assert _echoes_screen_rows(header, GRID_AFTER_SEARCH, NEIL_ROW) is False


def test_status_text_with_one_data_word_is_unaffected() -> None:
    after = GRID_AFTER_SEARCH + [_line("Saved note for patient 1", x=22, y=500)]
    assert _echoes_screen_rows(after[-1], after, SEARCH_BUTTON) is False
    before = list(GRID_AFTER_SEARCH)
    pc = _new_text_postcondition(before, after, click_point=(560, 490))
    assert pc is not None and pc.text == "Saved note for patient 1"

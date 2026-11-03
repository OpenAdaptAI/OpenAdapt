"""The screen form state reads the item's field rows, not the whole screen.

Real OCR on rendered frames: a clock elsewhere on the screen ticking over
must not void a person's approval, while an edited field still must.
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

import openadapt_flow.vision as vision
from openadapt_flow.approval_mode.form_state import field_rows, screen_text_state

SIZE = (1024, 768)


def _font(size: int):
    for name in ("DejaVuSans.ttf", "Arial.ttf", "/System/Library/Fonts/Helvetica.ttc"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _frame(note: str, clock: str) -> bytes:
    image = Image.new("RGB", SIZE, "white")
    draw = ImageDraw.Draw(image)
    font = _font(22)
    draw.text((40, 88), "Search: Neil Sorenson", fill="black", font=font)
    draw.text((40, 288), f"Clinical note: {note}", fill="black", font=font)
    draw.rectangle((0, 728, SIZE[0], SIZE[1]), fill=(32, 32, 32))
    draw.text((900, 736), clock, fill="white", font=font)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def test_rows_merge_and_stay_inside_the_frame():
    assert field_rows([], SIZE, 48) is None
    assert field_rows([100, 110, 300], SIZE, 48) == [
        (0, 76, 1024, 58),
        (0, 276, 1024, 48),
    ]
    assert field_rows([5], SIZE, 48) == [(0, 0, 1024, 29)]


def test_a_clock_outside_the_field_rows_does_not_change_the_form():
    rows = field_rows([100, 300], SIZE, 48)
    before = screen_text_state(vision, _frame("BP 128/82", "8:21 PM"), rows)
    after_clock = screen_text_state(vision, _frame("BP 128/82", "8:22 PM"), rows)
    assert before is not None
    assert before == after_clock
    # Without rows the whole screen is read, and the clock voids it.
    assert screen_text_state(
        vision, _frame("BP 128/82", "8:21 PM")
    ) != screen_text_state(vision, _frame("BP 128/82", "8:22 PM"))


def test_an_edited_field_in_a_row_still_changes_the_form():
    rows = field_rows([100, 300], SIZE, 48)
    before = screen_text_state(vision, _frame("BP 128/82", "8:21 PM"), rows)
    edited = screen_text_state(vision, _frame("BP 138/82", "8:21 PM"), rows)
    assert before is not None and edited is not None
    assert before != edited


def test_rows_that_read_nothing_are_unreadable_not_unchanged():
    blank = io.BytesIO()
    Image.new("RGB", SIZE, "white").save(blank, format="PNG")
    assert screen_text_state(vision, blank.getvalue(), [(0, 400, 1024, 48)]) is None

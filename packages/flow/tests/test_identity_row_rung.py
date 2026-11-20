"""Propose one moved row from its recorded identity band.

Rendered synthetic OCR exercises the proposal; the existing identity check
still decides whether the proposed point may be used.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

from PIL import Image, ImageDraw, ImageFont

import openadapt_flow.vision as vision
from openadapt_flow.ir import Anchor
from openadapt_flow.runtime.identity_template import build_identity_template
from openadapt_flow.runtime.resolver import _identity_row_point

SIZE = (800, 400)
ROW_Y = (60, 100, 140)  # row tops; each row is 40 px tall
CLICK = (60, 80)  # the first name cell of the recorded (first) row
BAND = (220, 70, 260, 20)  # last name and date of birth of that row


def _font(size: int):
    for name in ("DejaVuSans.ttf", "Arial.ttf", "/System/Library/Fonts/Helvetica.ttc"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _frame(rows, banner: str = "") -> bytes:
    image = Image.new("RGB", SIZE, "white")
    draw = ImageDraw.Draw(image)
    font = _font(18)
    for top, (first, last, dob) in zip(ROW_Y, rows):
        draw.text((24, top + 10), first, fill="black", font=font)
        draw.text((224, top + 10), last, fill="black", font=font)
        draw.text((404, top + 10), dob, fill="black", font=font)
    if banner:
        # A second place naming a record, in the band's columns.
        last, dob = banner.split()
        draw.text((224, 300), last, fill="black", font=font)
        draw.text((404, 300), dob, fill="black", font=font)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _anchor() -> Anchor:
    return Anchor(
        region=(CLICK[0] - 40, CLICK[1] - 20, 80, 40),
        click_point=CLICK,
        template="templates/step_003.png",
        ocr_text="Neil",
        identity_template=build_identity_template("Sorenson 1984-03-12"),
        identifier_region=BAND,
    )


TARGET = ("Neil", "Sorenson", "1984-03-12")
DECOY = ("Neil", "Anderson", "1972-10-04")
SIBLING = ("Neil", "Sorensen", "1983-01-19")


def test_a_moved_target_is_found_on_its_own_row():
    found = _identity_row_point(_anchor(), _frame([DECOY, TARGET]), vision)
    assert found is not None
    point, _region = found
    assert point[0] == CLICK[0]
    assert abs(point[1] - (CLICK[1] + 40)) <= 4


def test_a_near_identical_sibling_is_not_the_target():
    found = _identity_row_point(_anchor(), _frame([SIBLING, TARGET]), vision)
    assert found is not None
    assert abs(found[0][1] - (CLICK[1] + 40)) <= 4


def test_no_row_is_proposed_when_the_target_is_absent():
    assert _identity_row_point(_anchor(), _frame([SIBLING, DECOY]), vision) is None


def test_no_row_is_proposed_when_two_places_verify():
    # The target's row and a banner naming the same record: two places
    # verify, so no proposal (the other rungs and the identity check decide).
    frame = _frame([DECOY, TARGET], banner="Sorenson 1984-03-12")
    assert _identity_row_point(_anchor(), frame, vision) is None


def test_no_template_or_band_means_no_proposal():
    anchor = _anchor().model_copy(update={"identifier_region": None})
    assert _identity_row_point(anchor, _frame([DECOY, TARGET]), vision) is None
    anchor = _anchor().model_copy(update={"identity_template": None})
    assert _identity_row_point(anchor, _frame([DECOY, TARGET]), vision) is None


def test_an_unreadable_frame_is_no_proposal():
    broken = SimpleNamespace(ocr=lambda _png: 1 / 0)
    assert _identity_row_point(_anchor(), _frame([TARGET]), broken) is None

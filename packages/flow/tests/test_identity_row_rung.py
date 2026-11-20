"""Propose one moved row from its recorded identity band.

Rendered synthetic OCR exercises the proposal; the existing identity check
still decides whether the proposed point may be used.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw, ImageFont

import openadapt_flow.vision as vision
from openadapt_flow.ir import ActionKind, Anchor, Step, Workflow
from openadapt_flow.runtime.identity_template import build_identity_template
from openadapt_flow.runtime.replayer import Replayer
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


def _target_template() -> bytes:
    source = Image.open(io.BytesIO(_frame([TARGET])))
    x, y, w, h = _anchor().region
    out = io.BytesIO()
    source.crop((x, y, x + w, y + h)).save(out, format="PNG")
    return out.getvalue()


def _row_point(anchor, frame, observed=vision):
    return _identity_row_point(
        anchor, frame, observed, template_png=_target_template(), viewport=SIZE
    )


TARGET = ("Neil", "Sorenson", "1984-03-12")
DECOY = ("Neil", "Anderson", "1972-10-04")
SIBLING = ("Neil", "Sorensen", "1983-01-19")


def test_a_moved_target_is_found_on_its_own_row():
    found = _row_point(_anchor(), _frame([DECOY, TARGET]))
    assert found is not None
    point, _region = found
    assert point[0] == CLICK[0]
    assert abs(point[1] - (CLICK[1] + 40)) <= 4


def test_a_near_identical_sibling_is_not_the_target():
    found = _row_point(_anchor(), _frame([SIBLING, TARGET]))
    assert found is not None
    assert abs(found[0][1] - (CLICK[1] + 40)) <= 4


def test_no_row_is_proposed_when_the_target_is_absent():
    assert _row_point(_anchor(), _frame([SIBLING, DECOY])) is None


def test_no_row_is_proposed_when_two_places_verify():
    # The target's row and a banner naming the same record: two places
    # verify, so no proposal (the other rungs and the identity check decide).
    frame = _frame([DECOY, TARGET], banner="Sorenson 1984-03-12")
    assert _row_point(_anchor(), frame) is None


def test_no_template_or_band_means_no_proposal():
    anchor = _anchor().model_copy(update={"identifier_region": None})
    assert _row_point(anchor, _frame([DECOY, TARGET])) is None
    anchor = _anchor().model_copy(update={"identity_template": None})
    assert _row_point(anchor, _frame([DECOY, TARGET])) is None


def test_an_unreadable_frame_is_no_proposal():
    broken = SimpleNamespace(ocr=lambda _png: 1 / 0)
    assert _row_point(_anchor(), _frame([TARGET]), broken) is None


@pytest.mark.parametrize("identity_count", [0, 1, 2])
@pytest.mark.parametrize("target_present", [False, True])
@pytest.mark.parametrize("template_present", [False, True])
def test_joint_identity_and_target_presence_at_helper_and_runtime(
    tmp_path, identity_count, target_present, template_present
):
    # Both target labels repeat away from the original row, so earlier
    # template/label rungs cannot select one using its order alone.
    other = ("Other", "Different", "1970-02-03")
    rows = {
        0: [other, DECOY, SIBLING],
        1: [other, TARGET, SIBLING],
        2: [other, TARGET, TARGET],
    }[identity_count]
    # Keep both repeated targets beyond the existing local-template locality
    # radius, so the actual resolver reaches the row fallback being tested.
    shifted = Image.new("RGB", SIZE, "white")
    shifted.paste(Image.open(io.BytesIO(_frame(rows))), (0, 80))
    stream = io.BytesIO()
    shifted.save(stream, format="PNG")
    frame = stream.getvalue()
    if not target_present:
        image = Image.open(io.BytesIO(frame))
        ImageDraw.Draw(image).rectangle((0, 0, 120, SIZE[1]), fill="white")
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        frame = stream.getvalue()
    template = _target_template() if template_present else None
    anchor = _anchor().model_copy(update={"search_pad": 0, "ocr_text": None})
    expected = identity_count == 1 and target_present and template_present
    found = _identity_row_point(
        anchor, frame, vision, template_png=template, viewport=SIZE
    )
    assert (found is not None) is expected
    if template is not None:
        (tmp_path / "templates").mkdir()
        (tmp_path / anchor.template).write_bytes(template)
    step = Step(
        id="select",
        intent="select recorded row",
        action=ActionKind.CLICK,
        anchor=anchor,
        identity_armed=True,
        risk="reversible",
    )
    workflow = Workflow(name="joint-target", viewport=SIZE, steps=[step])
    replayer = Replayer(SimpleNamespace(viewport=SIZE), vision=vision)
    resolution, _region, error = replayer._resolve_step(step, frame, tmp_path, workflow)
    assert (resolution is not None and error is None) is expected
    if expected:
        assert resolution.rung == "geometry"
        assert abs(resolution.point[1] - (CLICK[1] + 120)) <= 4
        step.risk = "irreversible"
        resolution, _region, error = replayer._resolve_step(
            step, frame, tmp_path, workflow
        )
        assert resolution is not None and error is not None and "irreversible" in error


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "unreadable",
        "ambiguous",
        "outside",
        "contradictory",
        "low",
        "nonfinite",
    ],
)
def test_unknown_or_contradictory_target_is_no_row_proposal(fault):
    from openadapt_flow.runtime.resolver import TEMPLATE_THRESHOLD
    from openadapt_flow.vision.match import Match
    from openadapt_flow.vision.ocr import AmbiguousOcrMatchError

    frame = _frame([DECOY, TARGET])
    template = _target_template()
    seen = []

    def match(png, crop, **kwargs):
        assert png is frame and crop is template
        assert kwargs["threshold"] == TEMPLATE_THRESHOLD
        seen.append(kwargs["search_region"])
        if fault == "missing":
            return None
        if fault == "unreadable":
            raise ValueError("owned unreadable target")
        if fault == "ambiguous":
            raise AmbiguousOcrMatchError("owned ambiguous target")
        x, y, _w, _h = kwargs["search_region"]
        return Match(
            point=(x + 40, y + 20), region=(x + 1, y + 1, 80, 40), confidence=1.0
        ).model_copy(
            update={
                "region": (-200, -200, 80, 40)
                if fault == "outside"
                else (x + 1, y + 1, 80, 40),
                "point": (-200, -200) if fault == "contradictory" else (x + 40, y + 20),
                "confidence": 0.5
                if fault == "low"
                else float("nan")
                if fault == "nonfinite"
                else 1.0,
            }
        )

    observed = SimpleNamespace(ocr=vision.ocr, find_template=match)
    assert (
        _identity_row_point(
            _anchor(), frame, observed, template_png=template, viewport=SIZE
        )
        is None
    )
    assert len(seen) == 1

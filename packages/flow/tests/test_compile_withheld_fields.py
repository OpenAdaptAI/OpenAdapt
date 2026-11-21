"""Pixel checks never freeze a withheld credential field's black box.

The recorder fills a withheld field with black in every frame that still
shows it, so a REGION_STABLE crop over one would refute every live run.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw

from openadapt_flow.compiler.compile import (
    _checks_without_withheld_fields,
    _clip_away,
    _record_withheld_field_changes,
    _shows_blackout,
)
from openadapt_flow.ir import ActionKind, Postcondition, PostconditionKind, Step

USERNAME_BOX = (20, 10, 120, 20)  # withheld, blacked out by the recorder
PASSWORD_BOX = (20, 45, 120, 20)


def _frame(*, black: tuple[int, int, int, int] | None = None, whole: bool = False) -> bytes:
    image = Image.new("RGB", (200, 100), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 45, 140, 65), outline=(30, 90, 200), width=2)
    if whole:
        draw.rectangle((0, 0, 199, 99), fill="black")
    elif black is not None:
        x, y, w, h = black
        draw.rectangle((x, y, x + w - 1, y + h - 1), fill="black")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _step(region: tuple[int, int, int, int]) -> Step:
    return Step(
        id="step_002",
        intent="click recorded visual target",
        action=ActionKind.CLICK,
        expect=[
            Postcondition(
                kind=PostconditionKind.REGION_STABLE,
                region=region,
                phash="0" * 16,
                template="templates/step_002_expect.png",
            ),
            Postcondition(kind=PostconditionKind.TEXT_PRESENT, text="Sign in"),
        ],
    )


def test_blackout_is_read_from_the_frame_not_assumed():
    assert _shows_blackout(_frame(black=USERNAME_BOX), USERNAME_BOX)
    assert not _shows_blackout(_frame(), USERNAME_BOX)
    assert _shows_blackout(_frame(whole=True), USERNAME_BOX)
    assert not _shows_blackout(None, USERNAME_BOX)


def test_clip_keeps_the_largest_part_clear_of_the_box():
    assert _clip_away((10, 0, 160, 80), USERNAME_BOX) == (10, 30, 160, 50)
    # Nothing of 8 x 8 or more survives inside the box itself.
    assert _clip_away(USERNAME_BOX, USERNAME_BOX) is None


def test_a_check_over_the_black_box_is_clipped_and_recropped(tmp_path: Path):
    (tmp_path / "templates").mkdir()
    step = _step((10, 0, 160, 80))
    changes = _checks_without_withheld_fields(
        step, _frame(black=USERNAME_BOX), [USERNAME_BOX], tmp_path
    )
    region_checks = [pc for pc in step.expect if pc.kind is PostconditionKind.REGION_STABLE]
    assert [pc.region for pc in region_checks] == [(10, 30, 160, 50)]
    assert region_checks[0].phash != "0" * 16
    template = Image.open(tmp_path / "templates/step_002_expect.png").convert("L")
    assert template.size == (160, 50)
    pixels = list(template.getdata())
    assert sum(p <= 8 for p in pixels) / len(pixels) < 0.05
    assert [c["change"] for c in changes] == ["clip_region_stable_off_withheld_field"]
    # The other check on the step is untouched.
    assert any(pc.kind is PostconditionKind.TEXT_PRESENT for pc in step.expect)


def test_a_check_inside_the_black_box_is_dropped_and_recorded(tmp_path: Path):
    step = _step(USERNAME_BOX)
    changes = _checks_without_withheld_fields(
        step, _frame(black=USERNAME_BOX), [USERNAME_BOX], tmp_path
    )
    assert [pc.kind for pc in step.expect] == [PostconditionKind.TEXT_PRESENT]
    drops: list = []
    _record_withheld_field_changes(changes, drops)
    assert drops == [("step_002", USERNAME_BOX, "<withheld credential field>")]


def test_a_frame_from_before_the_field_was_typed_keeps_its_check(tmp_path: Path):
    step = _step((10, 0, 160, 80))
    assert _checks_without_withheld_fields(step, _frame(), [USERNAME_BOX], tmp_path) == []
    assert step.expect[0].region == (10, 0, 160, 80)


def test_a_check_clear_of_every_box_is_untouched(tmp_path: Path):
    step = _step(PASSWORD_BOX)
    before = step.model_copy(deep=True)
    assert _checks_without_withheld_fields(
        step, _frame(black=USERNAME_BOX), [USERNAME_BOX], tmp_path
    ) == []
    assert step == before

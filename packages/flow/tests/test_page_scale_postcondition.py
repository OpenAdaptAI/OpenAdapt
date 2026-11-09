"""A ``region_stable`` check holds under a uniform page zoom, and only then.

Silent-saves 1.1.0 (#1380) stopped the compiled MockMed program on every item
of its display-scaling block: at 125% the recorded region's content sits 25%
further from the top-left corner and is 25% larger, outside the check's
search window and scale band, so a correct screen failed. The check now also
looks at the scale this step's own click showed, when both axes agree, with
the same thresholds. A covered or missing region still fails.

No browser: synthetic frames and the real matcher.
"""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw

import openadapt_flow.vision as vision
from openadapt_flow.ir import Anchor, Postcondition, PostconditionKind
from openadapt_flow.runtime.replayer import Replayer, _uniform_page_scale
from tests.test_replayer import FakeBackend

VIEWPORT = (1280, 800)
REGION = (40, 120, 520, 260)


def _page(scale: float = 1.0, *, modal: bool = False) -> Image.Image:
    """A form-like page; ``scale`` zooms it from the top-left corner."""

    w, h = int(VIEWPORT[0] / scale) + 2, int(VIEWPORT[1] / scale) + 2
    page = Image.new("RGB", (w, h), (246, 248, 250))
    draw = ImageDraw.Draw(page)
    rng = np.random.default_rng(7)
    x0, y0, rw, rh = REGION
    draw.rectangle((x0, y0, x0 + rw, y0 + rh), outline=(30, 60, 90), width=3)
    for i in range(9):
        y = y0 + 18 + i * 26
        width = int(rng.integers(120, rw - 40))
        draw.rectangle((x0 + 16, y, x0 + 16 + width, y + 12), fill=(40, 40, 40))
        draw.rectangle((x0 + rw - 90, y, x0 + rw - 20, y + 16), outline=(0, 90, 160))
    if scale != 1.0:
        page = page.resize((round(w * scale), round(h * scale)), Image.BILINEAR)
    page = page.crop((0, 0, *VIEWPORT))
    if modal:
        sx = scale
        ImageDraw.Draw(page).rectangle(
            (
                int(x0 * sx) + 30,
                int(y0 * sx) + 20,
                int((x0 + rw) * sx),
                int((y0 + rh) * sx),
            ),
            fill=(255, 255, 255),
            outline=(0, 0, 0),
        )
    return page


def _png(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


@pytest.fixture()
def check(tmp_path: Path):
    base = _png(_page())
    x0, y0, rw, rh = REGION
    crop = _page().crop((x0, y0, x0 + rw, y0 + rh))
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "expect.png").write_bytes(_png(crop))
    pc = Postcondition(
        kind=PostconditionKind.REGION_STABLE,
        region=REGION,
        phash=vision.phash_png(base, region=REGION),
        template="templates/expect.png",
    )
    replayer = Replayer(FakeBackend(), vision=vision)

    def passes(frame: bytes, scale) -> bool:
        replayer._pc_page_scale = scale
        replayer._pc_scaled_match = False
        try:
            return replayer._postcondition_passes(pc, frame, tmp_path)
        finally:
            replayer._pc_page_scale = None

    return passes


def test_the_recorded_page_passes_without_a_scale(check) -> None:
    assert check(_png(_page()), None)


def test_a_zoomed_page_fails_without_its_scale(check) -> None:
    assert not check(_png(_page(1.25)), None)


def test_a_zoomed_page_passes_at_the_scale_its_click_showed(check) -> None:
    assert check(_png(_page(1.25)), 1.25)
    # One pixel of click-point noise is within the band.
    assert check(_png(_page(1.25)), 1.253)


def test_a_covered_region_still_fails_at_that_scale(check) -> None:
    assert not check(_png(_page(1.25, modal=True)), 1.25)


def test_another_scale_does_not_stand_in(check) -> None:
    assert not check(_png(_page(1.25)), 1.6)


def _step(click):
    return SimpleNamespace(
        anchor=Anchor(template="t.png", region=(0, 0, 10, 10), click_point=click)
    )


def _result(point):
    return SimpleNamespace(resolution=SimpleNamespace(point=point))


@pytest.mark.parametrize(
    ("click", "point", "scale"),
    [
        ((775, 186), (969, 232), 1.249),  # MockMed at 125%, recorded vs live
        ((343, 350), (430, 438), 1.253),
        ((400, 300), (400, 300), None),  # no zoom
        ((400, 300), (520, 300), None),  # moved right only: not a zoom
        ((400, 300), (560, 330), None),  # axes disagree
        ((30, 300), (38, 375), None),  # too close to the edge to tell
        ((400, 300), (1000, 750), None),  # beyond the bounds
    ],
)
def test_a_page_scale_needs_both_axes_to_agree(click, point, scale) -> None:
    assert _uniform_page_scale(_step(click), _result(point)) == scale


def test_no_resolution_means_no_scale() -> None:
    assert (
        _uniform_page_scale(_step((400, 300)), SimpleNamespace(resolution=None)) is None
    )

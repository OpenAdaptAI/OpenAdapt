"""Pixel identity tier vs. an identity band that embeds a run parameter.

Regression for a false right-record stop seen on a real OpenEMR run (made-up
patients). The demonstration typed First Name "Elena"; the next step (click
Last Name) armed identity on the form row, so its recorded identifier crop
shows the DEMONSTRATION's first name. The identity template records that the
band embeds parameter ``fname``. On replay with a different first name the
pixel tier compared the demo's pixels with the run's pixels, and the verdict
depended on the value: "Jonah" happened to abstain (two similar spikes) and
the OCR parameter check verified it; "Rosa" produced one uniquely localized
spike and the pixel tier reported a different identifier, halting a correct
run 2 of 2 times.

The pixel tier compares recorded pixels with live pixels. That is meaningful
only for content that should render the same on every run. When the band's
run value of an embedded parameter differs from the demonstrated value, the
recorded crop cannot show the run's entity, so the tier now defers and the
OCR parameter check (which substitutes the run's value into the recorded band)
decides. The check still stops on a different patient, a mismatch where the
run value equals the demonstration value still halts in the pixel tier, and a
band that rests on a glyph-confusable identifier keeps the pixel tier.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from openadapt_flow.ir import ActionKind, Anchor, Resolution, Step, Workflow
from openadapt_flow.runtime import identity as I
from openadapt_flow.runtime.identity_template import build_identity_template
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.vision.ocr import OcrLine

_W, _H = 711, 25
_CLICK = (456, 12)
_DEMO_FIRST = "Elena"
# One glyph away from the demonstrated value: a legitimate different first
# name that renders as a single, uniquely localized pixel spike, so the pixel
# tier alone reports "a different identifier" deterministically.
_RUN_FIRST = "Elana"


def _font(size: int):
    from PIL import ImageFont

    try:
        import matplotlib.font_manager as fm

        path = fm.findfont("DejaVu Sans", fallback_to_default=False)
    except Exception:  # pragma: no cover - dev extra provides matplotlib
        path = None
    if not path or not Path(path).exists():
        pytest.skip("no deterministic scalable TrueType font available")
    return ImageFont.truetype(path, size)


# (text, x) for every label/value on the rendered form row. The first-name
# value sits in its own input box; the rest is fixed form chrome.
def _row_items(first: str) -> list[tuple[str, int]]:
    return [
        ("Name:", 4),
        (first, 168),
        ("Middle", 302),
        ("Last Name", 380),
        ("Name", 600),
    ]


def _row_png(first: str) -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("L", (_W, _H), 255)
    draw = ImageDraw.Draw(img)
    font = _font(14)
    draw.rectangle((160, 1, 290, 23), outline=120)
    draw.rectangle((296, 1, 360, 23), outline=120)
    draw.rectangle((372, 1, 540, 23), outline=120)
    for text, x in _row_items(first):
        draw.text((x, 4), text, fill=20, font=font)
    ok, buf = cv2.imencode(".png", np.array(img))
    assert ok
    return buf.tobytes()


def _band_text(first: str) -> str:
    return " ".join(text for text, _ in _row_items(first))


class _RowOCR:
    """OCR stand-in that reads the row the live frame shows."""

    def __init__(self, first: str) -> None:
        self.lines = [
            OcrLine(text=text, region=(x, 4, 8 * len(text), 16), confidence=0.99)
            for text, x in _row_items(first)
        ]

    def ocr(self, png: bytes, *, region=None):
        return list(self.lines)


class _Backend:
    viewport = (_W, _H)

    def screenshot(self) -> bytes:  # pragma: no cover - not reached
        return _row_png(_DEMO_FIRST)


def _bundle(tmp_path: Path, *, plaintext: bool, band_first: str = _DEMO_FIRST):
    """A one-step bundle shaped like the compiled OpenEMR 'click Last Name'."""
    (tmp_path / "templates" / "identifiers").mkdir(parents=True, exist_ok=True)
    (tmp_path / "templates" / "t.png").write_bytes(_row_png(_DEMO_FIRST))
    (tmp_path / "templates" / "identifiers" / "s1.png").write_bytes(
        _row_png(band_first)
    )
    workflow = Workflow(name="wf", params={"fname": _DEMO_FIRST})
    band = _band_text(band_first)
    anchor_kwargs: dict = {}
    if plaintext:
        anchor_kwargs["context_text"] = band
    else:
        template = build_identity_template(band, param_examples=workflow.params)
        assert template is not None
        assert template.param_token_indices.get("fname"), (
            "the identity band must embed the first-name parameter"
        )
        anchor_kwargs["identity_template"] = template
    step = Step(
        id="s1",
        intent="click 'Last Name'",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template="templates/t.png",
            region=(380, 0, 160, _H),
            click_point=_CLICK,
            identifier_crop="templates/identifiers/s1.png",
            identifier_region=(0, 0, _W, _H),
            **anchor_kwargs,
        ),
    )
    workflow.steps = [step]
    resolution = Resolution(
        rung="structural", point=_CLICK, confidence=1.0, elapsed_ms=1.0
    )
    return workflow, step, resolution


def _check(tmp_path: Path, *, shown: str, run_value: str, plaintext: bool):
    workflow, step, resolution = _bundle(tmp_path, plaintext=plaintext)
    replayer = Replayer(_Backend(), vision=_RowOCR(shown), poll_interval_s=0.01)
    return replayer._verify_identity(
        step,
        resolution,
        _row_png(shown),
        {"fname": run_value},
        workflow,
        tmp_path,
    )


def test_precondition_pixel_tier_alone_calls_the_run_value_a_different_identifier():
    # The input that used to halt: the recorded crop shows the demo's first
    # name, the live crop the run's. On its own the pixel tier reports a
    # localized glyph change, exactly like the OpenEMR "Rosa" trials.
    verdict = I.verify_pixel_identity(_row_png(_DEMO_FIRST), _row_png(_RUN_FIRST))
    assert verdict is not None and verdict.status == "mismatch"


@pytest.mark.parametrize("plaintext", [False, True], ids=["template", "plaintext"])
def test_run_value_in_identity_band_is_not_judged_against_demo_pixels(
    tmp_path: Path, plaintext: bool
) -> None:
    check = _check(
        tmp_path, shown=_RUN_FIRST, run_value=_RUN_FIRST, plaintext=plaintext
    )
    assert check.mode == "param"
    assert check.status == "verified"
    assert check.param == "fname"


@pytest.mark.parametrize("plaintext", [False, True], ids=["template", "plaintext"])
def test_still_stops_on_a_different_patient(tmp_path: Path, plaintext: bool) -> None:
    # The run is for "Elana" but the screen shows a different person's first
    # name: the OCR parameter check must refuse.
    check = _check(tmp_path, shown="Maria", run_value=_RUN_FIRST, plaintext=plaintext)
    assert check.status == "mismatch"


@pytest.mark.parametrize("plaintext", [False, True], ids=["template", "plaintext"])
def test_still_stops_when_the_demonstrated_patient_is_shown(
    tmp_path: Path, plaintext: bool
) -> None:
    # The screen still shows the demonstration's patient instead of the run's.
    check = _check(
        tmp_path, shown=_DEMO_FIRST, run_value=_RUN_FIRST, plaintext=plaintext
    )
    assert check.status == "mismatch"


@pytest.mark.parametrize("plaintext", [False, True], ids=["template", "plaintext"])
def test_pixel_tier_still_judges_when_run_value_equals_demo_value(
    tmp_path: Path, plaintext: bool
) -> None:
    # Same first name as the demonstration: the recorded crop is a valid
    # reference, so a localized pixel change is still a pixel mismatch even
    # though OCR reads the expected name.
    workflow, step, resolution = _bundle(tmp_path, plaintext=plaintext)
    replayer = Replayer(_Backend(), vision=_RowOCR(_DEMO_FIRST), poll_interval_s=0.01)
    check = replayer._verify_identity(
        step,
        resolution,
        _row_png(_RUN_FIRST),
        {"fname": _DEMO_FIRST},
        workflow,
        tmp_path,
    )
    assert check.mode == "pixel"
    assert check.status == "mismatch"


def test_confusable_identifier_band_keeps_the_pixel_tier(tmp_path: Path) -> None:
    # A band that rests on a glyph-confusable identifier (an MRN with O/0)
    # keeps the pixel comparison, even when a parameter in it changed: OCR
    # cannot settle O versus 0, so the safer pixel verdict stands.
    workflow = Workflow(name="wf", params={"fname": _DEMO_FIRST})
    band = f"MRN MG44O8 {_band_text(_DEMO_FIRST)}"
    template = build_identity_template(band, param_examples=workflow.params)
    assert template is not None and template.rests_on_confusable_identifier
    (tmp_path / "templates" / "identifiers").mkdir(parents=True, exist_ok=True)
    (tmp_path / "templates" / "t.png").write_bytes(_row_png(_DEMO_FIRST))
    (tmp_path / "templates" / "identifiers" / "s1.png").write_bytes(
        _row_png(_DEMO_FIRST)
    )
    step = Step(
        id="s1",
        intent="click 'Last Name'",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template="templates/t.png",
            region=(380, 0, 160, _H),
            click_point=_CLICK,
            identifier_crop="templates/identifiers/s1.png",
            identifier_region=(0, 0, _W, _H),
            identity_template=template,
        ),
    )
    resolution = Resolution(
        rung="structural", point=_CLICK, confidence=1.0, elapsed_ms=1.0
    )
    replayer = Replayer(_Backend(), vision=_RowOCR(_RUN_FIRST), poll_interval_s=0.01)
    check = replayer._verify_identity(
        step,
        resolution,
        _row_png(_RUN_FIRST),
        {"fname": _RUN_FIRST},
        workflow,
        tmp_path,
    )
    assert check.mode == "pixel"
    assert check.status == "mismatch"


def test_run_bound_identity_params_helper() -> None:
    examples = {"fname": "Elena", "lname": "Marsh"}
    assert I.run_bound_identity_params(["fname"], {"fname": "Rosa"}, examples) == [
        "fname"
    ]
    # Same value as the demonstration, or no run value at all: not run-bound.
    assert I.run_bound_identity_params(["fname"], {"fname": "Elena"}, examples) == []
    assert I.run_bound_identity_params(["fname"], {}, examples) == []
    # A parameter the band does not embed never counts.
    assert I.run_bound_identity_params([], {"lname": "Pell"}, examples) == []

"""Apple Vision read strategy (strips, short-image upscale, upright re-read)
and the identity tier's row re-read. No Vision needed: ``_observe`` is faked."""

from __future__ import annotations

import io
import sys
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from PIL import Image, ImageDraw

from openadapt_flow.runtime import identity as I
from openadapt_flow.vision import ocr_backends as ob


def _png(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


class _Engine(ob.AppleVisionEngine):
    """AppleVisionEngine with Vision replaced by a bar detector.

    Each dark horizontal bar in the image comes back as one line whose text
    is the bar's height in the ORIGINAL image (bars are drawn 10 px tall, so
    the text says how the image was scaled). ``flip_first`` makes the default
    revision report every line upside down.
    """

    def __init__(self, flip_first: bool = False) -> None:  # no super(): no objc
        self._revision = 3
        self._upright_revision = 2
        self.flip_first = flip_first
        self.reads: list[tuple[tuple[int, int], Optional[int]]] = []

    def _observe(
        self, png: bytes, width: int, height: int, revision: Optional[int]
    ) -> list[tuple[ob.RawLine, bool]]:
        with Image.open(io.BytesIO(png)) as image:
            gray = image.convert("L")
            assert gray.size == (width, height)
            self.reads.append((gray.size, revision))
            column = [gray.getpixel((width // 2, y)) for y in range(height)]
        lines: list[tuple[ob.RawLine, bool]] = []
        y = 0
        while y < height:
            if column[y] < 128:
                start = y
                while y < height and column[y] < 128:
                    y += 1
                bar_h = y - start
                # A bar cut by the image edge is a partial read.
                text = "cut" if start == 0 or y == height else f"bar{bar_h}"
                flipped = self.flip_first and revision is None
                lines.append(((0, start, width, bar_h, text, 1.0), flipped))
            y += 1
        return lines


def _bars(width: int, height: int, ys: list[int]) -> bytes:
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    for y in ys:
        draw.rectangle((0, y, width - 1, y + 9), fill="black")
    return _png(image)


def test_tall_image_is_read_in_strips_and_each_line_kept_once() -> None:
    ys = list(range(40, 3600, 120))
    engine = _Engine()
    lines = engine.recognize_png(_bars(400, 3702, ys))
    assert len(engine.reads) > 1
    strip_heights = [size[1] for size, _ in engine.reads]
    assert max(strip_heights) <= engine.STRIP_HEIGHT + 2 * engine.STRIP_OVERLAP + 1
    assert sorted(line[1] for line in lines) == ys  # every bar once, in frame y
    assert {line[4] for line in lines} == {"bar10"}  # never a cut copy


def test_image_of_ordinary_height_is_read_once() -> None:
    engine = _Engine()
    lines = engine.recognize_png(_bars(400, 600, [100, 300]))
    assert engine.reads == [((400, 600), None)]
    assert [line[1] for line in lines] == [100, 300]


def test_short_image_is_upscaled_and_boxes_map_back() -> None:
    engine = _Engine()
    lines = engine.recognize_png(_bars(300, 64, [20]))
    assert engine.reads == [((600, 128), None)]
    assert [line[4] for line in lines] == ["bar20"]  # read at 2x
    x, y, w, h = lines[0][:4]
    assert (x, y, w) == (0, 20, 300) and 9 <= h <= 11


def test_upside_down_read_is_redone_at_the_upright_revision() -> None:
    engine = _Engine(flip_first=True)
    engine.recognize_png(_bars(400, 600, [100]))
    assert [revision for _, revision in engine.reads] == [None, 2]


def test_upright_read_is_not_redone() -> None:
    engine = _Engine()
    engine.recognize_png(_bars(400, 600, [100]))
    assert [revision for _, revision in engine.reads] == [None]


def test_no_upright_revision_keeps_the_first_read() -> None:
    engine = _Engine(flip_first=True)
    engine._upright_revision = None
    engine.recognize_png(_bars(400, 600, [100]))
    assert [revision for _, revision in engine.reads] == [None]


def _line(y: int, h: int) -> Any:
    return SimpleNamespace(region=(10, y, 50, h), text="x")


def test_row_region_spans_band_width_and_pads_the_row() -> None:
    band = (0, 100, 1120, 64)
    row = I.row_region([_line(124, 14), _line(122, 18), _line(125, 12)], band)
    # median height 14 -> 7 px pad; rows span 122..140
    assert row == (0, 115, 1120, 32)


def test_row_region_stays_inside_the_band() -> None:
    band = (0, 100, 1120, 64)
    row = I.row_region([_line(101, 30), _line(140, 22)], band)
    assert row is not None
    assert row[1] >= 100 and row[1] + row[3] <= 164


def test_row_region_without_lines_is_none() -> None:
    assert I.row_region([], (0, 0, 10, 10)) is None


# ---------------------------------------------------------------------------
# Replayer: the row re-read is the last attempt and follows the same rules
# ---------------------------------------------------------------------------


class _Vision:
    """OCR that answers by the image it is given.

    The full frame's band returns the target row with the status column
    missing (as Apple Vision did on a 64px band of a dense table). A crop
    whose height says it is the row alone returns the whole row.
    """

    def __init__(self, row_text: str, band_text: str) -> None:
        self.row_text = row_text
        self.band_text = band_text
        self.calls: list[tuple[tuple[int, int], Any]] = []

    def ocr(self, png: bytes, region: Any = None) -> list[Any]:
        from openadapt_flow.vision.ocr import OcrLine

        with Image.open(io.BytesIO(png)) as image:
            size = image.size
        self.calls.append((size, region))
        if region is not None:  # first pass: the band of the full frame
            return [
                OcrLine(text=self.band_text, region=(300, 112, 400, 16), confidence=1.0)
            ]
        if size[1] > 100:  # the 2x band retry
            return [
                OcrLine(text=self.band_text, region=(600, 48, 800, 32), confidence=1.0)
            ]
        # the row re-read (row height ~32 px at 2x)
        h = size[1]
        return [
            OcrLine(
                text=self.row_text, region=(600, h // 4, 800, h // 2), confidence=1.0
            )
        ]


def _replayer_check(row_text: str, band_text: str, recorded: str) -> tuple[str, int]:
    from openadapt_flow.ir import ActionKind, Anchor, Resolution, Step, Workflow
    from openadapt_flow.runtime.replayer import Replayer

    frame = _png(Image.new("RGB", (1120, 400), "white"))
    anchor = Anchor(
        region=(20, 88, 160, 64),
        click_point=(100, 120),
        template="templates/open.png",
        ocr_text="Open",
        context_text=recorded,
    )
    step = Step(id="open", intent="Open chart", action=ActionKind.CLICK, anchor=anchor)
    workflow = Workflow(name="row", viewport=(1120, 400), steps=[step])
    vision = _Vision(row_text, band_text)
    replayer = Replayer(SimpleNamespace(viewport=(1120, 400)), vision=vision)
    check = replayer._verify_identity_ocr(
        step,
        Resolution(rung="template", point=(100, 120), confidence=1.0, elapsed_ms=0.0),
        frame,
        {},
        workflow,
    )
    return check.status, len(vision.calls)


RECORDED = "MG367459 Sorensen, Philip 1957-10-28 M Active"


def test_row_reread_recovers_a_row_the_band_read_short() -> None:
    status, calls = _replayer_check(
        RECORDED, "MG367459 Sorensen, Philip 1957-10-28", RECORDED
    )
    assert calls == 3
    assert status == "verified"


def test_row_reread_of_a_different_patient_does_not_verify() -> None:
    sibling = "MG619501 Sorenson, Philip 1957-10-28 M Active"
    status, calls = _replayer_check(
        sibling, "MG619501 Sorenson, Philip 1957-10-28", RECORDED
    )
    assert calls == 3
    assert status != "verified"


def test_verified_band_skips_the_row_reread() -> None:
    status, calls = _replayer_check("ignored", RECORDED, RECORDED)
    assert status == "verified" and calls == 1


@pytest.mark.skipif(sys.platform != "darwin", reason="Apple Vision is macOS-only")
def test_apple_vision_reads_every_column_of_a_tall_frame() -> None:
    """The record-frame failure: a tall dense table lost a whole column."""
    pytest.importorskip("objc")
    from PIL import ImageFont

    try:
        font: Any = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 30)
    except OSError:
        pytest.skip("no Helvetica")
    image = Image.new("RGB", (2240, 3702), "white")
    draw = ImageDraw.Draw(image)
    rows = 50
    for k in range(rows):
        y = 200 + k * 68
        draw.text((20, y), f"MG{600000 + k}", fill="black", font=font)
        draw.text((350, y), "Sorensen, Philip", fill="black", font=font)
        draw.text((880, y), "1970-05-04", fill="black", font=font)
        draw.text(
            (1400, y), "Inactive" if k % 2 else "Pending", fill="black", font=font
        )
    lines = ob.AppleVisionEngine().recognize_png(_png(image))
    status = [line for line in lines if line[4] in ("Inactive", "Pending")]
    assert len(status) >= rows - 2

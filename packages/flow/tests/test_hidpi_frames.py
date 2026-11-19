"""The benchmark's 2x display copies leave the engine's 1x frames unchanged."""

from __future__ import annotations

import hashlib
import io

import pytest
from PIL import Image
from playwright.sync_api import sync_playwright

from benchmark.openemr_local import hidpi_frames
from openadapt_flow.backends.playwright_backend import PlaywrightBackend

PAGE = """<html><body style="font:13px Arial;margin:24px">
<h1>New patient</h1><p>Ines Carvalho, 1990-01-01 (made up)</p>
<script>
window.events = [];
addEventListener('resize', () => events.push('resize'));
matchMedia('(resolution: 2dppx)').addEventListener('change', () => events.push('dppx'));
</script></body></html>"""


@pytest.fixture()
def page():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 640, "height": 400}, device_scale_factor=1
        )
        page = context.new_page()
        page.set_content(PAGE)
        yield page
        browser.close()


def test_copy_is_2x_and_page_sees_nothing(page, tmp_path):
    backend = PlaywrightBackend(page)
    plain = backend.screenshot()
    page.evaluate("window.events.length = 0")  # the load's own resize
    hidpi_frames.attach(backend, tmp_path / "copies")
    frame = backend.screenshot()
    again = backend.screenshot()

    assert frame == plain == again  # the engine's frame is unchanged
    assert Image.open(io.BytesIO(frame)).size == (640, 400)
    copies = list((tmp_path / "copies").glob("*.png"))
    assert [c.stem for c in copies] == [hashlib.sha256(frame).hexdigest()]
    assert Image.open(copies[0]).size == (1280, 800)
    assert page.evaluate("window.events") == []
    assert page.evaluate("window.devicePixelRatio") == 1


def test_pair_writes_matching_copies_and_refuses_others(page, tmp_path):
    backend = PlaywrightBackend(page)
    hidpi_frames.attach(backend, tmp_path / "copies")
    run = tmp_path / "run"
    (run / "steps").mkdir(parents=True)
    (run / "steps" / "step_000_after.png").write_bytes(backend.screenshot())
    page.evaluate("document.body.innerHTML = '<h1>Saved</h1>'")
    (run / "steps" / "step_001_after.png").write_bytes(backend.screenshot())
    # A frame the backend never returned has no copy.
    blank = io.BytesIO()
    Image.new("RGB", (640, 400), "white").save(blank, format="PNG")
    (run / "steps" / "step_002_after.png").write_bytes(blank.getvalue())
    # A copy of a different screen is refused.
    wrong = (
        tmp_path / "copies" / (hashlib.sha256(blank.getvalue()).hexdigest() + ".png")
    )
    black = io.BytesIO()
    Image.new("RGB", (1280, 800), "black").save(black, format="PNG")
    wrong.write_bytes(black.getvalue())

    with pytest.raises(ValueError, match="step_002_after"):
        hidpi_frames.pair(run, tmp_path / "copies", tmp_path / "out")
    summary = hidpi_frames.pair(
        run, tmp_path / "copies", tmp_path / "out", strict=False
    )

    assert [p["copy"] for p in summary["pairs"]] == [
        "step_000_after@2x.png",
        "step_001_after@2x.png",
    ]
    assert all(p["copy_size"] == [1280, 800] for p in summary["pairs"])
    assert [m["frame"] for m in summary["missing"]] == ["step_002_after.png"]
    assert summary["missing"][0]["reason"].startswith("differs")
    assert (tmp_path / "out" / "pairs.json").exists()

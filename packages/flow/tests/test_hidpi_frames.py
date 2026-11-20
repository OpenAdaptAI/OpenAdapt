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


@pytest.mark.parametrize("method", ["screenshot", "guarded_keyboard_frame"])
def test_copy_is_2x_and_page_sees_nothing(page, tmp_path, method):
    backend = PlaywrightBackend(page)
    plain = getattr(backend, method)()
    page.evaluate("window.events.length = 0")  # the load's own resize
    hidpi_frames.attach(backend, tmp_path / "copies")
    frame = getattr(backend, method)()
    again = getattr(backend, method)()

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


R6_STOP = (
    "Postconditions failed for step 'step_039' (click 'Create New Patient'): "
    "expected screen state not reached (semantic drift) — failed: text_present "
    "'No matches were found.' — run aborted"
)


def _row(expected, outcome, new=0, changed=0):
    return {
        "expected": expected,
        "transaction_outcome": outcome,
        "new_charts": new,
        "changed_charts": changed,
    }


def test_runner_accepts_only_the_landing_demo_outcomes():
    from benchmark.openemr_local.certified_referrals import mismatches

    assert mismatches(_row("VERIFIED", "VERIFIED", new=1), None) == []
    assert mismatches(_row("VERIFIED", "HALTED_BEFORE_EFFECT"), None)
    assert mismatches(_row("VERIFIED", "VERIFIED", new=2), None)
    stop = _row("HALTED_BEFORE_EFFECT", "HALTED_BEFORE_EFFECT")
    assert mismatches(stop, R6_STOP) == []
    # A halt at the same step without the dialog is not the duplicate stop.
    no_dialog = R6_STOP.replace(
        "'No matches were found.'",
        "'Confirm Create New Patient', text_present 'No matches were found.'",
    )
    assert mismatches(stop, no_dialog)
    # Nor is a halt elsewhere, or one for another reason at that step.
    assert mismatches(stop, R6_STOP.replace("step_039", "step_009"))
    assert mismatches(
        stop, "Identity check failed for step 'step_039' (click 'Create New Patient')"
    )
    assert mismatches(stop, None)


def test_transient_capture_failure_retries_the_same_frame(page, tmp_path, monkeypatch):
    backend = PlaywrightBackend(page)
    capture = hidpi_frames.capture_scaled
    attempts = []

    def flaky(page, scale):
        attempts.append(scale)
        if len(attempts) == 1:
            raise RuntimeError("injected transient capture fault")
        return capture(page, scale)

    monkeypatch.setattr(hidpi_frames, "capture_scaled", flaky)
    hidpi_frames.attach(backend, tmp_path / "copies")
    first = backend.screenshot()
    assert list((tmp_path / "copies").glob("*.error"))
    second = backend.screenshot()
    assert first == second
    assert attempts == [2, 2]
    assert not list((tmp_path / "copies").glob("*.error"))
    assert len(list((tmp_path / "copies").glob("*.png"))) == 1

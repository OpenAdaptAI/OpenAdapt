"""The identity band of a target inside a dialog is read on the dialog alone.

OpenEMR's "Confirm Create New Patient" sits in a Bootstrap modal over the New
Patient form. The OCR identity band is a full-width strip, so it also read a
form label beside the modal: "County:" in one run, "Emergency Contact:" in
another, depending on where the form behind happened to be scrolled. The check
then refused a correct save every 9 to 17 items (#1347).

The recorder now stores the box of the topmost dialog holding the target, the
compiler reads the band on the frame with everything outside that box blanked,
and replay does the same on the live dialog. No dialog at the point fails the
check closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from openadapt_flow.compiler.compile import _event_dialog_region
from openadapt_flow.ir import Anchor, IdentityCheck
from openadapt_flow.runtime import identity as identity_mod
from openadapt_flow.runtime.replayer import Replayer

# A form behind a Bootstrap-style modal: the overlay covers the viewport and
# the visible box is its .modal-dialog. The dialog's buttons live in a
# same-origin iframe, as OpenEMR's dlgopen dialogs do.
_PAGE_HTML = """<!doctype html><html><body style="margin:0;font:14px sans-serif">
<form style="padding:20px">
 <div style="height:40px">County: <input></div>
 <div style="height:40px">Emergency Contact: <input></div>
 <div style="height:40px">Home Phone: <input></div>
</form>
<div class="modal show" role="dialog" aria-modal="true"
     style="position:fixed;inset:0;background:rgba(0,0,0,.4);display:block">
 <div class="modal-dialog" id="box"
      style="position:absolute;left:300px;top:60px;width:400px;height:200px;
             background:#fff">
  <div class="modal-header">Possible duplicates</div>
  <iframe id="body" style="border:0;width:380px;height:150px"
   srcdoc="<body style='margin:0'><p>No matches were found.</p>
   <button id='confirm' style='width:220px;height:30px'>Confirm Create New Patient</button>
   <button>Cancel</button></body>"></iframe>
 </div>
</div>
<button id="outside" style="position:fixed;left:10px;top:10px">Outside</button>
</body></html>"""


@pytest.fixture(scope="module")
def page_backend():
    sync = pytest.importorskip("playwright.sync_api")
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    with sync.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - no browser installed
            pytest.skip(f"chromium unavailable: {exc}")
        page = browser.new_page(
            viewport={"width": 1000, "height": 400}, device_scale_factor=1
        )
        page.set_content(_PAGE_HTML, wait_until="load")
        page.frame_locator("#body").locator("#confirm").wait_for()
        yield page, PlaywrightBackend(page)
        browser.close()


def _center(page, selector: str, frame: str | None = None) -> tuple[int, int]:
    js = (
        "el => { const r = el.getBoundingClientRect();"
        " return [r.x + r.width / 2, r.y + r.height / 2]; }"
    )
    if frame is None:
        x, y = page.eval_on_selector(selector, js)
        return int(x), int(y)
    fx, fy = page.eval_on_selector(
        frame, "el => { const r = el.getBoundingClientRect(); return [r.x, r.y]; }"
    )
    x, y = page.frame_locator(frame).locator(selector).evaluate(js)
    return int(fx + x), int(fy + y)


def test_dialog_box_is_the_visible_dialog_not_the_overlay(page_backend) -> None:
    page, backend = page_backend
    status, box = backend.dialog_region_at(*_center(page, "#confirm", "#body"))
    assert status == "dialog"
    assert box == (300, 60, 400, 200)


def test_point_on_the_overlay_outside_the_box_reads_the_dialog(page_backend) -> None:
    page, backend = page_backend
    # The overlay itself is the dialog element; every child box covers less
    # than the viewport, so the overlay's own rect is the scope.
    status, box = backend.dialog_region_at(100, 300)
    assert status == "dialog"
    assert box == (0, 0, 1000, 400)


def test_fading_out_modal_is_not_a_dialog(page_backend) -> None:
    page, backend = page_backend
    page.evaluate("document.querySelector('.modal').style.opacity = '0'")
    try:
        status, _ = backend.dialog_region_at(*_center(page, "#confirm", "#body"))
        assert status == "none"
    finally:
        page.evaluate("document.querySelector('.modal').style.opacity = '1'")


def test_non_modal_popover_is_not_a_dialog(page_backend) -> None:
    page, backend = page_backend
    page.evaluate("document.querySelector('.modal').style.display = 'none'")
    page.evaluate(
        "document.body.insertAdjacentHTML('beforeend', '<div id=pop role=dialog"
        ' style="position:fixed;left:20px;top:300px;width:200px;height:60px">'
        "<button id=yes>Delete?</button></div>')"
    )
    try:
        assert backend.dialog_region_at(*_center(page, "#yes")) == ("none", None)
    finally:
        page.evaluate("document.getElementById('pop').remove()")
        page.evaluate("document.querySelector('.modal').style.display = 'block'")


def test_no_dialog_open_is_none(page_backend) -> None:
    page, backend = page_backend
    page.evaluate("document.querySelector('.modal').style.display = 'none'")
    try:
        assert backend.dialog_region_at(*_center(page, "#outside")) == ("none", None)
    finally:
        page.evaluate("document.querySelector('.modal').style.display = 'block'")


@dataclass
class _Line:
    text: str
    region: tuple[int, int, int, int]
    confidence: float = 0.99


def test_band_scope_drops_the_label_behind_the_dialog() -> None:
    lines = [
        _Line("County:", (20, 160, 70, 20)),
        _Line("Confirm Create New Patient", (310, 160, 220, 20)),
        _Line("Cancel", (560, 160, 60, 20)),
    ]
    band = identity_mod.band_region((420, 170), 64, (1000, 400))
    exclude = (0, 0, 1, 1)
    full = identity_mod.context_from_lines(
        lines, exclude_region=exclude, band=band, point=(420, 170)
    )
    scoped = identity_mod.context_from_lines(
        lines,
        exclude_region=exclude,
        band=band,
        point=(420, 170),
        scope=(300, 60, 400, 200),
    )
    assert full == "County: Confirm Create New Patient Cancel"
    assert scoped == "Confirm Create New Patient Cancel"


def _png(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def test_mask_outside_keeps_only_the_dialog_pixels() -> None:
    img = np.zeros((40, 60, 3), np.uint8)
    out = identity_mod.mask_outside(_png(img), (10, 5, 20, 10))
    assert out is not None
    got = cv2.imdecode(np.frombuffer(out, np.uint8), cv2.IMREAD_COLOR)
    assert (got[5:15, 10:30] == 0).all()
    got[5:15, 10:30] = 255
    assert (got == 255).all()
    assert identity_mod.mask_outside(_png(img), (100, 100, 5, 5)) is None


def test_event_dialog_region_is_validated_and_clamped() -> None:
    assert _event_dialog_region(
        {"dialog_region": [300, 60, 400, 200]}, (1000, 400)
    ) == (
        300,
        60,
        400,
        200,
    )
    assert _event_dialog_region(
        {"dialog_region": [900, 300, 400, 200]}, (1000, 400)
    ) == (
        900,
        300,
        100,
        100,
    )
    assert _event_dialog_region({}, (1000, 400)) is None
    assert _event_dialog_region({"dialog_region": [1, 2, 3]}, (1000, 400)) is None
    assert _event_dialog_region({"dialog_region": [0, 0, 0, 5]}, (1000, 400)) is None
    assert _event_dialog_region({"dialog_region": [True, 0, 5, 5]}, (1000, 400)) is None


def test_anchor_without_dialog_serializes_as_before() -> None:
    anchor = Anchor(template="t.png", region=(0, 0, 10, 10), click_point=(5, 5))
    assert "dialog_region" not in anchor.model_dump()
    scoped = anchor.model_copy(update={"dialog_region": (1, 2, 3, 4)})
    assert scoped.model_dump()["dialog_region"] == (1, 2, 3, 4)


class _Probe:
    def __init__(self, answer):
        self.answer = answer

    def dialog_region_at(self, x, y):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _scoped(backend, png: bytes):
    replayer = Replayer.__new__(Replayer)
    replayer.backend = backend
    return Replayer._dialog_scoped_frame(
        replayer, SimpleNamespace(point=(400, 150)), png
    )


@pytest.mark.parametrize(
    "backend",
    [
        _Probe(("none", None)),
        _Probe(("unknown", None)),
        _Probe(RuntimeError("page closed")),
        SimpleNamespace(),  # a backend that cannot see dialogs
    ],
)
def test_no_live_dialog_fails_the_identity_check_closed(backend) -> None:
    result = _scoped(backend, _png(np.zeros((400, 1000, 3), np.uint8)))
    assert isinstance(result, IdentityCheck)
    assert result.status == "mismatch"


def test_live_dialog_masks_the_frame() -> None:
    result = _scoped(
        _Probe(("dialog", (300, 60, 400, 200))),
        _png(np.zeros((400, 1000, 3), np.uint8)),
    )
    assert isinstance(result, bytes)
    got = cv2.imdecode(np.frombuffer(result, np.uint8), cv2.IMREAD_COLOR)
    assert (got[60:260, 300:700] == 0).all()
    assert (got[:60] == 255).all()


def test_render_scaled_view_maps_the_dialog_box_back() -> None:
    from openadapt_flow.runtime import render_scale as rs

    class Inner:
        viewport = (1250, 500)

        def screenshot(self):
            return b""

        def dialog_region_at(self, x, y):
            assert (x, y) == (500, 200)
            return ("dialog", (375, 75, 500, 250))

    transform = rs.RenderTransform(
        scale=1.25,
        offset=(0.0, 0.0),
        recorded_viewport=(1000, 400),
        live_viewport=(1250, 500),
        landmarks_used=5,
        landmarks_located=5,
        max_residual_px=1.0,
    )
    view = rs.render_scaled_backend(Inner(), transform)
    assert view.dialog_region_at(400, 160) == ("dialog", (300, 60, 400, 200))

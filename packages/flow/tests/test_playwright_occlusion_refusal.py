"""Tests for occluded / off-viewport element refusal (REALISM_AUDIT rank 17, #1479).

When a DOM element exists uniquely but is covered by an overlay or located outside
the viewport, locate_structural must raise a typed refusal rather than returning
None. Returning None causes resolver.py to fall through to pixel/template rungs,
which risks clicking the covering overlay or off-screen coordinates.
"""

from __future__ import annotations

import pytest

from openadapt_flow.backend import (
    StructuralElementNotActionableRefused,
    StructuralResolutionRefused,
)
from openadapt_flow.backends.playwright_backend import PlaywrightBackend
from openadapt_flow.ir import Anchor, StructuralLocator
from openadapt_flow.runtime.resolver import resolve
from tests.test_replayer import FakeVision, Match, make_png

_HTML = """<!doctype html>
<html>
<head>
<style>
  body { margin: 0; padding: 0; width: 640px; height: 400px; }
  #btn-normal { position: absolute; top: 50px; left: 50px; width: 100px; height: 40px; }
  #btn-covered { position: absolute; top: 150px; left: 50px; width: 100px; height: 40px; }
  #overlay {
    position: fixed;
    top: 130px;
    left: 30px;
    width: 200px;
    height: 100px;
    background: rgba(0, 0, 0, 0.7);
    z-index: 1000;
  }
  #btn-offscreen { position: absolute; top: 1500px; left: 50px; width: 100px; height: 40px; }
</style>
</head>
<body>
  <button id="btn-normal">Save Normal</button>
  <button id="btn-covered">Save Covered</button>
  <div id="overlay">Modal Overlay</div>
  <button id="btn-offscreen">Save Offscreen</button>
</body>
</html>
"""


@pytest.fixture(scope="module")
def page():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:
            pytest.skip(f"chromium unavailable: {exc}")
        page = browser.new_page(viewport={"width": 640, "height": 400})
        page.set_content(_HTML, wait_until="load")
        yield page
        browser.close()


def test_normal_element_resolves_handle(page) -> None:
    backend = PlaywrightBackend(page)
    loc = StructuralLocator(selector="#btn-normal")
    handle = backend.locate_structural(loc)
    assert handle is not None
    assert handle.point[0] == 100
    assert handle.point[1] == 70


def test_missing_element_returns_none(page) -> None:
    backend = PlaywrightBackend(page)
    loc = StructuralLocator(selector="#non-existent-button")
    assert backend.locate_structural(loc) is None


def test_covered_element_raises_not_actionable_refusal(page) -> None:
    backend = PlaywrightBackend(page)
    loc = StructuralLocator(selector="#btn-covered")
    with pytest.raises(StructuralElementNotActionableRefused, match="occluded|covered"):
        backend.locate_structural(loc)


def test_offscreen_element_raises_not_actionable_refusal(page) -> None:
    backend = PlaywrightBackend(page)
    loc = StructuralLocator(selector="#btn-offscreen")
    with pytest.raises(StructuralElementNotActionableRefused, match="outside the viewport"):
        backend.locate_structural(loc)


def test_covered_element_never_falls_through_to_template_rung(page) -> None:
    backend = PlaywrightBackend(page)
    anchor = Anchor(
        template="templates/btn.png",
        region=(50, 150, 100, 40),
        click_point=(100, 170),
        structural=StructuralLocator(selector="#btn-covered"),
    )
    vision = FakeVision()
    vision.template_results = [
        Match(point=(100, 170), region=(50, 150, 100, 40), confidence=0.99)
    ]
    with pytest.raises(StructuralResolutionRefused):
        resolve(
            anchor,
            make_png(),
            vision,
            template_png=b"tpl",
            viewport=(640, 400),
            structural=backend,
        )
    # The visual template matcher must NOT be called when structural target is covered
    assert len(vision.template_calls) == 0


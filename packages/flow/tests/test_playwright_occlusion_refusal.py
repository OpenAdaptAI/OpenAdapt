"""Tests for occluded / off-viewport element refusal (REALISM_AUDIT rank 17, #1479).

When a DOM element exists uniquely but is covered by an overlay or located outside
the viewport, locate_structural must raise a typed refusal rather than returning
None. Returning None causes resolver.py to fall through to pixel/template rungs,
which risks clicking the covering overlay or off-screen coordinates.
"""

from __future__ import annotations

import pytest

from openadapt_flow.backend import (
    StructuralElementNotActionable,
    StructuralElementNotActionableRefused,
)
from openadapt_flow.backends.playwright_backend import PlaywrightBackend
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Step,
    StructuralHandle,
    StructuralLocator,
    Workflow,
)
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.runtime.resolver import resolve
from tests.test_replayer import FakeBackend, FakeVision, Match, make_png

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
    with pytest.raises(
        StructuralElementNotActionableRefused, match="outside the viewport"
    ):
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
    with pytest.raises(StructuralElementNotActionableRefused):
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


def _make_bundle(tmp_path):
    bundle_dir = tmp_path / "bundle"
    (bundle_dir / "templates").mkdir(parents=True, exist_ok=True)
    (bundle_dir / "templates" / "btn.png").write_bytes(make_png((50, 20)))
    return bundle_dir


class _TransientOverlayBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.locate_attempts = 0

    def locate_structural(self, locator: StructuralLocator) -> StructuralHandle | None:
        self.locate_attempts += 1
        if self.locate_attempts == 1:
            raise StructuralElementNotActionable("occluded by transient modal")
        return StructuralHandle(
            point=(100, 70),
            region=(50, 50, 100, 40),
            target_fingerprint="a" * 64,
            supported_operations=["click"],
        )


def test_transient_overlay_clears_during_retry_loop(tmp_path) -> None:
    bundle = _make_bundle(tmp_path)
    run_dir = tmp_path / "run"
    backend = _TransientOverlayBackend()
    vision = FakeVision()
    step = Step(
        id="s1",
        intent="click transient button",
        action=ActionKind.CLICK,
        timeout_s=1.0,
        anchor=Anchor(
            template="templates/btn.png",
            region=(50, 50, 100, 40),
            click_point=(100, 70),
            structural=StructuralLocator(selector="#btn"),
        ),
    )
    workflow = Workflow(name="wf", steps=[step])
    replayer = Replayer(backend, vision=vision, poll_interval_s=0.01)
    report = replayer.run(workflow, bundle_dir=bundle, run_dir=run_dir)

    assert report.success is True
    assert backend.locate_attempts >= 2
    assert backend.actions == [("click", 100, 70, False)]
    assert report.results[0].ok is True
    assert report.results[0].delivery_attempted is True


class _PersistentOverlayBackend(FakeBackend):
    def locate_structural(self, locator: StructuralLocator) -> StructuralHandle | None:
        raise StructuralElementNotActionable("occluded by modal dialog")


def test_persistent_overlay_halts_with_target_not_actionable_after_timeout(
    tmp_path,
) -> None:
    bundle = _make_bundle(tmp_path)
    run_dir = tmp_path / "run"
    backend = _PersistentOverlayBackend()
    vision = FakeVision()
    vision.template_results = [
        Match(point=(100, 70), region=(50, 50, 100, 40), confidence=0.99)
    ]
    step = Step(
        id="s1",
        intent="click covered button",
        action=ActionKind.CLICK,
        timeout_s=0.03,
        anchor=Anchor(
            template="templates/btn.png",
            region=(50, 50, 100, 40),
            click_point=(100, 70),
            structural=StructuralLocator(selector="#btn-covered"),
        ),
    )
    workflow = Workflow(name="wf", steps=[step])
    replayer = Replayer(backend, vision=vision, poll_interval_s=0.01)
    report = replayer.run(workflow, bundle_dir=bundle, run_dir=run_dir)

    assert report.success is False
    res = report.results[0]
    assert res.ok is False
    assert res.safety_halt is True
    assert res.failure_category == "safety_halt"
    assert res.safety_refusal_evidence is not None
    assert res.safety_refusal_evidence.stage == "target_resolution"
    assert res.safety_refusal_evidence.code == "target_not_actionable"
    assert "structural candidate is not actionable" in (res.error or "")
    # Never fell through to pixel template matching on the overlay!
    assert len(vision.template_calls) == 0


class _StickyHeaderBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.under_header = True

    def locate_structural(self, locator: StructuralLocator) -> StructuralHandle | None:
        if self.under_header:
            raise StructuralElementNotActionable(
                "target is occluded under sticky header"
            )
        return StructuralHandle(
            point=(100, 150),
            region=(50, 130, 100, 40),
            target_fingerprint="b" * 64,
            supported_operations=["click"],
        )


def test_sticky_header_during_scroll_readiness(tmp_path) -> None:
    bundle = _make_bundle(tmp_path)
    backend = _StickyHeaderBackend()
    replayer = Replayer(backend, vision=FakeVision(), poll_interval_s=0.01)
    step = Step(
        id="s1",
        intent="click target under header",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template="templates/btn.png",
            region=(50, 50, 100, 40),
            click_point=(100, 70),
            structural=StructuralLocator(selector="#target"),
        ),
    )
    workflow = Workflow(name="wf", steps=[step])

    # Probe 1: Target occluded under sticky header.
    # _implicit_scroll_target_ready returns miss("unresolved") so scroll loop continues.
    misses_1: list[str] = []
    ready_1 = replayer._implicit_scroll_target_ready(
        step,
        make_png(),
        workflow=workflow,
        bundle_dir=bundle,
        params={},
        misses=misses_1,
    )
    assert ready_1 is False
    assert misses_1 == ["unresolved"]

    # Probe 2: Once scrolled out from under sticky header, _implicit_scroll_target_ready succeeds.
    backend.under_header = False
    misses_2: list[str] = []
    ready_2 = replayer._implicit_scroll_target_ready(
        step,
        make_png(),
        workflow=workflow,
        bundle_dir=bundle,
        params={},
        misses=misses_2,
    )
    assert ready_2 is True
    assert misses_2 == []

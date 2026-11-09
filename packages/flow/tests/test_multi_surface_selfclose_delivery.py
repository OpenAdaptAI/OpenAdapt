"""A click that closes its own popup, when the browser reports the close.

The fixture's sign-in popup closes itself from its "approve" button. Playwright
can report "Target page, context or browser has been closed" from that click
after delivering it. These tests force that report on every click in the
popup and check both sides of the contract:

- the recording marks the closing click ``closes_surface``; replay then counts
  the click as delivered, because the step expected the close and the popup is
  confirmed closed;
- the same report on a step that did not expect its popup to close leaves
  delivery uncertain, as before.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")

from playwright._impl._errors import TargetClosedError  # noqa: E402
from playwright.sync_api import Locator, Mouse  # noqa: E402

import tests.test_multi_surface_e2e as _e2e  # noqa: E402
from openadapt_flow.backends.playwright_backend import PlaywrightBackend  # noqa: E402
from openadapt_flow.compiler import compile_recording  # noqa: E402
from openadapt_flow.ir import ActionKind, Workflow  # noqa: E402
from openadapt_flow.recorder import Recorder  # noqa: E402

pytestmark = pytest.mark.timeout(600)

app_url = _e2e.app_url
browser = _e2e.browser

QUERY = "/?popup=/selfclose.html"
POPUP_PATH = "/selfclose.html"
CLOSING_STEP = 3


def _closed_after(page) -> bool:
    for _ in range(50):
        if page.is_closed():
            return True
        try:
            page.wait_for_timeout(20)
        except Exception:
            pass
    return page.is_closed()


@pytest.fixture()
def racing_close(monkeypatch):
    """Make every click that closes the popup also raise TargetClosedError."""

    original_locator_click = Locator.click
    original_mouse_click = Mouse.click
    pages_by_mouse: dict[int, object] = {}

    def locator_click(self, *args, **kwargs):
        page = self.page
        result = original_locator_click(self, *args, **kwargs)
        if not kwargs.get("trial") and POPUP_PATH in page.url and _closed_after(page):
            raise TargetClosedError("Target page, context or browser has been closed")
        return result

    def mouse_click(self, *args, **kwargs):
        page = pages_by_mouse.get(id(self))
        result = original_mouse_click(self, *args, **kwargs)
        if page is not None and POPUP_PATH in page.url and _closed_after(page):
            raise TargetClosedError("Target page, context or browser has been closed")
        return result

    monkeypatch.setattr(Locator, "click", locator_click)
    monkeypatch.setattr(Mouse, "click", mouse_click)
    return pages_by_mouse


def _record(browser, url: str, root: Path, pages_by_mouse: dict) -> Workflow:
    page = _e2e._fresh_page(browser, url)
    try:
        recorder = Recorder(
            PlaywrightBackend(page),
            root / "recording",
            app_url=url,
            settle_interval_s=0.05,
            settle_timeout_s=2.0,
        )
        recorder.click(*_e2e._center(page, "#open-popup"))
        recorder.switch_surface("popup_1")
        popup = recorder.surface_backend("popup_1").page  # type: ignore[attr-defined]
        pages_by_mouse[id(popup.mouse)] = popup
        recorder.click(*_e2e._center(popup, "#agree"))
        # The browser reports the popup closed during this click; recording
        # continues because the click ran on a popup that is now closed.
        recorder.click(*_e2e._center(popup, "#approve-close"))
        assert popup.is_closed()
        recorder.switch_surface("main")
        recorder.click(*_e2e._center(page, "#finish"))
        recording = recorder.finish()
    finally:
        page.context.close()
    return compile_recording(recording, root / "bundle", name="selfclose-race")


def test_expected_close_counts_as_delivered(app_url, browser, tmp_path, racing_close):
    url = app_url + QUERY
    workflow = _record(browser, url, tmp_path, racing_close)
    assert [s.closes_surface for s in workflow.steps] == [
        s.id == workflow.steps[CLOSING_STEP].id for s in workflow.steps
    ]
    assert workflow.steps[CLOSING_STEP].action is ActionKind.CLICK
    report, page = _e2e._replay(browser, url, tmp_path / "bundle", tmp_path / "run")
    try:
        assert report.success, _e2e._summary(report)
        closing = report.results[CLOSING_STEP]
        assert closing.delivery_uncertainty is None
        assert page.locator("#done").inner_text() == "Referral Finished"
    finally:
        page.context.close()


def test_unexpected_close_stays_uncertain(app_url, browser, tmp_path, racing_close):
    url = app_url + QUERY
    workflow = _record(browser, url, tmp_path, racing_close)
    bundle = tmp_path / "bundle"
    # The same bundle, but the closing click is not declared to close.
    workflow.steps[CLOSING_STEP].closes_surface = False
    workflow.save(bundle)
    report, page = _e2e._replay(browser, url, bundle, tmp_path / "run")
    try:
        closing = report.results[CLOSING_STEP]
        assert closing.delivery_uncertainty is not None
        assert closing.delivery_uncertainty.cause_type == "TargetClosedError"
    finally:
        page.context.close()

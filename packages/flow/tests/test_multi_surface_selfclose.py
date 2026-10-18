"""A popup that closes itself, then an explicit switch back (docs/MULTI_SURFACE.md).

The fixture's sign-in popup writes back to its opener and calls
``window.close()``. Flow never returns to the opener on its own: both the
scripted and the interactive recorder write an explicit SWITCH_SURFACE back to
the main page, with the identity evidence of that page, and replay crosses
the closed popup only through that switch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Error as PlaywrightError  # noqa: E402

import tests.test_multi_surface_e2e as _e2e  # noqa: E402
from openadapt_flow.backends.playwright_backend import PlaywrightBackend  # noqa: E402
from openadapt_flow.compiler import compile_recording  # noqa: E402
from openadapt_flow.interactive_recorder import record_interactive  # noqa: E402
from openadapt_flow.ir import ActionKind, Workflow  # noqa: E402
from openadapt_flow.recorder import Recorder  # noqa: E402

pytestmark = pytest.mark.timeout(600)

app_url = _e2e.app_url
browser = _e2e.browser

QUERY = "/?popup=/selfclose.html"
SHAPE = [
    (ActionKind.CLICK, "main"),
    (ActionKind.SWITCH_SURFACE, "popup_1"),
    (ActionKind.CLICK, "popup_1"),
    (ActionKind.CLICK, "popup_1"),
    (ActionKind.SWITCH_SURFACE, "main"),
    (ActionKind.CLICK, "main"),
]


def _assert_shape(workflow: Workflow) -> None:
    assert [s.id for s in workflow.surfaces] == ["main", "popup_1"]
    assert [(s.action, s.surface) for s in workflow.steps] == SHAPE
    back = workflow.steps[4]
    # The explicit return carries the main page's own identity evidence.
    assert back.surface_origin is not None or back.expect
    assert back.identity_armed is True


def _replay_ok(browser, url: str, bundle_dir: Path, run_dir: Path) -> None:
    report, page = _e2e._replay(browser, url, bundle_dir, run_dir)
    try:
        assert report.success, _e2e._summary(report)
        assert report.model_calls == 0
        assert page.locator("#popup-status").inner_text() == "Signed In Elsewhere"
        assert page.locator("#done").inner_text() == "Referral Finished"
        # The popup really closed; only the main page is left.
        assert [p for p in page.context.pages if not p.is_closed()] == [page]
        assert [r.surface for r in report.results] == [s for _, s in SHAPE]
    finally:
        page.context.close()


def _drive(page, pump) -> None:
    def settle(n: int = 3) -> None:
        for _ in range(n):
            pump()

    page.wait_for_selector("#open-popup", state="visible", timeout=20000)
    with page.expect_popup() as info:
        page.click("#open-popup")
    popup = info.value
    popup.wait_for_load_state("load")
    settle()
    popup.click("#agree")
    settle()
    try:
        popup.click("#approve-close")
    except PlaywrightError as exc:
        # The button's own onclick closes the popup. Playwright can report
        # that close as an error after it delivered the click; the loop below
        # still requires the popup to have closed.
        if "closed" not in str(exc):
            raise
    for _ in range(100):
        if popup.is_closed():
            break
        page.wait_for_timeout(50)
    assert popup.is_closed()
    settle()
    page.click("#finish")
    settle(5)


@pytest.fixture(scope="module")
def interactive_bundle(app_url, tmp_path_factory) -> tuple[Path, Workflow]:
    # Recorded before the module's shared browser starts (one sync Playwright
    # session at a time).
    root = tmp_path_factory.mktemp("selfclose_interactive")
    recording = record_interactive(
        app_url + QUERY, root / "recording", headless=True, script=_drive
    )
    workflow = compile_recording(recording, root / "bundle", name="selfclose-i")
    return root / "bundle", workflow


def test_interactive_recorder_switches_back_from_a_self_closed_popup(
    interactive_bundle, app_url, browser, tmp_path
):
    bundle_dir, workflow = interactive_bundle
    _assert_shape(workflow)
    _replay_ok(browser, app_url + QUERY, bundle_dir, tmp_path / "run")


def test_scripted_recorder_switches_back_from_a_self_closed_popup(
    app_url, browser, tmp_path
):
    url = app_url + QUERY
    page = _e2e._fresh_page(browser, url)
    try:
        recorder = Recorder(
            PlaywrightBackend(page),
            tmp_path / "recording",
            app_url=url,
            settle_interval_s=0.05,
            settle_timeout_s=2.0,
        )
        recorder.click(*_e2e._center(page, "#open-popup"))
        recorder.switch_surface("popup_1")
        popup = recorder.surface_backend("popup_1").page  # type: ignore[attr-defined]
        recorder.click(*_e2e._center(popup, "#agree"))
        recorder.click(*_e2e._center(popup, "#approve-close"))
        assert popup.is_closed()
        recorder.switch_surface("main")
        recorder.click(*_e2e._center(page, "#finish"))
        recording = recorder.finish()
    finally:
        page.context.close()
    workflow = compile_recording(recording, tmp_path / "bundle", name="selfclose")
    _assert_shape(workflow)
    _replay_ok(browser, url, tmp_path / "bundle", tmp_path / "run")


def test_nothing_returns_to_the_opener_on_its_own(app_url, browser, tmp_path):
    """Without the recorded switch back, the bundle is not a valid workflow."""

    url = app_url + QUERY
    page = _e2e._fresh_page(browser, url)
    try:
        recorder = Recorder(
            PlaywrightBackend(page),
            tmp_path / "recording",
            app_url=url,
            settle_interval_s=0.05,
            settle_timeout_s=2.0,
        )
        recorder.click(*_e2e._center(page, "#open-popup"))
        recorder.switch_surface("popup_1")
        popup = recorder.surface_backend("popup_1").page  # type: ignore[attr-defined]
        recorder.click(*_e2e._center(popup, "#agree"))
        recorder.click(*_e2e._center(popup, "#approve-close"))
        recorder.switch_surface("main")
        recorder.click(*_e2e._center(page, "#finish"))
        recording = recorder.finish()
    finally:
        page.context.close()
    workflow = compile_recording(recording, tmp_path / "bundle", name="selfclose")
    data = workflow.model_dump(mode="json")
    data["steps"] = [s for s in data["steps"] if s["id"] != workflow.steps[4].id]
    with pytest.raises(
        ValueError, match="a surface change must be an explicit switch_surface step"
    ):
        Workflow.model_validate(data)

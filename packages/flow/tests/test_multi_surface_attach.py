"""Attach mode (``record --cdp``) follows popups and tabs (docs/MULTI_SURFACE.md).

The person's own Chromium (started here with remote debugging) shows the
fixture app of test_multi_surface_e2e.py. Flow attaches to its one tab. The
app's ``window.open`` popup and its ``noopener`` report tab come from the
browser's target list: a page is followed only when one of the followed pages
opened it (``window.opener``, or the target's opener id for a noopener tab).
Each action in another page is recorded as an explicit switch, and the bundle
replays in the same attached browser with zero model calls. A tab the person
opens by hand is still refused.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Iterator

import pytest

pytest.importorskip("playwright.sync_api")

import tests.test_multi_surface_e2e as _e2e  # noqa: E402
from openadapt_flow.backends.playwright_backend import PlaywrightBackend  # noqa: E402
from openadapt_flow.compiler import compile_recording  # noqa: E402
from openadapt_flow.interactive_recorder import (  # noqa: E402
    BrowserAttachError,
    record_interactive,
)
from openadapt_flow.ir import ActionKind, Workflow  # noqa: E402
from openadapt_flow.runtime.replayer import Replayer  # noqa: E402
from tests._attached_chromium import resize_tab_viewport  # noqa: E402
from tests.test_browser_attach import _chromium_executable  # noqa: E402

pytestmark = pytest.mark.timeout(600)

app_url = _e2e.app_url

SHAPE = [
    (ActionKind.CLICK, "main"),
    (ActionKind.SWITCH_SURFACE, "popup_1"),
    (ActionKind.CLICK, "popup_1"),
    (ActionKind.SWITCH_SURFACE, "main"),
    (ActionKind.CLICK, "main"),
    (ActionKind.SWITCH_SURFACE, "tab_1"),
    (ActionKind.CLICK, "tab_1"),
    (ActionKind.SWITCH_SURFACE, "main"),
    (ActionKind.CLICK, "main"),
]


@pytest.fixture(scope="module")
def endpoint(app_url, tmp_path_factory) -> Iterator[str]:
    executable = _chromium_executable()
    if executable is None:
        pytest.skip("no Chromium executable is installed")
    profile = tmp_path_factory.mktemp("attach-profile")
    process = subprocess.Popen(
        [
            str(executable),
            "--headless=new",
            "--no-sandbox",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-popup-blocking",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0",
            f"--user-data-dir={profile}",
            "--window-size=1280,800",
            "--force-device-scale-factor=1",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        port_file = profile / "DevToolsActivePort"
        deadline = time.monotonic() + 20
        port_line = ""
        while time.monotonic() < deadline and not port_line:
            if process.poll() is not None:
                pytest.fail("Chromium exited before its CDP endpoint was ready")
            try:
                port_line = port_file.read_text().splitlines()[0].strip()
            except (OSError, IndexError):
                time.sleep(0.05)
        assert port_line, "Chromium CDP endpoint did not become ready"
        yield f"http://127.0.0.1:{int(port_line)}"
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def _reset_person_browser(endpoint: str, url: str) -> None:
    """One tab at ``url`` with the replay viewport, as a person would have it."""

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(endpoint)
        context = browser.contexts[0]
        pages = list(context.pages)
        keep = pages[0]
        for extra in pages[1:]:
            extra.close()
        keep.goto(url)
        keep.wait_for_load_state("load")
        resize_tab_viewport(keep, *_e2e.VIEWPORT.values())


def _drive(page, pump) -> None:
    def settle(n: int = 3) -> None:
        for _ in range(n):
            pump()

    with page.expect_popup() as popup_info:
        page.click("#open-popup")
    popup = popup_info.value
    popup.wait_for_load_state("load")
    settle()
    popup.click("#approve")
    settle()
    with page.expect_popup() as tab_info:
        page.click("#open-report")
    tab = tab_info.value
    tab.wait_for_load_state("load")
    settle()
    tab.click("#reviewed")
    settle()
    page.click("#finish")
    settle(5)


@pytest.fixture(scope="module")
def attached_bundle(endpoint, app_url, tmp_path_factory) -> tuple[Path, Workflow]:
    root = tmp_path_factory.mktemp("attach_multi_surface")
    url = app_url + "/"
    _reset_person_browser(endpoint, url)
    recording = record_interactive(
        url, root / "recording", cdp_endpoint=endpoint, script=_drive
    )
    workflow = compile_recording(recording, root / "bundle", name="attached-popup")
    return root / "bundle", workflow


def test_cdp_attach_records_a_popup_and_a_tab_as_surfaces(attached_bundle) -> None:
    _bundle_dir, workflow = attached_bundle
    assert [s.id for s in workflow.surfaces] == ["main", "popup_1", "tab_1"]
    assert workflow.surfaces[1].opener_link == "window_opener"
    assert workflow.surfaces[2].opener_link == "none"
    assert [(s.action, s.surface) for s in workflow.steps] == SHAPE


def test_cdp_attached_bundle_replays_in_the_same_browser(
    attached_bundle, endpoint, app_url, tmp_path
) -> None:
    from playwright.sync_api import sync_playwright

    bundle_dir, _workflow = attached_bundle
    url = app_url + "/"
    _reset_person_browser(endpoint, url)
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(endpoint)
        page = browser.contexts[0].pages[0]
        report = Replayer(PlaywrightBackend(page), poll_interval_s=0.05).run(
            Workflow.load(bundle_dir), bundle_dir=bundle_dir, run_dir=tmp_path / "run"
        )
        assert report.success, _e2e._summary(report)
        assert report.model_calls == 0
        assert [r.surface for r in report.results] == [s for _, s in SHAPE]
        assert page.locator("#popup-status").inner_text() == "Approval Granted"
        assert page.locator("#done").inner_text() == "Referral Finished"


def test_cdp_attach_refuses_a_tab_the_person_opened(
    endpoint, app_url, tmp_path
) -> None:
    url = app_url + "/"
    _reset_person_browser(endpoint, url)

    def drive(page, pump) -> None:
        pump()
        # A tab no followed page opened: Flow cannot tell what it belongs to.
        stray = page.context.new_page()
        stray.goto(app_url + "/other.html")
        for _ in range(3):
            pump()

    with pytest.raises(BrowserAttachError, match="popup or new tab"):
        record_interactive(
            url, tmp_path / "recording", cdp_endpoint=endpoint, script=drive
        )

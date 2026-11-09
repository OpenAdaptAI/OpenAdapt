"""One bundle that switches backends: a web app, then a native Windows app.

The Windows half runs against the stateful MockWaa server that
``tests/test_windows_backend.py`` already uses. The demonstration starts in a
browser, switches to the Windows backend with an explicit SWITCH_SURFACE step,
and finishes there; replay gets the Windows backend by surface id. Refusals:
no backend supplied, a backend of the wrong kind, and a Windows backend that
shows a different application all halt at the switch, before any input
reaches the Windows app.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import cv2
import pytest

pytest.importorskip("playwright.sync_api")

from openadapt_flow.backends.playwright_backend import PlaywrightBackend  # noqa: E402
from openadapt_flow.backends.windows_backend import WindowsBackend  # noqa: E402
from openadapt_flow.compiler import compile_recording  # noqa: E402
from openadapt_flow.ir import ActionKind, PostconditionKind, Workflow  # noqa: E402
from openadapt_flow.recorder import Recorder  # noqa: E402
from openadapt_flow.runtime.replayer import Replayer  # noqa: E402
from tests.test_windows_backend import (  # noqa: E402
    BUTTON_CENTER,
    NOTE_VALUE,
    MockWaa,
    app_screens,
    blank,
    draw_text,
    png_bytes,
)

pytestmark = [
    pytest.mark.timeout(900),
    pytest.mark.filterwarnings("ignore:win_agent channel is plaintext HTTP"),
]

_PAGE = """<!doctype html><html><head><title>Intake Queue</title><style>
body{font-family:Arial,sans-serif;margin:40px}h1{font-size:34px}
button{font-size:24px;padding:14px 28px;border:2px solid #333;background:#eee}
p{font-size:24px}</style></head><body>
<h1>Intake Queue Overview</h1>
<button id="claim" onclick="document.getElementById('s').textContent='Item Claimed For Desktop';">Claim Item</button>
<p id="s"></p></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        data = _PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture(scope="module")
def app_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="module")
def waa() -> Iterator[MockWaa]:
    server = MockWaa(app_screens())
    yield server
    server.close()


@pytest.fixture(scope="module")
def browser() -> Iterator[object]:
    from openadapt_flow._browser_setup import ensure_chromium_installed

    ensure_chromium_installed()
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        chromium = pw.chromium.launch(headless=True)
        yield chromium
        chromium.close()


def _page(browser: object, url: str):
    page = browser.new_page(  # type: ignore[attr-defined]
        viewport={"width": 1280, "height": 800}, device_scale_factor=1
    )
    page.goto(url)
    page.wait_for_load_state("load")
    return page


def _windows(waa: MockWaa) -> WindowsBackend:
    return WindowsBackend(
        waa.url, screenshot_retry_delay_s=0.01, allow_legacy_exec=True
    )


@pytest.fixture(scope="module")
def bundle(app_url: str, browser: object, waa: MockWaa, tmp_path_factory):
    root = tmp_path_factory.mktemp("multi_backend")
    page = _page(browser, app_url)
    waa.reset()
    try:
        recorder = Recorder(
            PlaywrightBackend(page),
            root / "recording",
            app_url=app_url,
            settle_interval_s=0.02,
            settle_timeout_s=2.0,
        )
        box = page.locator("#claim").bounding_box()
        assert box is not None
        recorder.click(int(box["x"] + box["width"] / 2), int(box["y"] + 20))
        recorder.switch_surface("windows_1", backend=_windows(waa))
        recorder.click(*BUTTON_CENTER)
        recorder.type_text(NOTE_VALUE, param="note")
        recorder.press("Enter")
        recording = recorder.finish()
    finally:
        page.context.close()
    assert waa.state == 3
    workflow = compile_recording(recording, root / "bundle", name="web-then-windows")
    return root / "bundle", workflow


def test_compiles_a_typed_backend_switch(bundle) -> None:
    _dir, workflow = bundle
    assert [(s.id, s.kind) for s in workflow.surfaces] == [
        ("main", "web"),
        ("windows_1", "windows"),
    ]
    assert [(s.action, s.surface) for s in workflow.steps] == [
        (ActionKind.CLICK, None),  # before any switch: primary
        (ActionKind.SWITCH_SURFACE, "windows_1"),
        (ActionKind.CLICK, "windows_1"),
        (ActionKind.TYPE, "windows_1"),
        (ActionKind.KEY, "windows_1"),
    ]
    switch = workflow.steps[1]
    assert switch.surface_origin is None  # not a browser target
    # Its identity check is the new surface's own screen text.
    assert [pc.kind for pc in switch.expect] == [PostconditionKind.TEXT_PRESENT]
    assert switch.identity_armed is True


def test_replays_web_then_windows_with_zero_model_calls(
    bundle, app_url: str, browser: object, waa: MockWaa, tmp_path: Path
) -> None:
    bundle_dir, _ = bundle
    waa.reset()
    page = _page(browser, app_url)
    try:
        primary = PlaywrightBackend(page)
        replayer = Replayer(
            primary,
            poll_interval_s=0.02,
            surface_backends={"windows_1": _windows(waa)},
        )
        report = replayer.run(
            Workflow.load(bundle_dir),
            params={"note": NOTE_VALUE},
            bundle_dir=bundle_dir,
            run_dir=tmp_path / "run",
        )
        assert report.success, [(r.step_id, r.error) for r in report.results]
        assert report.model_calls == 0
        assert page.locator("#s").inner_text() == "Item Claimed For Desktop"
        assert waa.state == 3
        # The primary backend is restored when the run ends.
        assert replayer.backend is primary
    finally:
        page.context.close()


def _replay_with(bundle_dir: Path, browser, url, tmp_path, surface_backends):
    page = _page(browser, url)
    try:
        return Replayer(
            PlaywrightBackend(page),
            poll_interval_s=0.02,
            surface_backends=surface_backends,
        ).run(
            Workflow.load(bundle_dir),
            params={"note": NOTE_VALUE},
            bundle_dir=bundle_dir,
            run_dir=tmp_path / "run",
        )
    finally:
        page.context.close()


def test_switch_without_a_supplied_backend_halts(
    bundle, app_url, browser, waa: MockWaa, tmp_path
) -> None:
    bundle_dir, _ = bundle
    waa.reset()
    report = _replay_with(bundle_dir, browser, app_url, tmp_path, None)
    assert not report.success
    last = report.results[-1]
    assert last.step_id == "step_001" and last.safety_halt
    assert "no backend was supplied for surface 'windows_1'" in (last.error or "")
    assert waa.state == 0 and waa.commands == []


def test_switch_to_a_backend_of_the_wrong_kind_halts(
    bundle, app_url, browser, waa: MockWaa, tmp_path
) -> None:
    bundle_dir, _ = bundle
    waa.reset()
    other = _page(browser, app_url)
    try:
        report = _replay_with(
            bundle_dir,
            browser,
            app_url,
            tmp_path,
            {"windows_1": PlaywrightBackend(other)},
        )
    finally:
        other.context.close()
    assert not report.success
    last = report.results[-1]
    assert last.step_id == "step_001"
    assert "declared windows, but the supplied backend serves web" in (last.error or "")
    assert waa.commands == []


def test_switch_to_a_windows_app_that_is_not_the_recorded_one_halts(
    bundle, app_url, browser, tmp_path
) -> None:
    bundle_dir, _ = bundle
    other_app = blank()
    draw_text(other_app, 420, 84, "Unrelated Billing Console")
    cv2.rectangle(other_app, (560, 400), (720, 448), (205, 205, 205), -1)
    wrong = MockWaa([png_bytes(other_app)] * 4)
    try:
        report = _replay_with(
            bundle_dir, browser, app_url, tmp_path, {"windows_1": _windows(wrong)}
        )
        assert not report.success
        last = report.results[-1]
        assert last.step_id == "step_001" and last.safety_halt
        assert "Surface identity check failed" in (last.error or "")
        # No input reached the wrong application.
        assert wrong.commands == []
    finally:
        wrong.close()

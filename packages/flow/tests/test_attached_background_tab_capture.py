"""A capture of an attached tab that a popup pushed to the background.

In a person's Chromium (reached over CDP, so without Playwright's launch flags
that keep background tabs rendering), ``window.open`` opens a new foreground
tab. Chromium then draws no new frames for the opener, so a secret-masked
capture of it can wait forever (``Page.screenshot: Timeout 30000ms``). Attach
recording captures the opener's after-frame exactly then. The backend brings
the captured page to front first, so every capture is a live frame.
"""

from __future__ import annotations

import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest

pytest.importorskip("playwright.sync_api")

from openadapt_flow.backends.playwright_backend import PlaywrightBackend  # noqa: E402
from tests.test_browser_attach import _chromium_executable  # noqa: E402

_PAGE = (
    b"<input type=password id=secret value=hidden>"
    b"<button id=open onclick=\"window.open('about:blank', '_blank')\">open</button>"
)


@pytest.fixture
def app_url() -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(_PAGE)))
            self.end_headers()
            self.wfile.write(_PAGE)

        def log_message(self, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.timeout(120)
def test_masked_capture_of_a_backgrounded_attached_tab_does_not_stall(
    tmp_path: Path, app_url: str
) -> None:
    executable = _chromium_executable()
    if executable is None:
        pytest.skip("no Chromium executable is installed")
    from playwright.sync_api import sync_playwright

    profile = tmp_path / "profile"
    profile.mkdir()
    process = subprocess.Popen(
        [
            str(executable),
            "--headless=new",
            "--no-sandbox",
            "--no-first-run",
            "--no-default-browser-check",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0",
            f"--user-data-dir={profile}",
            "--window-size=1280,800",
            "--force-device-scale-factor=1",
            app_url,
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
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{int(port_line)}"
            )
            page = browser.contexts[0].pages[0]
            page.wait_for_load_state("load")
            backend = PlaywrightBackend(
                page,
                screenshot_scale="css",
                screenshot_mask_selectors=("#secret",),
                bring_to_front_before_capture=True,
            )
            with page.expect_popup() as popup_info:
                page.click("#open")
            popup = popup_info.value
            popup.set_content("<input id=note><button id=save>Save</button>")
            popup.fill("#note", "the popup now holds the foreground")
            popup.click("#save")
            page.set_default_timeout(10_000)
            for _ in range(12):
                assert backend.screenshot().startswith(b"\x89PNG")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()

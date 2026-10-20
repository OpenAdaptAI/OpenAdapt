"""The write fence blocks every write path and refuses when it can't see them.

A real browser and a local server: the server counts every non-``GET``
request it receives, so a test can tell "blocked in the browser" from
"reached the server".
"""

from __future__ import annotations

import http.server
import threading
from typing import Any, Iterator

import pytest

pytest.importorskip("playwright.sync_api")

PAGE = b"""<!doctype html><html><body>
<button id="local" onclick="document.getElementById('out').textContent='ok'">Close</button>
<button id="post" onclick="fetch('/write', {method: 'POST', body: 'x'})">Continue</button>
<button id="beacon" onclick="navigator.sendBeacon('/write', 'x')">Next</button>
<form method="post" action="/write"><button id="form">OK</button></form>
<button id="ws" onclick="window.sock = new WebSocket('ws://' + location.host + '/ws')">Got it</button>
<div id="out"></div>
</body></html>"""


class _Server(http.server.BaseHTTPRequestHandler):
    writes: list[str] = []

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self.send_response(200)
        self.send_header("content-type", "text/html")
        self.end_headers()
        self.wfile.write(PAGE)

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        type(self).writes.append(self.path)
        self.send_response(204)
        self.end_headers()

    def log_message(self, *_: Any) -> None:
        pass


@pytest.fixture()
def page_url() -> Iterator[str]:
    _Server.writes = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Server)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()


@pytest.fixture()
def backend(page_url: str) -> Iterator[Any]:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    try:
        backend, close = PlaywrightBackend.launch(page_url, headless=True)
    except Exception as exc:  # pragma: no cover - no browser installed
        pytest.skip(f"no Playwright browser: {exc}")
    try:
        yield backend
    finally:
        close()


def _click(backend: Any, selector: str) -> None:
    backend.page.click(selector)


def test_a_click_that_only_changes_the_page_holds_the_fence(backend: Any) -> None:
    with backend.write_fence() as report:
        assert report.refusal is None
        _click(backend, "#local")
    assert report.held
    assert backend.page.inner_text("#out") == "ok"


@pytest.mark.parametrize(
    ("selector", "attempt"),
    [("#post", "POST"), ("#beacon", "POST"), ("#form", "POST"), ("#ws", "websocket")],
)
def test_every_write_path_is_blocked_and_reported(
    backend: Any, selector: str, attempt: str
) -> None:
    with backend.write_fence() as report:
        assert report.refusal is None
        _click(backend, selector)
    assert not report.held
    assert attempt in report.attempts
    # Blocked in the browser: the server never saw a write.
    assert _Server.writes == []


def test_writes_pass_again_once_the_fence_is_down(backend: Any) -> None:
    with backend.write_fence() as report:
        _click(backend, "#local")
    assert report.held
    _click(backend, "#post")
    backend.page.wait_for_timeout(300)
    assert _Server.writes == ["/write"]


def test_the_fence_refuses_when_a_websocket_may_already_be_open(
    backend: Any,
) -> None:
    backend._websockets._open.add(1)
    with backend.write_fence() as report:
        pass
    assert report.refusal == "a WebSocket was already open"
    assert not report.held


def test_a_page_wrapped_after_its_document_loaded_is_refused(page_url: str) -> None:
    from playwright.sync_api import sync_playwright

    from openadapt_flow.backends._write_fence import tracker_for, write_fence

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - no browser installed
            pytest.skip(f"no Playwright browser: {exc}")
        page = browser.new_page()
        page.goto(page_url)
        tracker = tracker_for(page)
        with write_fence(page, tracker) as report:
            pass
        assert report.refusal is not None
        # A new document starts with no sockets, all of them seen.
        page.reload()
        with write_fence(page, tracker) as report:
            page.click("#local")
        assert report.held
        browser.close()

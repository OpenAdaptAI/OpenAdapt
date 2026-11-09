"""Real headless Chromium tries to write from workers and new windows.

Everything stays on a synthetic loopback app. The baselines prove each
attempt really sends a POST and opens a WebSocket from a worker when nothing
contains it; under containment neither may reach the app.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("playwright")

from openadapt_flow.backends.worker_containment import (  # noqa: E402
    NEW_WINDOW,
    WORKER_STARTED,
    contain_workers,
)


class App:
    """A loopback app whose pages start workers that write."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, str]] = []
        app = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (http.server API)
                path = urlsplit(self.path).path
                if path == "/ws":
                    app.writes.append(("websocket", self.path))
                    self.send_response(400)
                    self.end_headers()
                    return
                if path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/?attempt=shared")
                    self.end_headers()
                    return
                if path == "/worker.js":
                    body, kind = app.worker.encode(), "application/javascript"
                else:
                    query = parse_qs(urlsplit(self.path).query)
                    attempt = query.get("attempt", [""])[0]
                    script = app.attempt(attempt) if attempt else ""
                    body = (
                        "<!doctype html><html><head><script>try {"
                        + script
                        + "} catch (e) {}</script></head>"
                        "<body>synthetic caf\u00e9</body></html>"
                    ).encode()
                    kind = "text/html; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802
                app.writes.append(("post", self.path))
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.worker = (
            "fetch('" + self.base + "/write', {method: 'POST', body: 'synthetic'});"
            "new WebSocket('" + self.base.replace("http:", "ws:") + "/ws');"
        )

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def attempt(self, kind: str) -> str:
        worker = json.dumps(self.worker)
        blob = (
            f"URL.createObjectURL(new Blob([{worker}],"
            " {type: 'application/javascript'}))"
        )
        popup = "/?attempt=shared"
        shared_in_doc = (
            "<script>new SharedWorker(" + json.dumps(self.base + "/worker.js?w") + ")"
            "</scr' + 'ipt>"
        )
        attempts = {
            "dedicated": "window.w = new Worker('/worker.js?dedicated')",
            "shared": "window.w = new SharedWorker('/worker.js?shared')",
            "service": (
                "navigator.serviceWorker.register('/worker.js?service').catch(() => {})"
            ),
            "blob-dedicated": f"window.w = new Worker({blob})",
            "blob-shared": f"window.w = new SharedWorker({blob})",
            "module": "window.w = new Worker('/worker.js?module', {type: 'module'})",
            "blank-frame": (
                "const f = document.createElement('iframe');"
                " document.documentElement.append(f);"
                " window.w = new f.contentWindow.SharedWorker('/worker.js?blank')"
            ),
            "srcdoc-frame": (
                "const f = document.createElement('iframe');"
                f" f.srcdoc = '{shared_in_doc}';"
                " document.documentElement.append(f)"
            ),
            "cross-site-frame": (
                "const f = document.createElement('iframe');"
                " f.src = "
                + json.dumps(
                    self.base.replace("127.0.0.1", "localhost") + "/?attempt=shared"
                )
                + "; document.documentElement.append(f)"
            ),
            "popup-open": f"window.p = window.open('{popup}')",
            "popup-frame-open": (
                "const f = document.createElement('iframe');"
                " document.documentElement.append(f);"
                f" window.p = f.contentWindow.open('{popup}')"
            ),
            "popup-blank-write": (
                f"const p = window.open(''); p.document.write('{shared_in_doc}')"
            ),
            "popup-link": (
                "addEventListener('DOMContentLoaded', () => {"
                " const a = document.createElement('a');"
                f" a.href = '{popup}'; a.target = '_blank';"
                " document.body.append(a); a.click(); })"
            ),
            "popup-form": (
                "addEventListener('DOMContentLoaded', () => {"
                " const f = document.createElement('form');"
                " f.action = '/'; f.target = '_blank';"
                " const i = document.createElement('input');"
                " i.name = 'attempt'; i.value = 'shared'; f.append(i);"
                " document.body.append(f); f.submit(); })"
            ),
        }
        return attempts[kind]


ESCAPES = [
    "dedicated",
    "shared",
    "service",
    "blob-dedicated",
    "blob-shared",
    "module",
    "blank-frame",
    "srcdoc-frame",
    "cross-site-frame",
    "popup-open",
    "popup-frame-open",
    "popup-blank-write",
    "popup-link",
    "popup-form",
]


@pytest.fixture
def app():
    instance = App()
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture(scope="module")
def playwright():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        yield pw


def _launch(playwright: Any) -> Any:
    return playwright.chromium.launch(headless=True, args=["--site-per-process"])


@pytest.fixture(scope="module")
def browser(playwright):
    browser = _launch(playwright)
    try:
        yield browser
    finally:
        browser.close()


@pytest.fixture(scope="module")
def open_browser(playwright):
    # The baselines leave service workers behind, so they get their own
    # browser: containment reports a leftover worker it can't place.
    browser = _launch(playwright)
    try:
        yield browser
    finally:
        browser.close()


def _settle(page: Any, ms: int = 1500) -> None:
    page.wait_for_timeout(ms)


@pytest.mark.parametrize("kind", ESCAPES)
def test_each_attempt_really_writes_without_containment(open_browser, app, kind):
    context = open_browser.new_context(service_workers="allow")
    try:
        page = context.new_page()
        page.goto(app.base + "/?attempt=" + kind)
        _settle(page)
        assert {method for method, _ in app.writes} == {"post", "websocket"}
    finally:
        context.close()


@pytest.mark.parametrize("kind", ESCAPES)
def test_no_worker_or_new_window_write_reaches_the_app(browser, app, kind):
    # Service workers are allowed here so the policy itself must refuse them.
    context = browser.new_context(service_workers="allow")
    try:
        page = context.new_page()
        seen: list[str] = []
        state = contain_workers(page, seen.append)
        assert state.problem is None
        page.goto(app.base + "/?attempt=" + kind)
        _settle(page)
        assert app.writes == []
        # The page itself still loads and parses.
        assert page.locator("body").inner_text() == "synthetic caf\u00e9"
        assert seen == state.violations
        if kind in {"popup-link", "popup-form"}:
            assert NEW_WINDOW in state.violations
        assert WORKER_STARTED not in state.violations
    finally:
        context.close()


def test_a_redirect_to_a_worker_page_is_contained(browser, app):
    context = browser.new_context(service_workers="allow")
    try:
        page = context.new_page()
        contain_workers(page)
        page.goto(app.base + "/redirect")
        _settle(page)
        assert app.writes == []
    finally:
        context.close()


def test_a_worker_started_before_containment_is_reported(open_browser, app):
    context = open_browser.new_context(service_workers="block")
    try:
        page = context.new_page()
        page.goto(app.base + "/?attempt=shared")
        _settle(page)
        assert app.writes  # it ran before anything contained it
        state = contain_workers(page)
        page.wait_for_timeout(300)
        assert WORKER_STARTED in state.violations
        assert not state.ok
    finally:
        context.close()


def test_another_contexts_worker_is_not_reported(playwright, app):
    browser = _launch(playwright)
    other = browser.new_context(service_workers="block")
    context = browser.new_context(service_workers="block")
    try:
        state = contain_workers(context.new_page())
        stray = other.new_page()
        stray.goto(app.base + "/?attempt=shared")
        _settle(stray)
        assert app.writes  # the other context is not contained
        assert state.violations == []
    finally:
        browser.close()


def test_the_pages_own_websocket_still_opens(browser):
    from tests.test_create_form_wire import _KEEP_JS, _Server, _ws_url

    server = _Server()
    context = browser.new_context(service_workers="block")
    try:
        page = context.new_page()
        state = contain_workers(page)
        page.goto(server.url)
        assert page.evaluate(_KEEP_JS, _ws_url(server)) is True
        assert state.ok
    finally:
        context.close()
        server.http.shutdown()


def test_containment_refuses_without_the_target_domain():
    def refuse(_page: Any) -> Any:
        raise RuntimeError("no DevTools session")

    page = SimpleNamespace(context=SimpleNamespace(new_cdp_session=refuse))
    state = contain_workers(page)
    assert state.problem is not None
    assert not state.ok

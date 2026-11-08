"""Opening failures close invocation-owned resources, including cancellation.

The real-browser probes use existing launch APIs on the candidate and parent;
an absent hook cannot count as parent failure evidence. Resource doubles cover
the acquisition cut points that cannot be cancelled deterministically live.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace

import pytest

from openadapt_flow.backends import _browser_process, playwright_backend
from openadapt_flow.backends.playwright_backend import PlaywrightBackend


class Cancelled(BaseException):
    pass


@pytest.fixture
def local_url():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<!doctype html><p>Owned context</p>")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


@pytest.mark.parametrize("stage", ["websocket", "navigation", "construction"])
@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
def test_real_open_failure_closes_only_its_owned_resources(
    local_url, monkeypatch, stage, shared, cancel
):
    primary = Cancelled() if cancel else RuntimeError(stage)
    observed = {}

    def fail(*args, **kwargs):
        raise primary

    owner = _browser_process.shared_browser(headless=True) if shared else None
    if owner is not None:
        owner.__enter__()
    try:
        if shared:
            _, browser = _browser_process._SHARED_BROWSER[True]
            sentinel = browser.new_context()
            page = sentinel.new_page()
            before = list(browser.contexts)
        else:
            start = playwright_backend._start_browser

            def record_start(headless):
                pw, browser = start(headless)
                observed.update(browser=browser, stops=0)
                stop = pw.stop

                def record_stop():
                    observed["stops"] += 1
                    stop()

                pw.stop = record_stop
                observed["cleanup"] = record_stop
                return pw, browser

            monkeypatch.setattr(playwright_backend, "_start_browser", record_start)
        if stage == "websocket":
            monkeypatch.setattr(playwright_backend, "prepare_ws_guard", fail)
        elif stage == "navigation":
            # Fail after a real navigation, while launch still owns the page.
            from playwright.sync_api import Page

            goto = Page.goto

            def navigate_then_fail(self, *args, **kwargs):
                goto(self, *args, **kwargs)
                raise primary

            monkeypatch.setattr(Page, "goto", navigate_then_fail)
        else:
            monkeypatch.setattr(PlaywrightBackend, "__init__", fail)
        with pytest.raises(type(primary)) as caught:
            PlaywrightBackend.launch(local_url, headless=True)
        assert caught.value is primary
        if shared:
            assert browser.is_connected()
            assert browser.contexts == before
            assert not page.is_closed()
            sentinel.close()
        else:
            assert not observed["browser"].is_connected()
            assert observed["stops"] == 1
    finally:
        if owner is not None:
            owner.__exit__(None, None, None)
        elif observed and observed["browser"].is_connected():
            # A literal-parent semantic leak must still be cleaned by this
            # probe after the assertions, without granting the parent credit.
            try:
                observed["browser"].close()
            finally:
                observed["cleanup"]()


@pytest.mark.parametrize(
    "stage", ["context", "page", "guard", "websocket", "navigation", "construction"]
)
@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
def test_acquisition_cut_points_unwind_once(monkeypatch, stage, shared, cancel):
    events = []
    primary = Cancelled() if cancel else RuntimeError(stage)

    def at(name):
        events.append(name)
        if name == stage:
            raise primary

    class Context:
        def new_page(self):
            at("page")
            return SimpleNamespace(goto=lambda _: at("navigation"))

        def close(self):
            events.append("context-close")

    class Browser:
        def new_context(self, **kwargs):
            assert kwargs["service_workers"] == "block"
            at("context")
            return Context()

        def close(self):
            events.append("browser-close")

    browser = Browser()
    pw = SimpleNamespace(stop=lambda: events.append("driver-stop"))
    if shared:
        monkeypatch.setitem(playwright_backend._SHARED_BROWSER, True, (pw, browser))
    else:
        monkeypatch.setattr(
            playwright_backend, "_start_browser", lambda _: (pw, browser)
        )
    monkeypatch.setattr(
        playwright_backend, "prepare_ws_guard", lambda _: at("websocket")
    )
    monkeypatch.setattr(
        PlaywrightBackend, "__init__", lambda *_a, **_kw: at("construction")
    )
    with pytest.raises(type(primary)) as caught:
        PlaywrightBackend.launch(
            "http://127.0.0.1:1/", before_goto=lambda _: at("guard")
        )
    assert caught.value is primary
    assert events.count("context-close") == (0 if stage == "context" else 1)
    assert events.count("browser-close") == (0 if shared else 1)
    assert events.count("driver-stop") == (0 if shared else 1)


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("stage", ["driver-start", "browser-start"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_browser_start_preserves_primary_and_stops_owned_driver(
    monkeypatch, cancel, stage, cleanup_fails
):
    import playwright.sync_api
    from openadapt_flow import _browser_setup

    events = []
    primary = Cancelled() if cancel else RuntimeError(stage)

    def stop():
        events.append("driver-stop")
        if cleanup_fails:
            raise ValueError("cleanup")

    class Manager:
        def start(self):
            events.append("driver-start")
            if stage == "driver-start":
                raise primary
            return SimpleNamespace(chromium=object(), stop=stop)

        def __exit__(self, *args):
            stop()

    def launch(*args, **kwargs):
        raise primary

    monkeypatch.setattr(playwright.sync_api, "sync_playwright", Manager)
    monkeypatch.setattr(_browser_setup, "require_browser_support", lambda: None)
    monkeypatch.setattr(_browser_setup, "launch_chromium", launch)
    with pytest.raises(type(primary)) as caught:
        _browser_process._start_browser(True)
    assert caught.value is primary
    assert events == ["driver-start", "driver-stop"]


@pytest.mark.parametrize("primary_fails", [False, True])
@pytest.mark.parametrize("cancel_cleanup", [False, True])
def test_shared_browser_closes_all_process_owners_and_preserves_primary(
    monkeypatch, primary_fails, cancel_cleanup
):
    events = []
    primary = RuntimeError("body")
    cleanup = Cancelled() if cancel_cleanup else ValueError("cleanup")

    def close():
        events.append("browser-close")
        raise cleanup

    monkeypatch.setattr(
        _browser_process,
        "_start_browser",
        lambda _: (
            SimpleNamespace(stop=lambda: events.append("driver-stop")),
            SimpleNamespace(close=close),
        ),
    )
    with pytest.raises(type(primary if primary_fails else cleanup)) as caught:
        with _browser_process.shared_browser():
            if primary_fails:
                raise primary
    assert caught.value is (primary if primary_fails else cleanup)
    assert events == ["browser-close", "driver-stop"]
    assert True not in _browser_process._SHARED_BROWSER


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
def test_failed_close_is_latched_without_retrying_owners(monkeypatch, shared, cancel):
    events = []
    cleanup = Cancelled() if cancel else ValueError("cleanup")

    def context_close():
        events.append("context-close")
        raise cleanup

    context = SimpleNamespace(
        new_page=lambda: SimpleNamespace(goto=lambda _: None), close=context_close
    )
    browser = SimpleNamespace(
        new_context=lambda **_: context,
        close=lambda: events.append("browser-close"),
    )
    pw = SimpleNamespace(stop=lambda: events.append("driver-stop"))
    if shared:
        monkeypatch.setitem(playwright_backend._SHARED_BROWSER, True, (pw, browser))
    else:
        monkeypatch.setattr(
            playwright_backend, "_start_browser", lambda _: (pw, browser)
        )
    monkeypatch.setattr(playwright_backend, "prepare_ws_guard", lambda _: None)
    monkeypatch.setattr(PlaywrightBackend, "__init__", lambda *_a, **_kw: None)
    _, close = PlaywrightBackend.launch("http://127.0.0.1:1/")
    for _ in range(2):
        with pytest.raises(type(cleanup)) as caught:
            close()
        assert caught.value is cleanup
    assert events == (
        ["context-close"]
        if shared
        else ["context-close", "browser-close", "driver-stop"]
    )


def test_dispatcher_attempts_remaining_closers_after_cancellation():
    from openadapt_flow.jit.compile_agent import RecordingDispatcher

    events = []
    primary = Cancelled()
    dispatcher = RecordingDispatcher(SimpleNamespace(result=None))
    dispatcher.on_close(lambda: events.append("remaining"))

    def cancelled():
        events.append("cancelled")
        raise primary

    dispatcher.on_close(cancelled)
    with pytest.raises(Cancelled) as caught:
        dispatcher.close()
    assert caught.value is primary
    assert events == ["cancelled", "remaining"]

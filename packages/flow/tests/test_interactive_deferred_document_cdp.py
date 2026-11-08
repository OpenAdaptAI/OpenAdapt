"""Independent real-CDP regressions for cross-batch document ownership."""

from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from openadapt_flow.interactive_recorder import BrowserAttachError, InteractiveRecorder
from tests.test_browser_attach import _chromium_executable


@pytest.fixture
def document_browser(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            title = "Second view" if self.path == "/second" else "First view"
            color = "#227744" if self.path == "/second" else "#772244"
            body = f"""<!doctype html><title>{title}</title>
            <body style="margin:0;height:3000px;background:{color}">
            <label>Shared field <input id="shared" name="shared"></label>
            <button id="point">Mark position</button></body>""".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    executable = _chromium_executable()
    assert executable is not None, "cached Chromium is required; no install is permitted"
    profile = tmp_path / "owned-document-browser"
    profile.mkdir()
    process = subprocess.Popen(
        [
            str(executable),
            "--headless=new",
            "--no-sandbox",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0",
            f"--user-data-dir={profile}",
            "--window-size=1280,800",
            "--force-device-scale-factor=1",
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    endpoints = queue.Queue()

    def read_endpoint():
        assert process.stderr is not None
        for raw in iter(process.stderr.readline, b""):
            found = re.search(
                rb"DevTools listening on (ws://127\.0\.0\.1:[0-9]+/devtools/browser/[A-Za-z0-9-]+)",
                raw,
            )
            if found:
                endpoints.put(found.group(1).decode("ascii"))

    reader = threading.Thread(target=read_endpoint, daemon=True)
    reader.start()
    try:
        yield url, endpoints.get(timeout=10), process
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        if process.stderr is not None:
            process.stderr.close()
        reader.join(timeout=1)
        server.shutdown()
        server.server_close()
        worker.join(timeout=1)


def _perform(session, kind, value):
    assert session.page is not None
    if kind == "input":
        session.page.fill("#shared", value)
    elif kind == "scroll":
        before = session.page.evaluate("window.scrollY")
        session.page.mouse.wheel(0, 120)
        session.page.wait_for_function("before => window.scrollY > before", arg=before)
    else:
        box = session.page.locator("#point").bounding_box()
        assert box is not None
        session.page.mouse.click(
            box["x"] + box["width"] / 2,
            box["y"] + box["height"] / 2,
            click_count=2 if value == "beta" else 1,
        )
    assert session.pump()


@pytest.mark.timeout(90)
@pytest.mark.parametrize(
    "kind,attribute", [("input", "_pending_type"), ("scroll", "_pending_scroll"), ("click", "_pending_click")]
)
def test_cross_document_deferred_action_refuses_complete_publication(
    document_browser, tmp_path, monkeypatch, kind, attribute
):
    url, endpoint, process = document_browser
    out = tmp_path / "recording"
    session = InteractiveRecorder(url, out, cdp_endpoint=endpoint)
    session.start()
    try:
        _perform(session, kind, "alpha")
        pending = getattr(session, attribute)
        assert pending is not None
        original = dict(pending)
        assert len(session._producer_ordinals) == 1
        first_owner = next(iter(session._producer_ordinals))
        if kind == "input":
            assert pending["value"] == "alpha"
        assert session.page is not None
        session.page.goto(url + "second")
        session.page.wait_for_selector("#shared")
        observed = []
        original_persist = session._persist_acquired_event

        def observe(event):
            observed.append(dict(event))
            original_persist(event)

        monkeypatch.setattr(session, "_persist_acquired_event", observe)
        refusal = None
        try:
            _perform(session, kind, "beta")
        except BrowserAttachError as error:
            refusal = error
        assert refusal is not None, "a different document replaced a deferred action"
        assert "producer ownership" in str(refusal)
        assert session._listener_error is refusal
        assert len(observed) == 1
        assert observed[0]["_oaflow_producer_doc"] != first_owner
        if kind == "input":
            assert observed[0]["_oaflow_input_session"] == pending["input_session"]
            assert observed[0]["field"] == pending["field"]
        assert getattr(session, attribute) is pending
        assert pending == original
        assert first_owner is not None
        assert session.done
        with pytest.raises(BrowserAttachError, match="producer ownership"):
            session.finish()
        assert not out.exists()
        assert process.poll() is None, "the fixture still owns its attached browser"
    finally:
        session.abort()


@pytest.mark.timeout(90)
@pytest.mark.parametrize("kind", ["input", "scroll", "click"])
def test_same_document_deferred_actions_keep_one_recorded_step(
    document_browser, tmp_path, kind
):
    url, endpoint, process = document_browser
    session = InteractiveRecorder(url, tmp_path / "recording", cdp_endpoint=endpoint)
    session.start()
    try:
        _perform(session, kind, "alpha")
        _perform(session, kind, "beta")
        result = session.finish()
        events = [json.loads(row) for row in (result / "events.jsonl").read_text().splitlines()]
        assert len(events) == 1
        assert events[0]["kind"] == {"input": "type", "scroll": "scroll", "click": "double_click"}[kind]
        if kind == "input":
            assert events[0]["text"] == "beta"
        if kind == "scroll":
            assert events[0]["dy"] > 120
        assert process.poll() is None
    finally:
        session.abort()


@pytest.mark.timeout(90)
def test_first_action_in_a_new_document_needs_no_prior_owner(
    document_browser, tmp_path
):
    url, endpoint, _process = document_browser
    session = InteractiveRecorder(url, tmp_path / "recording", cdp_endpoint=endpoint)
    session.start()
    try:
        assert session.page is not None
        session.page.goto(url + "second")
        _perform(session, "input", "beta")
        result = session.finish()
        events = [json.loads(row) for row in (result / "events.jsonl").read_text().splitlines()]
        assert len(events) == 1
        assert events[0]["kind"] == "type"
        assert events[0]["text"] == "beta"
    finally:
        session.abort()

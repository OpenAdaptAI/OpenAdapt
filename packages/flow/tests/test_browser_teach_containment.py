"""Real headless browser guards on fresh credential-free loopback servers."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import sync_playwright

from openadapt_flow.agent import browser_teach as bt
from openadapt_flow.agent.bridge import BridgeError
from openadapt_flow.backends import worker_containment
from openadapt_flow.teach_scope import TeachScope, WriteClaim, control_labels_at
from openadapt_flow.wire_check import body_shape

BODY = b"record=expected&value=synthetic"


class App:
    def __init__(self) -> None:
        self.received: list[tuple[str, str]] = []
        self.ws_handshakes = 0
        self.ws_frames: list[bytes] = []
        self.frame_received = threading.Event()
        self.html: str | None = None
        self.frame_url: str | None = None
        app = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self.respond("GET")

            def do_POST(self) -> None:  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.respond("POST")

            def respond(self, method: str) -> None:
                parts = urlsplit(self.path)
                app.received.append((method, parts.path))
                if parts.path == "/ws":
                    key = self.headers.get("Sec-WebSocket-Key")
                    if (
                        not key
                        or self.headers.get("Upgrade", "").lower() != "websocket"
                    ):
                        self.send_response(400)
                        self.end_headers()
                        return
                    accept = base64.b64encode(
                        hashlib.sha1(
                            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
                        ).digest()
                    ).decode()
                    self.send_response(101)
                    self.send_header("Upgrade", "websocket")
                    self.send_header("Connection", "Upgrade")
                    self.send_header("Sec-WebSocket-Accept", accept)
                    self.end_headers()
                    self.wfile.flush()
                    app.ws_handshakes += 1
                    self.close_connection = True
                    self.connection.settimeout(2)
                    try:
                        # This disposable server only accepts one small,
                        # masked text frame from our own capability control.
                        header = self.rfile.read(2)
                        if (
                            len(header) != 2
                            or header[0] != 0x81
                            or not header[1] & 0x80
                        ):
                            return
                        size = header[1] & 0x7F
                        if size >= 126:
                            return
                        mask = self.rfile.read(4)
                        payload = self.rfile.read(size)
                        if len(mask) != 4 or len(payload) != size:
                            return
                        app.ws_frames.append(
                            bytes(
                                value ^ mask[i % 4] for i, value in enumerate(payload)
                            )
                        )
                        app.frame_received.set()
                    except OSError:
                        return
                    return
                if parts.path == "/redirect307":
                    self.send_response(307)
                    self.send_header("Location", "/write")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if parts.path == "/write":
                    self.send_response(303)
                    self.send_header("Location", "/final")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                kind = parse_qs(parts.query).get("attempt", [""])[0]
                content_type = "text/html"
                if parts.path == "/worker.js":
                    kind = parse_qs(parts.query).get("kind", [""])[0]
                    text = (
                        "new WebSocket("
                        + json.dumps(app.base.replace("http:", "ws:") + "/ws")
                        + ");"
                        if kind == "socket"
                        else "fetch("
                        + json.dumps(app.base + "/write")
                        + ", {method:'POST',body:'record=expected&value=synthetic'});"
                    )
                    content_type = "application/javascript"
                elif parts.path == "/frame":
                    text = "<script>setTimeout(() => fetch('/write', {method:'POST',body:'synthetic'}), 150)</script>"
                elif parts.path == "/frame-redirect":
                    text = "<script>setTimeout(() => fetch('/redirect307', {method:'POST',body:'synthetic'}).catch(()=>{}), 150)</script>"
                elif app.html is not None:
                    text = app.html
                else:
                    script = ""
                    if kind == "page-socket":
                        script = (
                            "new WebSocket("
                            + json.dumps(app.base.replace("http:", "ws:") + "/ws")
                            + ");"
                        )
                    elif kind:
                        constructor = "SharedWorker" if kind == "shared" else "Worker"
                        script = f"new {constructor}('/worker.js?kind={kind}');"
                    text = (
                        "<!doctype html><script>setTimeout(() => {const b=document.createElement('button');"
                        "b.id='ready';b.textContent='Continue';document.body.append(b);}, 300);"
                        "try {"
                        + script
                        + "} catch(e) {}</script><body><h1>Synthetic form</h1>"
                        "<form method=post action='/write'><input name=record value=expected>"
                        "<input name=value value=synthetic><button id=save>Save</button></form>"
                        "<form method=post action='/redirect307'><button id=redirect>Redirect</button></form>"
                    )
                    if app.frame_url:
                        text += (
                            "<iframe src=" + json.dumps(app.frame_url) + "></iframe>"
                        )
                body = text.encode()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def writes(self) -> list[tuple[str, str]]:
        return [r for r in self.received if r[1] in ("/write", "/ws")]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


@pytest.fixture
def app():
    found = App()
    try:
        yield found
    finally:
        found.close()


@pytest.fixture
def plain_browser():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            browser.close()


class Adapter:
    def claim(self, method: str, url: str, body: bytes | None) -> WriteClaim | None:
        if urlsplit(url).path != "/write":
            return None
        values = parse_qs(
            urlsplit(url).query if method == "GET" else (body or b"").decode()
        )
        return WriteClaim(
            record={"record": values.get("record", [""])[0]},
            fields={"value": values.get("value", [""])[0]},
        )


def scope(app: App) -> TeachScope:
    return TeachScope(
        task_id="browser-tool.synthetic",
        origins=(app.base,),
        record={"record": "expected"},
        fields={"value": "synthetic"},
        write_adapter=Adapter(),
        allowed_write_paths=("/redirect307",),
    )


def program(app: App, attempt: str = "") -> dict[str, Any]:
    return {
        "origin": app.base,
        "url": app.base + "/?attempt=" + attempt,
        "steps": [{"op": "click", "target": {"css": "#ready", "label": "Continue"}}],
    }


@pytest.mark.parametrize(
    "attempt,method", [("shared", "POST"), ("dedicated", "POST"), ("socket", "GET")]
)
def test_worker_capability_control(plain_browser, app, attempt, method):
    context = plain_browser.new_context(service_workers="block")
    try:
        page = context.new_page()
        page.goto(app.base + "/?attempt=" + attempt)
        page.locator("#ready").wait_for()
        page.wait_for_timeout(300)
        assert any(r[0] == method for r in app.writes), app.received
    finally:
        context.close()


@pytest.mark.parametrize("attempt", ["shared", "dedicated", "socket"])
def test_teaching_worker_attempt_is_refused(app, tmp_path, attempt):
    browser = bt._Browser(
        app.base,
        tmp_path,
        True,
        allow_writes=True,
        allowed_writes=("POST /write",),
        scope=scope(app),
    )
    try:
        browser.page.goto(app.base + "/?attempt=" + attempt)
        browser.page.locator("#ready").wait_for()
        browser.page.wait_for_timeout(100)
        browser.settle()
        assert browser.blocked, "a blocked worker attempt must halt teaching"
        assert not browser.containment.ok
        assert worker_containment.WORKER_REFUSED in browser.containment.violations
        assert worker_containment.WORKER_STARTED not in browser.containment.violations
        assert app.writes == []
    finally:
        browser.close()


@pytest.mark.timeout(20)
@pytest.mark.parametrize("attempt", ["shared", "dedicated", "socket", "page-socket"])
def test_every_compiled_call_refuses_the_attempt(app, tmp_path, attempt):
    for _call in range(2):
        result = bt._run_steps(
            program(app, attempt),
            {},
            tmp_path,
            True,
            allow_writes=True,
            allowed_writes=("POST /write",),
            pinned=[],
        )
        assert result.stopped is not None
        assert result.blocked, "a later call must retain the browser refusal"
        assert app.ws_handshakes == 0
        assert app.ws_frames == []
        assert app.writes == []


@pytest.mark.timeout(15)
def test_page_websocket_capability_control(plain_browser, app):
    page = plain_browser.new_page()
    try:
        page.goto(app.base)
        page.evaluate(
            "url => {const ws=new WebSocket(url);ws.onopen=()=>ws.send('synthetic frame');}",
            app.base.replace("http:", "ws:") + "/ws",
        )
        assert app.frame_received.wait(2)
        assert app.ws_handshakes == 1
        assert app.ws_frames == [b"synthetic frame"]
    finally:
        page.close()


@pytest.mark.timeout(15)
def test_teaching_page_websocket_is_refused(app, tmp_path):
    browser = bt._Browser(
        app.base,
        tmp_path,
        True,
        allow_writes=True,
        allowed_writes=("POST /write",),
        scope=scope(app),
    )
    try:
        browser.page.goto(app.base + "/?attempt=page-socket")
        browser.page.locator("#ready").wait_for()
        browser.settle()
        assert any("WebSocket" in reason for reason in browser.blocked)
        assert browser._pending_sockets == []
        assert app.ws_handshakes == 0
        assert app.ws_frames == []
        assert app.writes == []
    finally:
        browser.close()


@pytest.mark.timeout(15)
def test_websocket_close_error_keeps_the_refusal(app, tmp_path, monkeypatch):
    browser = bt._Browser(app.base, tmp_path, True, allow_writes=True)
    try:
        browser.page.goto(app.base + "/?attempt=page-socket")
        browser.page.locator("#ready").wait_for()
        assert len(browser._pending_sockets) == 1

        def unreadable_close(**kwargs):
            raise RuntimeError("synthetic close failure")

        monkeypatch.setattr(browser._pending_sockets[0], "close", unreadable_close)
        browser.settle()
        assert "a refused WebSocket could not be closed" in browser.blocked
        assert browser._pending_sockets == []
        assert app.ws_handshakes == 0
        assert app.ws_frames == []
        assert app.writes == []
    finally:
        browser.close()
    assert browser._closed


def test_containment_problem_refuses_before_navigation(app, tmp_path, monkeypatch):
    monkeypatch.setattr(
        worker_containment,
        "contain_workers",
        lambda *a: worker_containment.WorkerContainment(problem="coverage unavailable"),
    )
    with pytest.raises(BridgeError, match="coverage unavailable"):
        bt._Browser(app.base, tmp_path, True, allow_writes=True)
    assert app.received == []


def test_containment_state_is_checked_without_callback(app, tmp_path):
    browser = bt._Browser(app.base, tmp_path, True, allow_writes=True)
    try:
        browser.containment.violations.append("synthetic lost callback")
        browser.settle()
        assert "synthetic lost callback" in browser.blocked
    finally:
        browser.close()


def test_redirected_post_is_checked_before_second_hop(app, tmp_path):
    browser = bt._Browser(
        app.base,
        tmp_path,
        True,
        allow_writes=True,
        allowed_writes=("POST /redirect307",),
        scope=scope(app),
    )
    try:
        browser.page.goto(app.base)
        with pytest.raises(Exception, match="Failed to fetch"):
            browser.page.evaluate(
                "fetch('/redirect307', {method:'POST',body:'synthetic'})"
            )
        browser.settle()
        assert ("POST", "/redirect307") in app.received
        assert browser.blocked
        assert app.writes == []
    finally:
        browser.close()


def test_allowed_save_and_303_preserve_one_write(app, tmp_path):
    browser = bt._Browser(
        app.base,
        tmp_path,
        True,
        allow_writes=True,
        allowed_writes=("POST /write",),
        scope=scope(app),
    )
    try:
        browser.page.goto(app.base)
        browser.page.locator("#save").click()
        browser.settle()
        assert browser.page.url == app.base + "/final"
        assert browser.blocked == []
        assert app.writes == [("POST", "/write")]
        assert browser.writes_sent == ["POST /write"]
    finally:
        browser.close()


def test_declared_get_write_is_attributed_and_pinned(app, tmp_path):
    browser = bt._Browser(
        app.base,
        tmp_path,
        True,
        allow_writes=True,
        allowed_writes=("GET /write",),
        scope=scope(app),
    )
    try:
        browser.page.goto(app.base)
        with pytest.raises(Exception, match="Failed to fetch"):
            browser.page.evaluate("fetch('/write?record=other&value=synthetic')")
        browser.settle()
        assert browser.blocked
        assert app.writes == []
        assert browser.writes_sent == []
    finally:
        browser.close()
    request = SimpleNamespace(
        method="GET", url=app.base + "/write?record=expected&value=synthetic"
    )
    pinned = [{"write": "GET /write", "shape": body_shape(request)}]
    browser = bt._Browser(
        app.base,
        tmp_path,
        True,
        allow_writes=True,
        allowed_writes=("GET /write",),
        pinned=pinned,
    )
    try:
        browser.page.goto(app.base)
        with pytest.raises(Exception, match="Failed to fetch"):
            browser.page.evaluate(
                "fetch('/write?record=expected&value=synthetic&extra=value')"
            )
        browser.settle()
        assert browser.blocked
        assert app.writes == []
    finally:
        browser.close()
    browser = bt._Browser(
        app.base,
        tmp_path,
        True,
        allow_writes=True,
        allowed_writes=("GET /write",),
        pinned=pinned,
    )
    try:
        browser.page.goto(app.base)
        browser.page.evaluate("fetch('/write?record=expected&value=synthetic')")
        browser.settle()
        assert browser.blocked == []
        assert browser.writes_sent == ["GET /write"]
        assert app.writes == [("GET", "/write")]
    finally:
        browser.close()


WRAPPED = """<!doctype html><style>body{margin:20px}a{font:20px/70px sans-serif}</style>
<a id=wrapped aria-label='Open report' href='#' onclick="window.clicked=[event.clientX,event.clientY]; return false"><span>Delete record</span><br><span>Continue</span></a>"""


def test_wrapped_link_gap_is_not_the_click_point(plain_browser, app):
    app.html = WRAPPED
    page = plain_browser.new_page()
    try:
        page.goto(app.base)
        el = page.locator("#wrapped")
        box = el.bounding_box()
        labels = control_labels_at(
            page, int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)
        )
        assert labels is not None and "Delete record" not in labels
        el.click()
        assert page.evaluate("window.clicked") is not None
        with pytest.raises(bt.StepStopped, match="forbids this control"):
            bt._check_forbidden(
                page,
                SimpleNamespace(forbidden_controls={"delete record"}),
                "click",
                el,
                None,
            )
        assert app.writes == []
    finally:
        page.close()


def test_guarded_fragment_click_uses_the_checked_point(plain_browser, app):
    app.html = WRAPPED.replace("Delete record", "Open report")
    page = plain_browser.new_page()
    try:
        page.goto(app.base)
        point = bt._check_forbidden(
            page,
            SimpleNamespace(forbidden_controls={"delete record"}),
            "click",
            page.locator("#wrapped"),
            None,
        )
        assert point is not None
        page.mouse.click(*point)
        assert page.evaluate("window.clicked") == list(point)
    finally:
        page.close()


def test_tab_enter_checks_visible_name_as_well_as_accessible_name(plain_browser, app):
    app.html = """<button id=start>Start</button><div id=danger role=button tabindex=0 aria-label='Open report' onkeydown="if(event.key==='Enter') fetch('/write',{method:'POST',body:'synthetic'})">Delete record</div>"""
    page = plain_browser.new_page()
    try:
        page.goto(app.base)
        page.locator("#start").focus()
        page.keyboard.press("Tab")
        assert page.locator("#danger").evaluate("e=>e===document.activeElement")
        with pytest.raises(bt.StepStopped, match="forbids this control"):
            bt._check_forbidden(
                page,
                SimpleNamespace(forbidden_controls={"delete record"}),
                "press",
                None,
                "Enter",
            )
        assert app.writes == []
        # Positive capability control: the key really would reach a write.
        with page.expect_request("**/write"):
            page.keyboard.press("Enter")
        page.wait_for_timeout(100)
        assert app.writes == [("POST", "/write")]
    finally:
        page.close()


def test_same_process_frame_redirect_is_guarded(app, tmp_path):
    app.frame_url = app.base + "/frame-redirect"
    browser = bt._Browser(
        app.base,
        tmp_path,
        True,
        allow_writes=True,
        allowed_writes=("POST /redirect307",),
    )
    try:
        browser.page.goto(app.base)
        browser.page.wait_for_timeout(400)
        browser.settle()
        assert ("POST", "/redirect307") in app.received
        assert browser.blocked
        assert app.writes == []
    finally:
        browser.close()


@pytest.fixture
def separate_frame_channel(monkeypatch):
    # Headless shell keeps these loopback frames in one process. The public
    # Chromium channel's new headless mode creates actual child targets.
    from playwright.sync_api import BrowserType

    launch = BrowserType.launch

    def new_headless(browser_type, *args, **kwargs):
        assert kwargs.get("headless") is True
        kwargs["channel"] = "chromium"
        return launch(browser_type, *args, **kwargs)

    monkeypatch.setattr(BrowserType, "launch", new_headless)


def _separate_frame_document(app, child):
    child.html = """<button id=post onclick="fetch('/write',{method:'POST',body:'synthetic'}).catch(()=>{})">Post</button><button id=socket onclick="const ws=new WebSocket(location.origin.replace('http:','ws:')+'/ws');ws.onopen=()=>ws.send('synthetic frame')">Socket</button>"""
    app.frame_url = child.base.replace("127.0.0.1", "localhost") + "/"


def _assert_separate_frame(context, page, frame):
    session = context.new_cdp_session(frame)
    try:
        child = session.send("Target.getTargetInfo", {})["targetInfo"]
        main_session = context.new_cdp_session(page)
        try:
            main = main_session.send("Target.getTargetInfo", {})["targetInfo"]
        finally:
            main_session.detach()
        assert child["type"] == "iframe"
        assert child["targetId"] != main["targetId"]
    finally:
        session.detach()  # requires an actual independent session, never skipped


@pytest.mark.timeout(20)
def test_separate_frame_capability_control(app, separate_frame_channel):
    child = App()
    _separate_frame_document(app, child)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context()
        try:
            page = context.new_page()
            page.goto(app.base)
            frame = next(f for f in page.frames if f is not page.main_frame)
            frame.locator("#post").wait_for()
            _assert_separate_frame(context, page, frame)
            frame.locator("#post").click()
            page.wait_for_timeout(100)
            assert child.writes == [("POST", "/write")]
            frame.locator("#socket").click()
            assert child.frame_received.wait(2)
            assert child.ws_handshakes == 1
            assert child.ws_frames == [b"synthetic frame"]
        finally:
            context.close()
            browser.close()
            child.close()


@pytest.mark.timeout(20)
def test_uncovered_separate_frame_refuses_before_request(
    app, tmp_path, separate_frame_channel
):
    child = App()
    _separate_frame_document(app, child)
    browser = bt._Browser(
        app.base, tmp_path, True, allow_writes=True, allowed_writes=("POST /write",)
    )
    try:
        browser.page.goto(app.base)
        frame = next(f for f in browser.page.frames if f is not browser.page.main_frame)
        frame.locator("#post").wait_for()
        _assert_separate_frame(browser.context, browser.page, frame)
        frame.evaluate(
            "() => {document.querySelector('#post').click();document.querySelector('#socket').click();}"
        )
        browser.page.wait_for_timeout(150)
        browser.settle()
        assert any("frame" in reason for reason in browser.blocked)
        assert any("WebSocket" in reason for reason in browser.blocked)
        assert child.writes == []
        assert child.ws_handshakes == 0
        assert child.ws_frames == []
    finally:
        browser.close()
        child.close()


@pytest.mark.timeout(15)
def test_declared_get_write_rejects_a_blank_extra_field(app, tmp_path):
    request = SimpleNamespace(
        method="GET", url=app.base + "/write?record=expected&value=synthetic"
    )
    pinned = [{"write": "GET /write", "shape": body_shape(request)}]
    browser = bt._Browser(
        app.base,
        tmp_path,
        True,
        allow_writes=True,
        allowed_writes=("GET /write",),
        pinned=pinned,
    )
    try:
        browser.page.goto(app.base)
        error = None
        try:
            browser.page.evaluate(
                "fetch('/write?record=expected&value=synthetic&extra=')"
            )
        except Exception as exc:
            error = str(exc)
        browser.settle()
        assert error is not None and "Failed to fetch" in error, {
            "error": error,
            "received": app.received,
            "sent": browser.writes_sent,
        }
        assert browser.blocked
        assert app.writes == []
        assert browser.writes_sent == []
    finally:
        browser.close()

"""A create-new form's save is checked on the wire, not in the page.

The #1581 round-3 review found that page listeners which run after every
in-page check (a form submit listener, a document capture listener, a
formdata listener) can still change what a submission carries. So the
Playwright backend holds the save's request at the network and lets it go
only when its method, address and fields equal what the checked form would
send.

Real headless Chromium; the server records every request it receives, so
each case asserts what actually arrived.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import parse_qs, parse_qsl, urlsplit

import pytest

from openadapt_flow.compiler import compile_recording
from openadapt_flow.interactive_recorder import record_interactive
from openadapt_flow.ir import Workflow

sync_api = pytest.importorskip("playwright.sync_api")

pytestmark = pytest.mark.timeout(900)

_SCRIPTS = {
    # A form submit listener copies the app's current parent id in.
    "sub7": "window.currentPid = '7';",
    "sub9": "window.currentPid = '9';",
    # A document capture listener adds a record id: an edit-shaped submit.
    "bubble42": """
document.addEventListener('submit', (e) => {
  const h = document.createElement('input'); h.type = 'hidden'; h.name = 'id';
  h.value = '42'; e.target.appendChild(h);
}, true);""",
    # A formdata listener injects a record id into the entry list.
    "fd42": """
document.getElementById('ticket').addEventListener('formdata', (e) => {
  e.formData.set('id', '42');
});""",
    # An SPA: the submit listener sends the form as JSON with fetch.
    "fetch": """
document.getElementById('ticket').addEventListener('submit', (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target, e.submitter).entries());
  fetch('/ticket', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(data)}).then(() => {
      document.body.innerHTML = '<h1>Ticket created</h1>';
    });
});""",
    "fetch42": """
document.getElementById('ticket').addEventListener('submit', (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target, e.submitter).entries());
  data.id = '42';
  fetch('/ticket', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(data)}).then(() => {
      document.body.innerHTML = '<h1>Ticket created</h1>';
    });
});""",
    # The click opens a popup whose page posts a write of its own.
    "popup": """
document.getElementById('ticket').addEventListener('submit', () => {
  window.open('/popup', '_blank');
}, true);""",
    # A service worker controls the page: its requests skip page routes.
    "sw": """
navigator.serviceWorker.register('/sw.js');""",
}
# The save opens a WebSocket and sends on it. It saves with fetch, so the
# page stays put while the save is held (a native save navigates, and the
# socket goes with the page).
_SCRIPTS["ws"] = """
document.getElementById('ticket').addEventListener('submit', () => {
  const ws = new WebSocket('ws://' + location.host + '/ws');
  ws.onopen = () => ws.send('close-account');
}, true);""" + _SCRIPTS["fetch"]
_POPUP = """<!doctype html><title>Popup</title>
<form method="post" action="/account/close"><input name="confirm" value="yes"></form>
<script>document.forms[0].submit();</script>"""
_SERVICE_WORKER = """
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', (e) => e.respondWith(fetch(e.request)));
"""
_SUBMIT_COPIES = """
document.getElementById('ticket').addEventListener('submit', () => {
  document.querySelector('[name=pid]').value = window.currentPid;
}, true);"""


def _page(variant: str) -> str:
    pid = (
        '<input type="hidden" name="pid" value="">' if variant.startswith("sub") else ""
    )
    scripts = _SCRIPTS.get(variant, "") + (
        _SUBMIT_COPIES if variant.startswith("sub") else ""
    )
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>New ticket</title>
</head><body style="font:16px Arial;margin:24px"><h1>New ticket</h1>
<form id="ticket" method="post" action="/ticket">
  {pid}
  <p><label>Summary <input name="field_summary" size="40"></label></p>
  <p><label>Description <textarea name="field_description" rows="3"
     cols="40"></textarea></label></p>
  <p><input type="submit" name="submit" value="Create ticket"></p>
</form>
<script>{scripts}</script>
</body></html>"""


_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class _Server:
    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (http.server API)
                path = urlsplit(self.path).path
                if path == "/ws":
                    self._accept_websocket()
                elif path == "/popup":
                    self._send(_POPUP)
                elif path == "/sw.js":
                    self._send(_SERVICE_WORKER, "text/javascript")
                else:
                    query = parse_qs(urlsplit(self.path).query)
                    self._send(_page(query.get("v", ["plain"])[0]))

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode()
                if "json" in (self.headers.get("Content-Type") or ""):
                    fields = json.loads(body)
                else:
                    fields = dict(parse_qsl(body, keep_blank_values=True))
                server.received.append({"path": self.path, "fields": fields})
                self._send(
                    "<!doctype html><title>Created</title><h1>Ticket created</h1>"
                )

            def _accept_websocket(self) -> None:
                # A real handshake, so a socket that got through would
                # deliver its message here.
                key = self.headers["Sec-WebSocket-Key"] + _WS_GUID
                accept = base64.b64encode(hashlib.sha1(key.encode()).digest())
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept.decode())
                self.end_headers()
                self.wfile.flush()
                head = self.rfile.read(2)
                if len(head) < 2:
                    return
                length = head[1] & 0x7F
                if length == 126:
                    length = int.from_bytes(self.rfile.read(2), "big")
                mask = self.rfile.read(4)
                data = bytes(
                    b ^ mask[i % 4] for i, b in enumerate(self.rfile.read(length))
                )
                server.received.append(
                    {"path": "/ws", "fields": {"message": data.decode()}}
                )
                self.close_connection = True

            def _send(self, html: str, ctype: str = "text/html") -> None:
                body = html.encode()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                pass

        self.http = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.http.server_address[1]}/newticket"


@pytest.fixture(scope="module")
def server() -> Iterator[_Server]:
    srv = _Server()
    yield srv
    srv.http.shutdown()


_BUNDLES: dict[str, Path] = {}


def _bundle(server: _Server, variant: str, root: Path) -> Path:
    if variant in _BUNDLES:
        return _BUNDLES[variant]
    out = root / variant

    def drive(page: Any, pump: Callable[[], Any]) -> None:
        def settle() -> None:
            for _ in range(12):
                pump()

        page.wait_for_selector("[name=field_summary]", state="visible")
        page.locator("[name=field_summary]").click()
        settle()
        page.keyboard.type("Printer jams on tray 2")
        settle()
        page.locator("[name=field_description]").click()
        settle()
        page.keyboard.type("Seen twice today")
        settle()
        page.locator("[name=submit]").click()
        settle()
        # The save navigates: let the recorder see the page it lands on.
        for _ in range(30):
            pump()

    rec = record_interactive(
        f"{server.url}?v={variant}",
        out / "rec",
        headless=True,
        script=drive,
        param_fields=("field_summary", "field_description"),
    )
    compile_recording(rec, out / "bundle", name="newticket")
    # A slow recording can leave the save unarmed; fail here, not later.
    assert Workflow.load(out / "bundle").steps[-1].anchor.form_identity is not None
    _BUNDLES[variant] = out / "bundle"
    return out / "bundle"


def _replay(
    server: _Server,
    variant: str,
    bundle: Path,
    run_dir: Path,
    *,
    service_workers: str = "allow",
) -> tuple[Any, list]:
    from openadapt_flow.backends.playwright_backend import (
        PlaywrightBackend,
        prepare_ws_guard,
    )
    from openadapt_flow.runtime import Replayer

    before = len(server.received)
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(
                viewport={"width": 1280, "height": 800},
                service_workers=service_workers,  # type: ignore[arg-type]
            )
            # As the runner does: guarded before the first document.
            prepare_ws_guard(page.context)
            page.goto(f"{server.url}?v={variant}")
            page.wait_for_selector("[name=field_summary]", state="visible")
            if variant == "sw" and service_workers == "allow":
                page.wait_for_function("!!navigator.serviceWorker.controller")
            report = Replayer(PlaywrightBackend(page)).run(
                Workflow.load(bundle),
                params={
                    "field_summary": "Scanner offline on floor 3",
                    "field_description": "Since this morning",
                },
                bundle_dir=bundle,
                run_dir=run_dir,
            )
            page.wait_for_timeout(500)
        finally:
            browser.close()
    return report, server.received[before:]


@pytest.mark.parametrize("variant", ["plain", "sub7", "fetch"])
def test_the_checked_submission_goes_out_once(
    server: _Server, tmp_path_factory: pytest.TempPathFactory, variant: str
) -> None:
    bundle = _bundle(server, variant, tmp_path_factory.mktemp("rec"))
    assert Workflow.load(bundle).steps[-1].anchor.form_identity is not None
    report, received = _replay(server, variant, bundle, tmp_path_factory.mktemp("run"))
    if variant == "sub7":
        # The submit listener fills the pid after the check, even on the
        # demonstrated page: what is sent differs from the checked form, so
        # it is held (a safe-direction false halt, documented).
        assert not report.success
        assert received == []
        return
    assert report.success, [(r.step_id, r.error) for r in report.results]
    assert len(received) == 1
    assert received[0]["fields"]["field_summary"] == "Scanner offline on floor 3"
    assert "id" not in received[0]["fields"]


@pytest.mark.parametrize(
    "recorded, replayed",
    [("sub7", "sub9"), ("plain", "bubble42"), ("plain", "fd42"), ("fetch", "fetch42")],
)
def test_a_submission_changed_after_the_check_never_leaves(
    server: _Server,
    tmp_path_factory: pytest.TempPathFactory,
    recorded: str,
    replayed: str,
) -> None:
    bundle = _bundle(server, recorded, tmp_path_factory.mktemp("rec"))
    report, received = _replay(server, replayed, bundle, tmp_path_factory.mktemp("run"))
    assert not report.success
    assert received == [], received
    errors = " ".join(r.error or "" for r in report.results)
    assert (
        "differs from the checked form" in errors
        or "changed after it was checked" in errors
    )


def test_a_write_from_a_popup_never_leaves(
    server: _Server, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A popup is another page: the wire check holds its requests too."""

    bundle = _bundle(server, "plain", tmp_path_factory.mktemp("rec"))
    report, received = _replay(server, "popup", bundle, tmp_path_factory.mktemp("run"))
    assert "/account/close" not in [r["path"] for r in received], received
    assert not report.success
    errors = " ".join(r.error or "" for r in report.results)
    # The save or the popup's write goes first; either way only the save left.
    assert (
        "different address than the checked form's" in errors
        or "second submission" in errors
    ), errors


def test_a_page_a_service_worker_controls_is_refused(
    server: _Server, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A service worker's requests skip routes: refuse before the click."""

    bundle = _bundle(server, "plain", tmp_path_factory.mktemp("rec"))
    report, received = _replay(server, "sw", bundle, tmp_path_factory.mktemp("run"))
    assert not report.success
    assert received == [], received
    errors = " ".join(r.error or "" for r in report.results)
    assert "service worker" in errors, errors
    # With service workers blocked (as the runner's own browsers are), the
    # same page saves.
    report, received = _replay(
        server,
        "sw",
        bundle,
        tmp_path_factory.mktemp("run"),
        service_workers="block",
    )
    assert report.success, [(r.step_id, r.error) for r in report.results]
    assert len(received) == 1


def test_a_websocket_opened_by_the_save_halts_the_run(
    server: _Server, tmp_path_factory: pytest.TempPathFactory
) -> None:
    bundle = _bundle(server, "fetch", tmp_path_factory.mktemp("rec"))
    report, received = _replay(server, "ws", bundle, tmp_path_factory.mktemp("run"))
    assert not report.success
    assert "/ws" not in [r["path"] for r in received], received
    errors = " ".join(r.error or "" for r in report.results)
    assert "WebSocket" in errors, errors


def _browser_page(playwright: Any, *, guarded: bool = True) -> Any:
    """A page in a fresh context, guarded before its first document (as the
    runner's pages are) unless ``guarded`` is False."""

    from openadapt_flow.backends.playwright_backend import prepare_ws_guard

    browser = playwright.chromium.launch(headless=True)
    page = browser.new_context(service_workers="block").new_page()
    if guarded:
        prepare_ws_guard(page.context)
    return browser, page


def _expected(server: _Server) -> Any:
    from openadapt_flow.wire_check import ExpectedSubmission

    origin = server.url.rsplit("/", 1)[0]
    return ExpectedSubmission(method="POST", address=f"{origin}/ticket")


def _hold(backend: Any, server: _Server) -> None:
    held = backend._arm_wire_check(_expected(server))
    assert held is None, held


def _ws_url(server: _Server) -> str:
    return "ws://" + server.url.split("://", 1)[1].rsplit("/", 1)[0] + "/ws"


def _leaked(server: _Server, before: int, marker: str) -> list[dict[str, Any]]:
    return [r for r in server.received[before:] if marker in json.dumps(r)]


def _messages(server: _Server, before: int) -> list[str]:
    return [
        r["fields"]["message"] for r in server.received[before:] if r["path"] == "/ws"
    ]


# The page keeps the browser's own constructors and an open socket, then
# reaches for a connection every other way while a save is held (the #1581
# reviews' probes). ``M`` marks what it sends; it must never arrive.
_KEEP_JS = """(U) => new Promise((done) => {
  window.__kept = WebSocket; window.__keptWorker = Worker;
  window.__keptSend = WebSocket.prototype.send;
  const s = new WebSocket(U); window.__open = s;
  s.onopen = () => done(true); setTimeout(() => done(false), 3000);
})"""
_WORKER_SRC = (
    "`const s = new WebSocket(${JSON.stringify(U)});"
    " s.onopen = () => s.send(${JSON.stringify(M)});`"
)
_REACHES = {
    "prototype_constructor": "new WebSocket.prototype.constructor(U)",
    "constructor_prototype": "new (Object.getPrototypeOf(WebSocket))(U)",
    "instance_constructor": (
        "new (Object.getPrototypeOf(WebSocket.prototype).constructor)(U)"
    ),
    "new_iframe": (
        "(() => { const f = document.createElement('iframe');"
        " document.body.appendChild(f); return new f.contentWindow.WebSocket(U); })()"
    ),
    "new_iframe_prototype": (
        "(() => { const f = document.createElement('iframe');"
        " document.body.appendChild(f);"
        " return new f.contentWindow.WebSocket.prototype.constructor(U); })()"
    ),
    "blob_iframe": (
        "(() => { const f = document.createElement('iframe');"
        " f.src = URL.createObjectURL(new Blob([`<script>${" + _WORKER_SRC + "}"
        "<\\/script>`], {type: 'text/html'})); document.body.appendChild(f);"
        " return null; })()"
    ),
    "data_iframe": (
        "(() => { const f = document.createElement('iframe');"
        " f.src = 'data:text/html,' + encodeURIComponent(`<script>${"
        + _WORKER_SRC
        + "}<\\/script>`); document.body.appendChild(f); return null; })()"
    ),
    "new_blank_window": "new (window.open('about:blank').WebSocket)(U)",
    "worker": ("new Worker(URL.createObjectURL(new Blob([" + _WORKER_SRC + "])))"),
    "kept_worker": (
        "new window.__keptWorker(URL.createObjectURL(new Blob([" + _WORKER_SRC + "])))"
    ),
    "kept_reference": "new window.__kept(U)",
    "kept_send_on_open_socket": "(window.__keptSend.call(window.__open, M), null)",
    "send_on_open_socket": "(window.__open.send(M), null)",
    "beacon": "(navigator.sendBeacon('/beacon', M), null)",
    "fetch_keepalive": (
        "(fetch('/ka', {method: 'POST', body: M, keepalive: true})"
        ".catch(() => 0), null)"
    ),
}
_REACH_JS = """([U, M, E]) => {
  try {
    const s = eval(E);
    if (s && typeof s.send === 'function') {
      s.onopen = () => s.send(M);
      return 'constructed';
    }
    return s instanceof Worker ? 'constructed' : 'no socket';
  } catch (e) { return 'refused'; }
}"""


@pytest.mark.parametrize("reach", sorted(_REACHES))
def test_no_route_to_the_wire_while_held(server: _Server, reach: str) -> None:
    """On a page guarded from its first document, nothing the page sends
    while the save is held reaches the server, and the run is told."""

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    before = len(server.received)
    with sync_api.sync_playwright() as playwright:
        browser, page = _browser_page(playwright)
        try:
            page.goto(f"{server.url}?v=fetch")
            assert page.evaluate(_KEEP_JS, _ws_url(server))
            backend = PlaywrightBackend(page)
            _hold(backend, server)
            opened = page.evaluate(_REACH_JS, [_ws_url(server), reach, _REACHES[reach]])
            page.wait_for_timeout(800)
            refusal = backend.form_submit_guard_refusal(disarm=True)
            page.wait_for_timeout(500)
        finally:
            browser.close()
    assert _leaked(server, before, reach) == []
    # A lookup that lands on EventTarget makes no socket: nothing to report.
    if opened != "no socket":
        assert refusal is not None, (opened, refusal)


def test_a_page_loaded_before_the_guard_cannot_hold_a_save(
    server: _Server,
) -> None:
    """Its scripts may have kept the browser's own WebSocket, Worker or
    send before any guard existed: refuse the save rather than claim it."""

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    before = len(server.received)
    with sync_api.sync_playwright() as playwright:
        browser, page = _browser_page(playwright, guarded=False)
        try:
            page.goto(f"{server.url}?v=fetch")
            assert page.evaluate(_KEEP_JS, _ws_url(server))
            backend = PlaywrightBackend(page)
            refusal = backend._arm_wire_check(_expected(server))
            assert not backend.form_submit_guard_armed
            # The same page, reloaded after the guard exists, can.
            page.reload()
            reloaded = backend._arm_wire_check(_expected(server))
            backend.form_submit_guard_refusal(disarm=True)
        finally:
            browser.close()
    assert refusal is not None and "loaded before" in refusal, refusal
    assert reloaded is None, reloaded
    assert _messages(server, before) == []


@pytest.mark.parametrize("decide", ["at_once", "after_settle"])
def test_a_websocket_opened_while_held_never_connects(
    server: _Server, decide: str
) -> None:
    """A socket the page opens while the save is held never reaches the
    server, whenever the run decides its outcome."""

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    before = len(server.received)
    with sync_api.sync_playwright() as playwright:
        browser, page = _browser_page(playwright)
        try:
            page.goto(f"{server.url}?v=fetch")
            backend = PlaywrightBackend(page)
            _hold(backend, server)
            opened = page.evaluate(
                _REACH_JS, [_ws_url(server), "held", "new WebSocket(U)"]
            )
            if decide == "after_settle":
                backend.wait_form_submit_guard(500)
            refusal = backend.form_submit_guard_refusal(disarm=True)
            assert not backend.form_submit_guard_armed
            page.wait_for_timeout(1000)
        finally:
            browser.close()
    assert opened == "refused"
    assert _messages(server, before) == []
    assert refusal is not None and "WebSocket" in refusal, refusal


def test_websockets_connect_promptly_while_python_is_busy(server: _Server) -> None:
    """With nothing held (before and after a save), a socket connects
    without waiting for Python: nothing routes it through the driver."""

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    before = len(server.received)
    with sync_api.sync_playwright() as playwright:
        browser, page = _browser_page(playwright)
        try:
            page.goto(f"{server.url}?v=fetch")
            backend = PlaywrightBackend(page)
            _hold(backend, server)
            backend.form_submit_guard_refusal(disarm=True)
            for marker in ("first", "second"):
                count = len(server.received)
                page.evaluate(_REACH_JS, [_ws_url(server), marker, "new WebSocket(U)"])
                # Python is busy (not servicing the driver) for up to 3 s.
                deadline = time.monotonic() + 3
                while len(server.received) == count and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert len(server.received) > count, f"{marker} socket waited"
        finally:
            browser.close()
    assert _messages(server, before) == ["first", "second"]


def test_websockets_connect_again_once_the_hold_is_released(server: _Server) -> None:
    """The guard refuses sockets only while a save is held: a document
    loaded during the hold starts held, and once the outcome is read every
    document (old or new) connects its sockets as before."""

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    ws_url = _ws_url(server)
    before = len(server.received)
    with sync_api.sync_playwright() as playwright:
        browser, page = _browser_page(playwright)
        try:
            page.goto(f"{server.url}?v=fetch")
            backend = PlaywrightBackend(page)
            _hold(backend, server)
            popup = page.context.new_page()
            popup.goto(f"{server.url}?v=plain")
            assert (
                popup.evaluate(_REACH_JS, [ws_url, "held", "new WebSocket(U)"])
                == "refused"
            )
            refusal = backend.form_submit_guard_refusal(disarm=True)
            assert refusal is not None and "WebSocket" in refusal
            for target, marker in ((page, "old"), (popup, "popup")):
                assert (
                    target.evaluate(_REACH_JS, [ws_url, marker, "new WebSocket(U)"])
                    == "constructed"
                )
            fresh = page.context.new_page()
            fresh.goto(f"{server.url}?v=plain")
            assert (
                fresh.evaluate(_REACH_JS, [ws_url, "new", "new WebSocket(U)"])
                == "constructed"
            )
            page.wait_for_timeout(1500)
        finally:
            browser.close()
    assert sorted(_messages(server, before)) == ["new", "old", "popup"]


def test_a_page_running_a_worker_is_refused_before_the_click(
    server: _Server,
) -> None:
    """A worker's WebSockets skip the page guard and the routes."""

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    with sync_api.sync_playwright() as playwright:
        browser, page = _browser_page(playwright)
        try:
            page.goto(f"{server.url}?v=fetch")
            with page.expect_worker():
                page.evaluate(
                    "() => { window.__w = new Worker(URL.createObjectURL("
                    "new Blob(['setInterval(() => 0, 1000)']))); }"
                )
            refusal = PlaywrightBackend(page)._arm_wire_check(_expected(server))
        finally:
            browser.close()
    assert refusal is not None and "worker" in refusal, refusal


def test_a_launched_page_is_guarded_before_its_first_document(
    server: _Server,
) -> None:
    """With the guard installed before the first load (as ``launch`` does),
    even a reference the page kept refuses to open while held."""

    from openadapt_flow.backends.playwright_backend import (
        PlaywrightBackend,
        prepare_ws_guard,
    )

    before = len(server.received)
    with sync_api.sync_playwright() as playwright:
        browser, page = _browser_page(playwright, guarded=False)
        try:
            ws_guard = prepare_ws_guard(page.context)
            assert prepare_ws_guard(page.context) == ws_guard  # once per context
            page.goto(f"{server.url}?v=fetch")
            page.evaluate("() => { window.__kept = WebSocket; }")
            backend = PlaywrightBackend(page, ws_guard=ws_guard)
            assert (
                page.evaluate(
                    _REACH_JS, [_ws_url(server), "free", "new window.__kept(U)"]
                )
                == "constructed"
            )
            page.wait_for_timeout(800)
            _hold(backend, server)
            opened = page.evaluate(
                _REACH_JS, [_ws_url(server), "held", "new window.__kept(U)"]
            )
            refusal = backend.form_submit_guard_refusal(disarm=True)
            page.wait_for_timeout(800)
        finally:
            browser.close()
    assert opened == "refused"
    assert refusal is not None and "WebSocket" in refusal, refusal
    assert _messages(server, before) == ["free"]


def test_holds_on_a_reloading_frame_release_every_frame(server: _Server) -> None:
    """Arming and releasing while an iframe keeps reloading leaves no frame
    held once the reloads stop."""

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    before = len(server.received)
    with sync_api.sync_playwright() as playwright:
        browser, page = _browser_page(playwright)
        try:
            page.goto(f"{server.url}?v=fetch")
            page.evaluate(
                """() => { const f = document.createElement('iframe');
                  f.src = '/newticket?v=plain'; document.body.appendChild(f);
                  window.__reload = setInterval(() => {
                    f.src = '/newticket?v=plain&t=' + Math.random(); }, 30); }"""
            )
            page.wait_for_timeout(300)
            backend = PlaywrightBackend(page)
            for _ in range(10):
                if backend._arm_wire_check(_expected(server)) is None:
                    backend.form_submit_guard_refusal(disarm=True)
            page.evaluate("() => clearInterval(window.__reload)")
            page.wait_for_timeout(500)
            opened = [
                frame.evaluate(
                    _REACH_JS, [_ws_url(server), f"frame{i}", "new WebSocket(U)"]
                )
                for i, frame in enumerate(page.frames)
            ]
            page.wait_for_timeout(800)
        finally:
            browser.close()
    assert opened == ["constructed"] * len(opened), opened
    assert sorted(_messages(server, before)) == [
        f"frame{i}" for i in range(len(opened))
    ]


# A page that knows about the guard tries to end the hold or to pass for a
# guarded page (the #1644 review's probes).
_TAMPER_JS = """([U, M]) => {
  const tried = [];
  for (const k of Object.getOwnPropertyNames(window)) {
    if (!k.startsWith('__oaflow_ws_')) continue;
    const v = window[k];
    try { if (v.holds) { v.holds.clear(); tried.push('holds'); } } catch (e) {}
    try { v.early = true; } catch (e) {}
    for (const o of [{release: 'x'}, {released: 'x'}, {}]) {
      try { tried.push(JSON.stringify(v(undefined, o))); } catch (e) {}
      try { tried.push(JSON.stringify(v('guess', o))); } catch (e) {}
    }
    try { delete window[k]; } catch (e) {}
    try { window[k] = () => [0, 0, 0, true, 'x']; } catch (e) {}
    try { Object.defineProperty(window, k, {value: 1}); } catch (e) {}
  }
  try {
    const s = new WebSocket(U);
    s.onopen = () => s.send(M);
    return ['constructed', tried];
  } catch (e) { return ['refused', tried]; }
}"""


def test_a_page_cannot_end_the_hold_through_the_guard(server: _Server) -> None:
    """The guard's state is out of the page's reach: what the page can find
    is a fixed function that answers -1 without the secret."""

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    before = len(server.received)
    with sync_api.sync_playwright() as playwright:
        browser, page = _browser_page(playwright)
        try:
            page.goto(f"{server.url}?v=fetch")
            backend = PlaywrightBackend(page)
            _hold(backend, server)
            opened, tried = page.evaluate(_TAMPER_JS, [_ws_url(server), "tamper"])
            refusal = backend.form_submit_guard_refusal(disarm=True)
            page.wait_for_timeout(800)
        finally:
            browser.close()
    assert opened == "refused", tried
    assert all(t == "-1" for t in tried if t != "holds"), tried
    assert "holds" not in tried
    assert refusal is not None and "WebSocket" in refusal, refusal
    assert _leaked(server, before, "tamper") == []


@pytest.mark.parametrize(
    "fake",
    [
        "() => [0, 0, 0, true, 'not the proof']",
        "() => [0, 0, 0, true, null]",
        "() => [0, 0, 0, false, null]",
        "() => -1",
        "({early: true, holds: new Set(), counts: new Map()})",
        "({early: true, holds: new Set(), counts: new Map(), guarded: {},"
        " patched: []})",
    ],
)
def test_a_page_cannot_pass_for_guarded_from_its_start(
    server: _Server, fake: str
) -> None:
    """A document opened before the guard defines the guard's name (as a
    page that learned it could) before the backend reaches it: the save is
    refused, and since anything under that name may have been handed the
    secret, the browser refuses every later hold too, even reloaded."""

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    before = len(server.received)
    with sync_api.sync_playwright() as playwright:
        browser, page = _browser_page(playwright, guarded=False)
        try:
            page.goto(f"{server.url}?v=fetch")
            backend = PlaywrightBackend(page)  # guards the context from now on
            page.evaluate(
                f"""(k) => Object.defineProperty(window, k, {{
                  value: ({fake}), writable: false, configurable: false }})""",
                backend._ws_guard_key,
            )
            refusal = backend._arm_wire_check(_expected(server))
            assert not backend.form_submit_guard_armed
            page.reload()
            # Reloaded, the document is guarded from its start.
            again = backend._arm_wire_check(_expected(server))
            held = (
                page.evaluate(_REACH_JS, [_ws_url(server), "held", "new WebSocket(U)"])
                if again is None
                else None
            )
            backend.form_submit_guard_refusal(disarm=True)
            page.wait_for_timeout(800)
        finally:
            browser.close()
    assert refusal is not None and "answered for the WebSocket guard" in refusal
    assert again is not None and "answered for" in again, again
    assert held is None
    assert _messages(server, before) == []


def test_a_page_cannot_pass_on_a_guarded_frames_answer(server: _Server) -> None:
    """A document opened before the guard puts a function under the guard's
    name that hands each call to the guard of an iframe loaded since (which
    is guarded from its start) and returns its answer: the same document
    answers twice, so the save is refused and the browser holds no more."""

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    with sync_api.sync_playwright() as playwright:
        browser, page = _browser_page(playwright, guarded=False)
        try:
            page.goto(f"{server.url}?v=fetch")
            backend = PlaywrightBackend(page)  # guards the context from now on
            page.evaluate(
                """(k) => new Promise((done) => {
                  const f = document.createElement('iframe');
                  f.src = '/newticket?v=plain'; f.onload = () => done(true);
                  document.body.appendChild(f);
                  Object.defineProperty(window, k, {
                    value: (...a) => f.contentWindow[k](...a),
                    writable: false, configurable: false });
                })""",
                backend._ws_guard_key,
            )
            refusal = backend._arm_wire_check(_expected(server))
            armed = backend.form_submit_guard_armed
        finally:
            browser.close()
    assert not armed
    assert refusal is not None and "answered for the WebSocket guard" in refusal


_PROTOTYPE_HOOK_JS = """() => {
  const k = Object.getOwnPropertyNames(window).find(
    (n) => n.startsWith('__oaflow_ws_'));
  window.__k = k;
  window.__seen = [];
  for (const name of ['start', 'armed', 'released', 'hold', 'release', 'id']) {
    Object.defineProperty(Object.prototype, name, {configurable: true, get() {
      window.__seen.push(name);
      try {
        const a = window[k].arguments;
        if (a && a[0] && !window.__auth) {
          window.__auth = a[0]; window.__opts = JSON.stringify(a[1]);
        }
      } catch (e) { window.__err = e.name; }
      return undefined;
    }});
  }
  let strict = false;
  try { void window[k].arguments; } catch (e) { strict = true; }
  try { void window[k].caller; strict = false; } catch (e) {}
  return [k || null, strict];
}"""


def test_page_getters_never_see_the_guards_secret(server: _Server) -> None:
    """A getter on Object.prototype runs if the guard reads an option
    through the prototype chain; with a sloppy-mode function it could read
    the secret from ``.arguments`` and release the hold. The guard is strict
    and reads its options only as own properties (the #1647 review's
    probe)."""

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    before = len(server.received)
    with sync_api.sync_playwright() as playwright:
        browser, page = _browser_page(playwright)
        try:
            page.goto(f"{server.url}?v=fetch")
            backend = PlaywrightBackend(page)
            key, strict = page.evaluate(_PROTOTYPE_HOOK_JS)
            _hold(backend, server)
            seen, auth = page.evaluate("() => [window.__seen, !!window.__auth]")
            opened = page.evaluate(
                """([U, M]) => {
                  try {
                    if (window.__auth) {
                      const o = JSON.parse(window.__opts);
                      window[window.__k](window.__auth, {release: o.hold});
                    }
                    const s = new WebSocket(U);
                    s.onopen = () => s.send(M);
                    return 'constructed';
                  } catch (e) { return 'refused'; }
                }""",
                [_ws_url(server), "hooked"],
            )
            page.wait_for_timeout(800)
            refusal = backend.form_submit_guard_refusal(disarm=True)
        finally:
            browser.close()
    assert key and strict
    assert seen == [] and not auth, (seen, auth)
    assert opened == "refused"
    assert refusal is not None and "WebSocket" in refusal, refusal
    assert _leaked(server, before, "hooked") == []


_EAVESDROP_JS = """() => {
  // Playwright evaluates through the page's eval: see every call.
  const original = window.eval;
  window.__heard = [];
  window.eval = function (source) {
    window.__heard.push(String(source));
    const f = original(source);
    if (typeof f !== 'function') return f;
    return function (...args) {
      try { window.__heard.push(JSON.stringify(args)); } catch (e) {}
      return f.apply(this, args);
    };
  };
}"""
_FORGE_JS = """([U, M]) => {
  const k = Object.getOwnPropertyNames(window).find(
    (n) => n.startsWith('__oaflow_ws_'));
  const words = new Set();
  for (const heard of window.__heard) {
    for (const m of heard.matchAll(/"([^"]{8,})"/g)) words.add(m[1]);
  }
  let tries = 0;
  for (const x of words) {
    for (const e of words) {
      for (const op of ['release', 'released']) {
        try { window[k](op, x, e, 'n'); tries += 1; } catch (err) {}
      }
      // The earlier guard's form: a secret and an options object.
      try { window[k](x, {id: e, release: e}); tries += 1; } catch (err) {}
    }
  }
  try {
    const s = new WebSocket(U);
    s.onopen = () => s.send(M);
    return ['constructed', tries];
  } catch (e) { return ['refused', tries]; }
}"""


def test_a_page_that_hears_every_call_cannot_end_the_hold(server: _Server) -> None:
    """The page replaces eval, which Playwright evaluates through, so it
    sees every call the backend makes while arming; trying every string it
    heard as a hold's secret still can't end the hold."""

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    before = len(server.received)
    with sync_api.sync_playwright() as playwright:
        browser, page = _browser_page(playwright)
        try:
            page.goto(f"{server.url}?v=fetch")
            backend = PlaywrightBackend(page)
            page.evaluate(_EAVESDROP_JS)
            _hold(backend, server)
            heard = page.evaluate("() => window.__heard.length")
            opened, tries = page.evaluate(_FORGE_JS, [_ws_url(server), "forged"])
            refusal = backend.form_submit_guard_refusal(disarm=True)
            page.wait_for_timeout(800)
        finally:
            browser.close()
    assert heard > 0 and tries > 0, (heard, tries)
    assert opened == "refused"
    assert refusal is not None and "WebSocket" in refusal, refusal
    assert _leaked(server, before, "forged") == []

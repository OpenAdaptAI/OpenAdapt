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
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.runtime import Replayer

    before = len(server.received)
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(
                viewport={"width": 1280, "height": 800},
                service_workers=service_workers,  # type: ignore[arg-type]
            )
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


@pytest.mark.parametrize("decide", ["at_once", "after_settle", "late_route"])
def test_a_websocket_opened_while_held_never_connects(
    server: _Server, monkeypatch: pytest.MonkeyPatch, decide: str
) -> None:
    """A socket the page opens while the save is held never reaches the
    server, however late the backend services it. The page loaded before
    the backend existed (as the runner hands pages over), which Playwright
    1.61 does not route; ``late_route`` runs the socket's route callback
    only after the run decided its outcome and disarmed, as a loaded host
    can."""

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.wire_check import ExpectedSubmission

    deferred: list[tuple[Any, Any]] = []
    route = PlaywrightBackend._route_websocket
    if decide == "late_route":
        monkeypatch.setattr(
            PlaywrightBackend,
            "_route_websocket",
            lambda self, ws: deferred.append((self, ws)),
        )
    before = len(server.received)
    origin = server.url.rsplit("/", 1)[0]
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(f"{server.url}?v=fetch")
            backend = PlaywrightBackend(page)
            held = backend._arm_wire_check(
                ExpectedSubmission(method="POST", address=f"{origin}/ticket")
            )
            assert held is None, held
            opened = page.evaluate(
                """(u) => {
                  try {
                    const ws = new WebSocket(u);
                    ws.onopen = () => ws.send('close-account');
                    return 'constructed';
                  } catch (e) { return 'refused'; }
                }""",
                f"ws://{origin.split('://', 1)[1]}/ws",
            )
            if decide == "after_settle":
                backend.wait_form_submit_guard(500)
            refusal = backend.form_submit_guard_refusal(disarm=True)
            assert not backend.form_submit_guard_armed
            for owner, ws in deferred:
                route(owner, ws)
            page.wait_for_timeout(1000)
        finally:
            browser.close()
    received = server.received[before:]
    assert "/ws" not in [r["path"] for r in received], (opened, received)
    assert refusal is not None and "WebSocket" in refusal, (opened, refusal)


def test_websockets_connect_again_once_the_hold_is_released(server: _Server) -> None:
    """The guard refuses sockets only while a save is held: a document
    loaded during the hold starts held, and once the outcome is read every
    document (old or new) connects its sockets as before."""

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.wire_check import ExpectedSubmission

    origin = server.url.rsplit("/", 1)[0]
    ws_url = f"ws://{origin.split('://', 1)[1]}/ws"
    open_socket = """([u, m]) => {
      try {
        const ws = new WebSocket(u);
        ws.onopen = () => ws.send(m);
        return 'constructed';
      } catch (e) { return 'refused'; }
    }"""
    before = len(server.received)
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_context().new_page()
            page.goto(f"{server.url}?v=fetch")
            backend = PlaywrightBackend(page)
            assert (
                backend._arm_wire_check(
                    ExpectedSubmission(method="POST", address=f"{origin}/ticket")
                )
                is None
            )
            popup = page.context.new_page()
            popup.goto(f"{server.url}?v=plain")
            assert popup.evaluate(open_socket, [ws_url, "held"]) == "refused"
            refusal = backend.form_submit_guard_refusal(disarm=True)
            assert refusal is not None and "WebSocket" in refusal
            assert page.evaluate(open_socket, [ws_url, "old"]) == "constructed"
            assert popup.evaluate(open_socket, [ws_url, "popup"]) == "constructed"
            fresh = page.context.new_page()
            fresh.goto(f"{server.url}?v=plain")
            assert fresh.evaluate(open_socket, [ws_url, "new"]) == "constructed"
            page.wait_for_timeout(1500)
        finally:
            browser.close()
    sent = sorted(
        r["fields"]["message"] for r in server.received[before:] if r["path"] == "/ws"
    )
    assert sent == ["new", "old", "popup"], sent

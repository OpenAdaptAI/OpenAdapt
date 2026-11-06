"""The unpacked extension's network-save capture in real headless Chromium.

Part of OpenAdapt#1872. Loads ``chrome_extension/`` as an unpacked MV3
extension, posts to a local HTTP server with credentials in headers, cookies,
the query string and the JSON body, and reads what the service worker would
send to the native host. Only ``chrome.runtime.connectNative`` is replaced
(inside the service worker) so the outgoing messages can be read; the
``webRequest`` events and the redactor are Chrome's and the extension's own.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

EXT = Path(__file__).resolve().parents[1] / "chrome_extension"

BEARER = "live-bearer-9a8b7c6d5e4f"
COOKIE = "live-cookie-0011223344"
API_KEY = "live-api-key-99887766"
QUERY_TOKEN = "live-query-token-5544"
PASSWORD = "hunter2-live-browser-password"
SECRETS = [BEARER, COOKIE, API_KEY, QUERY_TOKEN, PASSWORD]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet
        pass

    def _send(self, body: bytes, ctype: str = "text/html") -> None:
        self.send_response(200 if self.command == "GET" else 201)
        self.send_header("Content-Type", ctype)
        self.send_header("Set-Cookie", f"sid={COOKIE}; Path=/")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        self._send(b"<!doctype html><title>claims</title><p>form</p>")

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self._send(b'{"id": 7}', "application/json")


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture
def extension(tmp_path):
    sync = pytest.importorskip("playwright.sync_api")
    with sync.sync_playwright() as playwright:
        try:
            context = playwright.chromium.launch_persistent_context(
                str(tmp_path / "profile"),
                channel="chromium",
                headless=True,
                args=[
                    f"--disable-extensions-except={EXT}",
                    f"--load-extension={EXT}",
                ],
            )
        except Exception as exc:  # noqa: BLE001 - no browser on this machine
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            workers = context.service_workers
            worker = (
                workers[0]
                if workers
                else context.wait_for_event("serviceworker", timeout=15000)
            )
        except Exception as exc:  # noqa: BLE001
            context.close()
            pytest.skip(f"extension service worker did not start: {exc}")
        worker.evaluate(
            """() => {
              globalThis.__posted = [];
              chrome.runtime.connectNative = () => ({
                postMessage(m) { globalThis.__posted.push(JSON.parse(JSON.stringify(m))); },
                onDisconnect: { addListener() {} },
              });
            }"""
        )
        yield context, worker
        context.close()


def _post(page, base: str) -> None:
    page.evaluate(
        """async ([base, bearer, key, token, password]) => {
          await fetch(base + '/api/claims?access_token=' + token + '&page=2', {
            method: 'POST',
            headers: {
              'Authorization': 'Bearer ' + bearer,
              'X-API-Key': key,
              'Content-Type': 'application/json',
            },
            body: JSON.stringify({claim: {id: 7, note: 'ok'}, password}),
          });
          await fetch(base + '/api/claims?page=3');
        }""",
        [base, BEARER, API_KEY, QUERY_TOKEN, PASSWORD],
    )


def _posted(worker, count: int, page) -> list[dict]:
    for _ in range(50):
        posted = worker.evaluate("() => globalThis.__posted")
        if len(posted) >= count:
            return posted
        page.wait_for_timeout(100)
    return worker.evaluate("() => globalThis.__posted")


def test_real_chrome_save_is_captured_with_secrets_withheld(extension, server):
    context, worker = extension
    page = context.new_page()
    page.goto(server + "/form")  # sets the cookie the POST will carry
    worker.evaluate("() => __openadaptExtensionBackground.setRecording(true)")
    _post(page, server)
    posted = _posted(worker, 1, page)
    saves = [
        m["evidence"] for m in posted if m["evidence"].get("kind") == "network_save"
    ]
    assert len(saves) == 1, posted  # the GET is not a save
    save = saves[0]
    assert save["method"] == "POST" and save["status"] == 201
    assert save["url"].endswith("/api/claims?access_token=<secret>&page=2")
    headers = {h["name"]: h["value"] for h in save["request_headers"]}
    assert headers["authorization"] == "<secret>"
    assert headers["x-api-key"] == "<secret>"
    assert headers["content-type"] == "application/json"
    assert save["request_body"] == {
        "claim": {"id": 7, "note": "ok"},
        "password": "<secret>",
    }
    blob = json.dumps(posted)
    for secret in SECRETS:
        assert secret not in blob, secret


def test_real_chrome_nothing_is_captured_while_idle(extension, server):
    context, worker = extension
    page = context.new_page()
    page.goto(server + "/form")
    _post(page, server)
    page.wait_for_timeout(500)
    assert worker.evaluate("() => globalThis.__posted") == []
    assert worker.evaluate("() => __openadaptExtensionBackground.queued") == []

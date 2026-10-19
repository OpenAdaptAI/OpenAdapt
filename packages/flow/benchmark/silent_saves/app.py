"""MockMed with its fault server, behind a proxy that hides the fault.

MockMed's fault hook is a ``?fault=<kind>`` query on the page URL. Handing
that URL to an executor would give it the answer, so executors get a plain
``http://127.0.0.1:<port>/#tasks`` instead. The proxy in front of the fault
server sets the fault in the page's script for the current item, and blocks
the reset and delete routes. The runner resets and reads the store on the
fault server directly. Every save that reaches the proxy gets the item's
fault, whether the page sends it or an executor posts to the API itself.

What the proxy cannot hide: the fault name is in the served ``app.js``, and
the save request carries it. The protocol forbids executors from reading
page scripts or network traffic for it (see README.md).
"""

from __future__ import annotations

import http.client
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen

#: The line in MockMed's app.js that reads the fault from the page URL.
FAULT_LINE = re.compile(r"var FAULT = [^;\n]*;")
_BLOCKED = ("/api/reset", "/api/encounter/")
_FORWARD_REQUEST = ("Content-Type", "Cookie", "Accept")
_FORWARD_RESPONSE = ("Content-Type", "Set-Cookie", "Location")


class MockMed:
    """One MockMed fault server and its proxy, for a whole run."""

    def __init__(self, port: int = 0) -> None:
        from openadapt_flow.mockmed.fault_server import serve

        upstream, _db, self._stop_upstream = serve(port=0)
        self.store_base = upstream.rstrip("/")
        self.fault = "ok"
        self.drift: tuple[str, ...] = ()
        self.slow_ms = 0
        self.idempotency_key = False
        self._proxy = ThreadingHTTPServer(("127.0.0.1", port), _handler(self))
        self._thread = threading.Thread(
            target=self._proxy.serve_forever, name="silent-saves-proxy", daemon=True
        )
        self._thread.start()
        self.base = f"http://127.0.0.1:{self._proxy.server_address[1]}"

    @property
    def server_duplicate(self) -> bool:
        """``duplicate`` with the save key on: the server processes it twice.

        MockMed's own ``duplicate`` makes the page send the save twice, and
        the fault server drops the second send when the page sends a key.
        With a key, the proxy instead sends the save on twice without it, so
        two rows still land.
        """

        return self.fault == "duplicate" and self.idempotency_key

    @property
    def page_fault(self) -> str:
        return "ok" if self.server_duplicate else self.fault

    @property
    def url(self) -> str:
        """The page an executor opens. It carries no fault."""

        return f"{self.base}/#tasks"

    def prepare(
        self,
        fault: str,
        drift: tuple[str, ...] = (),
        slow_ms: int = 0,
        idempotency_key: bool = False,
    ) -> None:
        """Reset the store and set what the next page load will get."""

        self.fault = fault
        self.drift = tuple(drift)
        self.slow_ms = slow_ms
        self.idempotency_key = idempotency_key
        _json(
            f"{self.store_base}/api/reset",
            method="POST",
            body={"seed_concurrent": fault == "stale"},
        )

    def records(self) -> list[dict[str, Any]]:
        snap = _json(f"{self.store_base}/api/db") or {}
        return [r for r in snap.get("records", []) if isinstance(r, dict)]

    def close(self) -> None:
        self._proxy.shutdown()
        self._proxy.server_close()
        self._thread.join(timeout=5)
        self._stop_upstream()


#: MockMed's app.js reads these from the page URL; the proxy sets them.
_DRIFT_READ = "(new URLSearchParams(location.search).get('drift') || '')"
_SLOW_READ = "new URLSearchParams(location.search).get('slowms')"
_KEY_READ = "new URLSearchParams(location.search).get('idempotency') === 'demo'"


def rewrite_app_js(
    source: str,
    fault: str,
    drift: tuple[str, ...] = (),
    slow_ms: int = 0,
    idempotency_key: bool = False,
) -> str:
    """Set MockMed's fault, UI changes and save key in its script.

    Fails if a hook it rewrites has moved, so a change to MockMed cannot
    silently turn a fault or a UI change off.
    """

    for name in (fault, *drift):
        if not re.fullmatch(r"[a-z_-]+", name):
            raise ValueError(f"bad fault or change name {name!r}")
    out, n = FAULT_LINE.subn(f"var FAULT = '{fault}';", source, count=1)
    if n != 1:
        raise RuntimeError("MockMed app.js no longer has the FAULT line to set")
    for hook, value in (
        (_DRIFT_READ, "'" + ",".join(drift) + "'"),
        (_SLOW_READ, f"'{int(slow_ms)}'" if slow_ms else "null"),
        (_KEY_READ, "true" if idempotency_key else "false"),
    ):
        if out.count(hook) != 1:
            raise RuntimeError(f"MockMed app.js no longer reads {hook!r} once")
        out = out.replace(hook, value)
    return out


def _handler(app: MockMed) -> Callable[..., BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

        def do_GET(self) -> None:  # noqa: N802
            self._forward("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._forward("POST")

        def do_DELETE(self) -> None:  # noqa: N802
            self._send(403, {"Content-Type": "text/plain"}, b"blocked by the benchmark")

        def _forward(self, method: str) -> None:
            path = urlparse(self.path).path
            if method == "POST" and path.startswith(_BLOCKED):
                self._send(
                    403, {"Content-Type": "text/plain"}, b"blocked by the benchmark"
                )
                return
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length else None
            target = self.path
            sends = 1
            if method == "POST" and path == "/api/encounter":
                # The fault applies to every save, however it is sent: through
                # the page, or straight to the API.
                target = f"/api/encounter?fault={app.page_fault}"
                if app.server_duplicate:
                    sends = 2
                    body = _without_key(body)
            headers = {
                k: self.headers[k] for k in _FORWARD_REQUEST if self.headers.get(k)
            }
            if body is not None and "Content-Type" not in headers:
                headers["Content-Type"] = "application/json"
            for _ in range(sends):
                status, out, payload = _upstream(
                    app.store_base, method, target, body, headers
                )
            if method == "GET" and path == "/app.js" and status == 200:
                payload = rewrite_app_js(
                    payload.decode("utf-8"),
                    app.page_fault,
                    app.drift,
                    app.slow_ms,
                    app.idempotency_key,
                ).encode()
            self._send(status, out, payload)

        def _send(self, status: int, headers: dict[str, str], payload: bytes) -> None:
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            # Each item gets its own fault; nothing may be served from cache.
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def _upstream(
    base: str, method: str, target: str, body: Optional[bytes], headers: dict[str, str]
) -> tuple[int, dict[str, str], bytes]:
    up = urlparse(base)
    conn = http.client.HTTPConnection(up.hostname, up.port, timeout=30)
    try:
        conn.request(method, target, body=body, headers=headers)
        resp = conn.getresponse()
        payload = resp.read()
        out = {k: v for k, v in resp.getheaders() if k in _FORWARD_RESPONSE}
        return resp.status, out, payload
    finally:
        conn.close()


def _without_key(body: Optional[bytes]) -> Optional[bytes]:
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return body
    if isinstance(data, dict):
        data.pop("key", None)
    return json.dumps(data).encode()


def _json(url: str, *, method: str = "GET", body: Optional[Any] = None) -> Any:
    data = None if body is None else json.dumps(body).encode()
    req = Request(  # noqa: S310 - loopback only
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urlopen(req, timeout=10) as resp:  # noqa: S310
        payload = resp.read().decode()
    return json.loads(payload) if payload else None

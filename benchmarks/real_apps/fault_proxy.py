"""Local HTTP reverse proxy for deterministic silent-failure fault injection.

Sits between a web client and a real (or local test) application backend so
every web app in the Real-App Verification Matrix can be tested under both:

- Normal passthrough (happy-path ``VERIFIED``), and
- Active fault injection (silent-fail ``HALT``):
  - ``drop_write_return_200``: Drops matching POST/PUT/PATCH writes without
    forwarding to the backend and returns a synthetic HTTP 200 OK banner.
  - ``session_expiry_redirect``: Returns HTTP 302 redirect to ``/login`` on
    matching writes while dropping persistence.
  - ``optimistic_ui_rollback``: Intercepts GraphQL/JSON mutations, returns a
    synthetic 200 JSON payload that tricks optimistic React UIs into showing
    success, while never writing to the backend.
  - ``slow_response``: Adds deterministic latency before forwarding.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Literal

FaultMode = Literal[
    "none",
    "drop_write_return_200",
    "session_expiry_redirect",
    "optimistic_ui_rollback",
    "slow_response",
]


@dataclass
class FaultRule:
    """Active fault injection rule evaluated on incoming proxy requests."""

    mode: FaultMode = "none"
    path_contains: str = ""
    methods: tuple[str, ...] = ("POST", "PUT", "PATCH")
    delay_s: float = 0.0
    synthetic_body: dict[str, Any] | None = None
    redirect_location: str = "/login?expired=1"


class FaultProxy:
    """Context-managed local HTTP reverse proxy with live toggleable fault rules."""

    def __init__(
        self,
        upstream_base_url: str,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        rule: FaultRule | None = None,
    ) -> None:
        self.upstream_base_url = upstream_base_url.rstrip("/")
        self._host = host
        self._port = port
        self._lock = threading.Lock()
        self._rule: FaultRule = rule or FaultRule()
        self.intercepted_count: int = 0
        self.forwarded_count: int = 0
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("FaultProxy is not started")
        host, port = self._server.server_address[:2]
        host_str = host.decode("ascii") if isinstance(host, bytes) else str(host)
        return f"http://{host_str}:{port}"

    def set_rule(self, rule: FaultRule) -> None:
        with self._lock:
            self._rule = rule

    def _current_rule(self) -> FaultRule:
        with self._lock:
            return self._rule

    def _record_intercepted(self) -> None:
        with self._lock:
            self.intercepted_count += 1

    def _record_forwarded(self) -> None:
        with self._lock:
            self.forwarded_count += 1

    def start(self) -> FaultProxy:
        proxy = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

            def _handle_all(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or "0")
                body = self.rfile.read(length) if length > 0 else b""
                rule = proxy._current_rule()
                matches = (
                    rule.mode != "none"
                    and self.command.upper() in rule.methods
                    and (not rule.path_contains or rule.path_contains in self.path)
                )

                if matches and rule.mode == "drop_write_return_200":
                    proxy._record_intercepted()
                    payload = json.dumps(
                        rule.synthetic_body
                        or {"status": "ok", "saved": True, "injected_fault": rule.mode}
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return

                if matches and rule.mode == "session_expiry_redirect":
                    proxy._record_intercepted()
                    payload = b"Session expired"
                    self.send_response(302)
                    self.send_header("Location", rule.redirect_location)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return

                if matches and rule.mode == "optimistic_ui_rollback":
                    proxy._record_intercepted()
                    payload = json.dumps(
                        rule.synthetic_body
                        or {
                            "data": {"ok": True, "optimistic": True},
                            "extensions": {"simulated_rollback": True},
                        }
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return

                if matches and rule.mode == "slow_response" and rule.delay_s > 0:
                    proxy._record_intercepted()
                    time.sleep(rule.delay_s)

                proxy._record_forwarded()
                target_url = f"{proxy.upstream_base_url}{self.path}"
                fwd_headers = {
                    k: v
                    for k, v in self.headers.items()
                    if k.lower() not in ("host", "content-length", "connection")
                }
                req = urllib.request.Request(
                    target_url,
                    data=body if self.command.upper() not in ("GET", "HEAD") else None,
                    headers=fwd_headers,
                    method=self.command.upper(),
                )
                try:
                    with urllib.request.urlopen(req, timeout=15.0) as resp:
                        resp_body = resp.read()
                        self.send_response(resp.status)
                        ct = resp.headers.get("Content-Type", "application/octet-stream")
                        self.send_header("Content-Type", ct)
                        self.send_header("Content-Length", str(len(resp_body)))
                        self.end_headers()
                        self.wfile.write(resp_body)
                except urllib.error.HTTPError as err:
                    err_body = err.read()
                    self.send_response(err.code)
                    self.send_header(
                        "Content-Type",
                        err.headers.get("Content-Type", "text/plain"),
                    )
                    self.send_header("Content-Length", str(len(err_body)))
                    self.end_headers()
                    self.wfile.write(err_body)

            def do_GET(self) -> None:
                self._handle_all()

            def do_POST(self) -> None:
                self._handle_all()

            def do_PUT(self) -> None:
                self._handle_all()

            def do_PATCH(self) -> None:
                self._handle_all()

            def do_DELETE(self) -> None:
                self._handle_all()

        self._server = ThreadingHTTPServer((self._host, self._port), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def __enter__(self) -> FaultProxy:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

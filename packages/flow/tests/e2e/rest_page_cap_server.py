"""Loopback-only collection with explicit synthetic pagination metadata."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit


@dataclass
class CappedRecordServer:
    """Serve actual pages; cap/total/next are data, never verifier verdicts."""

    page_size: int = 2
    metadata: bool = True
    http_status: int = 200
    rows: list[dict[str, Any]] = field(default_factory=list)
    _server: ThreadingHTTPServer | None = field(default=None, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _lock: Any = field(default_factory=threading.Lock, init=False, repr=False)

    def __enter__(self) -> CappedRecordServer:
        if self.page_size < 1:
            raise ValueError("the synthetic page size must be positive")
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                parts = urlsplit(self.path)
                query = parse_qs(parts.query)
                with fixture._lock:
                    rows = [dict(row) for row in fixture.rows]
                try:
                    if parts.path.startswith("/records/"):
                        identity = unquote(parts.path.removeprefix("/records/"))
                        records = [row for row in rows if row["id"] == identity]
                        total, cap, following = len(records), fixture.page_size, None
                    elif parts.path == "/records":
                        requested = int(query.get("limit", [str(fixture.page_size)])[0])
                        page = int(query.get("page", ["0"])[0])
                        if requested < 1 or page < 0:
                            raise ValueError("invalid page")
                        cap = min(requested, fixture.page_size)
                        start = page * cap
                        records = rows[start : start + cap]
                        total = len(rows)
                        following = (
                            f"/records?limit={requested}&page={page + 1}"
                            if start + len(records) < total
                            else None
                        )
                    else:
                        self.send_error(404)
                        return
                except (KeyError, ValueError):
                    self.send_error(400)
                    return
                body: dict[str, Any] = {"records": records}
                if fixture.metadata:
                    body["page"] = {"cap": cap, "total": total, "next": following}
                data = json.dumps(body).encode()
                self.send_response(fixture.http_status)
                if fixture.http_status == 206:
                    # The complete representation has one trailing newline;
                    # this is an explicit partial byte range, still valid JSON.
                    self.send_header(
                        "Content-Range", f"bytes 0-{len(data) - 1}/{len(data) + 1}"
                    )
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args: Any) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("the synthetic server is not running")
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def append(self, record: dict[str, Any]) -> None:
        with self._lock:
            if any(row["id"] == record["id"] for row in self.rows):
                raise ValueError("duplicate synthetic identity")
            self.rows.append(dict(record))

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self.rows]

    def __exit__(self, *_args: Any) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                raise TimeoutError("the synthetic page server did not drain")


def open_read_session(_config: Any) -> Any:
    """A real anonymous client for the declared test-only session hook."""
    import httpx

    return httpx.Client(trust_env=False)

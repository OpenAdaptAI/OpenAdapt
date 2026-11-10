"""Loopback-only collection with explicit synthetic pagination metadata."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from copy import deepcopy
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
    immutable_pages: bool = False
    after_first_page: Callable[[CappedRecordServer], None] | None = field(
        default=None, repr=False
    )
    transform_response: Callable[[dict[str, Any], int], None] | None = field(
        default=None, repr=False
    )
    repeat_boundary: bool = False
    _server: ThreadingHTTPServer | None = field(default=None, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _lock: Any = field(default_factory=threading.Lock, init=False, repr=False)
    _views: dict[str, list[dict[str, Any]]] = field(
        default_factory=dict, init=False, repr=False
    )

    def __enter__(self) -> CappedRecordServer:
        if self.page_size < 1:
            raise ValueError("the synthetic page size must be positive")
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                parts = urlsplit(self.path)
                query = parse_qs(parts.query)
                with fixture._lock:
                    view = query.get("view", [None])[0]
                    if fixture.immutable_pages:
                        if view is None:
                            view = str(len(fixture._views) + 1)
                            fixture._views[view] = deepcopy(fixture.rows)
                        if view not in fixture._views:
                            self.send_error(404)
                            return
                        rows = deepcopy(fixture._views[view])
                    else:
                        rows = deepcopy(fixture.rows)
                try:
                    if parts.path.startswith("/records/"):
                        identity = unquote(parts.path.removeprefix("/records/"))
                        records = [row for row in rows if row["id"] == identity]
                        total, cap, following = len(records), fixture.page_size, None
                    elif parts.path in (
                        "/records",
                        "/fhir/Patient",
                        "/fhir/Patient/_history",
                    ):
                        requested = int(query.get("limit", [str(fixture.page_size)])[0])
                        page = int(query.get("page", ["0"])[0])
                        if requested < 1 or page < 0:
                            raise ValueError("invalid page")
                        cap = min(requested, fixture.page_size)
                        start = page * cap
                        records = rows[start : start + cap]
                        if fixture.repeat_boundary and page == 1:
                            # Deliberate wire fault: raw count can equal total
                            # while the last native row is never represented.
                            records = [rows[0], *records]
                        total = len(rows)
                        following = (
                            f"{parts.path}?limit={requested}&page={page + 1}"
                            if start + len(records) < total
                            else None
                        )
                        if following is not None and view is not None:
                            following += f"&view={view}"
                    else:
                        self.send_error(404)
                        return
                except (KeyError, ValueError):
                    self.send_error(400)
                    return
                body: dict[str, Any] = {"records": records}
                if fixture.metadata:
                    body["page"] = {"cap": cap, "total": total, "next": following}
                    if fixture.immutable_pages:
                        body["page"]["snapshot"] = view
                if parts.path.startswith("/fhir/Patient"):
                    body.update(
                        {
                            "resourceType": "Bundle",
                            "type": "history"
                            if parts.path.endswith("/_history")
                            else "searchset",
                            "total": total,
                            "entry": [
                                {"resource": {"resourceType": "Patient", **row}}
                                for row in records
                            ],
                            "link": (
                                [{"relation": "next", "url": fixture.url + following}]
                                if following
                                else []
                            ),
                        }
                    )
                if fixture.transform_response is not None:
                    fixture.transform_response(body, int(query.get("page", ["0"])[0]))
                data = json.dumps(body).encode()
                if (
                    parts.path
                    in ("/records", "/fhir/Patient", "/fhir/Patient/_history")
                    and int(query.get("page", ["0"])[0]) == 0
                ):
                    hook, fixture.after_first_page = fixture.after_first_page, None
                    if hook is not None:
                        hook(fixture)
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
            return deepcopy(self.rows)

    def replace(self, rows: list[dict[str, Any]]) -> None:
        """A synthetic foreign commit, separate from any already frozen view."""
        with self._lock:
            self.rows = deepcopy(rows)

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

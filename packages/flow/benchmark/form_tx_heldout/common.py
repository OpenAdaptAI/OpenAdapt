"""Shared HTTP plumbing for the held-out carrier quote portals.

Every portal is a ``BasePortal`` subclass served by a stdlib
``ThreadingHTTPServer``. State lives on the portal instance (never in module
globals), so several portals or several copies of one portal can run in one
process.

Test-only endpoints, present on every portal:

* ``GET  /__oracle/submissions``  every stored submission, in order.
* ``POST /__oracle/reset``        drop drafts, sessions, submissions, case.
* ``POST /__oracle/load_case``    ``{"case_id": ...}``: reset, then configure
  the portal for that case (prior policy on file, session expiry, ...).

Each stored submission carries ``case_id``, ``submitted_at`` (UTC ISO 8601),
``submit_seq`` (1-based order of this submission since the last reset) and
``submit_count`` (how many submissions the portal holds at the time of the
read). Repeated submissions are all stored; nothing is deduplicated.
"""

from __future__ import annotations

import html
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
CREDENTIALS = json.loads((HERE / "fixtures" / "portal_logins.json").read_text())


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, str]
    form: dict[str, str]
    form_multi: dict[str, list[str]]
    json: Any
    cookies: dict[str, str]


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "text/html; charset=utf-8"
    headers: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def html(cls, text: str, status: int = 200) -> "Response":
        return cls(status, text.encode("utf-8"))

    @classmethod
    def json_(cls, obj: Any, status: int = 200) -> "Response":
        return cls(status, json.dumps(obj).encode("utf-8"), "application/json")

    @classmethod
    def redirect(cls, location: str) -> "Response":
        return cls(303, b"", headers=[("Location", location)])

    def set_cookie(self, name: str, value: str) -> "Response":
        self.headers.append(("Set-Cookie", f"{name}={value}; Path=/; HttpOnly; SameSite=Lax"))
        return self

    def clear_cookie(self, name: str) -> "Response":
        self.headers.append(("Set-Cookie", f"{name}=; Path=/; Max-Age=0"))
        return self


class BasePortal:
    """One carrier portal. Subclasses implement ``route`` and ``fresh_draft``."""

    portal_id = "base"
    cookie = "sid"

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.creds = CREDENTIALS[self.portal_id]
        self.reset()

    # ---- state -------------------------------------------------------
    def reset(self) -> None:
        with self.lock:
            self.case_id: str | None = None
            self.config: dict[str, Any] = {}
            self.sessions: set[str] = set()
            self.submissions: list[dict[str, Any]] = []
            self.fired: set[str] = set()
            self.draft: dict[str, Any] = self.fresh_draft()
            self.quote_seq = 0

    def load_case(self, case_id: str) -> bool:
        from .case_defs import portal_config

        cfg = portal_config(self.portal_id, case_id)
        if cfg is None:
            return False
        with self.lock:
            self.reset()
            self.case_id = case_id
            self.config = cfg
            self.draft = self.fresh_draft()
        return True

    def fresh_draft(self) -> dict[str, Any]:
        return {}

    def store_submission(self, record: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            rec = dict(record)
            rec["case_id"] = self.case_id
            rec["submitted_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            rec["submit_seq"] = len(self.submissions) + 1
            self.submissions.append(rec)
            return rec

    def fire_once(self, event: str) -> bool:
        """True the first time ``event`` happens after a case load, if configured."""
        with self.lock:
            if event in self.fired:
                return False
            if self.config.get("expire_on") != event:
                return False
            self.fired.add(event)
            return True

    # ---- sessions ----------------------------------------------------
    def new_session(self) -> str:
        sid = secrets.token_hex(12)
        with self.lock:
            self.sessions.add(sid)
        return sid

    def has_session(self, req: Request) -> bool:
        return req.cookies.get(self.cookie, "") in self.sessions

    def drop_sessions(self) -> None:
        with self.lock:
            self.sessions.clear()

    def check_login(self, username: str, password: str) -> bool:
        return username.strip() == self.creds["username"] and password == self.creds["password"]

    # ---- dispatch ----------------------------------------------------
    def dispatch(self, req: Request) -> Response:
        if req.path.startswith("/__oracle/"):
            return self.oracle(req)
        try:
            return self.route(req)
        except Exception as exc:  # keep the server alive; surface the error
            return Response.html(f"<h1>Server error</h1><pre>{esc(repr(exc))}</pre>", 500)

    def oracle(self, req: Request) -> Response:
        if req.path == "/__oracle/submissions" and req.method == "GET":
            with self.lock:
                n = len(self.submissions)
                out = [dict(r, submit_count=n) for r in self.submissions]
            return Response.json_(out)
        if req.path == "/__oracle/reset" and req.method == "POST":
            self.reset()
            return Response.json_({"ok": True})
        if req.path == "/__oracle/load_case" and req.method == "POST":
            case_id = (req.json or {}).get("case_id") or req.form.get("case_id", "")
            if not self.load_case(case_id):
                return Response.json_({"ok": False, "error": f"unknown case {case_id!r}"}, 404)
            return Response.json_({"ok": True, "case_id": case_id})
        return Response.json_({"error": "not found"}, 404)

    def route(self, req: Request) -> Response:  # pragma: no cover - abstract
        raise NotImplementedError


def _handler_for(portal: BasePortal) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _go(self, method: str) -> None:
            parts = urlsplit(self.path)
            query = {k: v[-1] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            ctype = self.headers.get("Content-Type", "")
            form_multi: dict[str, list[str]] = {}
            body_json: Any = None
            if raw and "json" in ctype:
                try:
                    body_json = json.loads(raw.decode("utf-8"))
                except ValueError:
                    body_json = None
            elif raw:
                form_multi = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
            cookies: dict[str, str] = {}
            if self.headers.get("Cookie"):
                jar = SimpleCookie()
                try:
                    jar.load(self.headers["Cookie"])
                except Exception:
                    pass
                cookies = {k: m.value for k, m in jar.items()}
            req = Request(
                method,
                parts.path,
                query,
                {k: v[-1] for k, v in form_multi.items()},
                form_multi,
                body_json,
                cookies,
            )
            resp = portal.dispatch(req)
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.content_type)
            self.send_header("Content-Length", str(len(resp.body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in resp.headers:
                self.send_header(k, v)
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(resp.body)

        def do_GET(self) -> None:  # noqa: N802
            self._go("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._go("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._go("PUT")

        def log_message(self, *args: Any) -> None:
            pass

    return Handler


def serve(portal: BasePortal, host: str = "127.0.0.1", port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer((host, port), _handler_for(portal))
    server.daemon_threads = True
    server.portal = portal  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True, name=f"{portal.portal_id}-http").start()
    return server, f"http://{host}:{server.server_port}"


def slow(seconds: float) -> None:
    time.sleep(seconds)


@dataclass
class PortalSpec:
    name: str
    title: str
    description: str
    factory: Callable[[], BasePortal]
    entry_path: str
    login: dict[str, Any]
    cases: list[str]
    dev_case: str
    intake_vocabulary: dict[str, Any] = field(default_factory=dict)

    def start(self, host: str = "127.0.0.1", port: int = 0) -> tuple[ThreadingHTTPServer, str]:
        return serve(self.factory(), host, port)


def money(n: int) -> str:
    return f"${n:,}"


def parse_int(text: Any) -> int | None:
    s = str(text or "").replace(",", "").replace("$", "").strip()
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def parse_date(text: str) -> str:
    """Accept ISO ``YYYY-MM-DD`` or ``MM/DD/YYYY``; return ISO or ''."""
    s = (text or "").strip()
    try:
        if "/" in s:
            return datetime.strptime(s, "%m/%d/%Y").date().isoformat()
        return datetime.strptime(s, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return ""

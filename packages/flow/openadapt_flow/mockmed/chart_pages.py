"""Server-rendered record pages with their own sign-in, for read-back tests.

The MockMed SPA keeps saved encounters in page memory, so a second browser
cannot see them there. A real clinical application loads a record from its
server when a new session opens it. These pages model that: a sign-in form
that sets a session cookie, and a per-patient record page rendered from the
server-side store on every request.

They are what ``effects.kind: ui_readback`` reads in the fixture servers
(``mockmed/fault_server.py`` and ``benchmark/effect_e2e/record_service.py``)
and in the tests. All data is synthetic. Routes, all under ``/chart/``:

- ``GET  /chart/signin``           sign-in form
- ``POST /chart/signin``           form post; sets ``chart_sid`` and redirects
- ``GET  /chart/home``             signed-in marker (``#chart-signed-in``)
- ``GET  /chart/patient/<id>``     the record page (needs a session)
- ``POST /chart/viewed/<id>``      an "I viewed this" audit beacon (only sent
                                   when :attr:`ChartDisplay.view_beacon` is on)

:class:`ChartDisplay` changes how the record page displays the same data,
which is how the read-back benchmark probes false stops: shortened text, a
slow replica, a changed layout, and a page that sends a POST when it loads.
"""

from __future__ import annotations

import html
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

#: Synthetic credentials for the read-back session in fixtures and tests.
READER_USERNAME = "chart-reader"
READER_PASSWORD = "synthetic-reader-password"

#: Returns the rows saved for a patient, or None when the patient is unknown.
#: Each row has at least ``id``, ``type`` and ``note``.
RowSource = Callable[[str], Optional[list[dict[str, Any]]]]


@dataclass
class ChartDisplay:
    """How the record page displays the saved data (not what it stores)."""

    #: Shorten the visible note to this many characters plus an ellipsis.
    #: The full note stays in the cell's ``title`` attribute.
    truncate_at: Optional[int] = None
    #: Hide a row until this many seconds after the page first served it,
    #: like a read replica that lags the primary.
    lag_s: float = 0.0
    #: 1 = the markup the read-back fixtures declare. 2 = a redesign that
    #: renames the row and cell classes.
    markup_version: int = 1
    #: The page posts a "record viewed" audit beacon when it loads.
    view_beacon: bool = False
    #: How the beacon is sent: ``fetch``, ``beacon`` (``navigator.sendBeacon``),
    #: ``xhr``, or ``form`` (a hidden form submitted by script).
    beacon_transport: str = "fetch"


@dataclass
class ChartPages:
    """Route handler for the ``/chart/`` pages."""

    rows_for: RowSource
    username: str = READER_USERNAME
    password: str = READER_PASSWORD
    display: ChartDisplay = field(default_factory=ChartDisplay)
    _sessions: set[str] = field(default_factory=set)
    _first_seen: dict[Any, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    #: Count of audit beacons received (a write the read-back must not send).
    beacons_received: int = 0
    sign_ins: int = 0

    def reset_display(self, **changes: Any) -> None:
        with self._lock:
            self.display = ChartDisplay(**changes)
            self._first_seen.clear()

    # -- routing -------------------------------------------------------------
    def handle_get(self, handler: BaseHTTPRequestHandler) -> bool:
        path = urlparse(handler.path).path
        if not path.startswith("/chart/"):
            return False
        if path == "/chart/signin":
            _send_html(handler, 200, _SIGNIN_PAGE)
            return True
        if not self._signed_in(handler):
            _redirect(handler, "/chart/signin")
            return True
        if path == "/chart/home":
            _send_html(
                handler,
                200,
                _page(
                    "Chart home",
                    '<div id="chart-signed-in">Signed in as '
                    f"{html.escape(self.username)}</div>",
                ),
            )
            return True
        prefix = "/chart/patient/"
        if path.startswith(prefix):
            self._record_page(handler, path[len(prefix) :])
            return True
        _send_html(handler, 404, _page("Not found", "<p>Not found.</p>"))
        return True

    def handle_post(self, handler: BaseHTTPRequestHandler) -> bool:
        path = urlparse(handler.path).path
        if not path.startswith("/chart/"):
            return False
        if path == "/chart/signin":
            length = int(handler.headers.get("Content-Length", 0) or 0)
            body = handler.rfile.read(length).decode("utf-8") if length else ""
            form = parse_qs(body)
            user = (form.get("username") or [""])[0]
            password = (form.get("password") or [""])[0]
            if secrets.compare_digest(user, self.username) and secrets.compare_digest(
                password, self.password
            ):
                token = secrets.token_urlsafe(16)
                with self._lock:
                    self._sessions.add(token)
                    self.sign_ins += 1
                handler.send_response(303)
                handler.send_header("Location", "/chart/home")
                handler.send_header(
                    "Set-Cookie", f"chart_sid={token}; HttpOnly; Path=/chart"
                )
                handler.send_header("Content-Length", "0")
                handler.end_headers()
            else:
                _send_html(
                    handler,
                    401,
                    _page(
                        "Sign in",
                        '<p id="chart-signin-error">Wrong user name '
                        "or password.</p>" + _SIGNIN_FORM,
                    ),
                )
            return True
        if path.startswith("/chart/viewed/"):
            with self._lock:
                self.beacons_received += 1
            _send_html(handler, 204, "")
            return True
        _send_html(handler, 404, _page("Not found", "<p>Not found.</p>"))
        return True

    # -- pages ---------------------------------------------------------------
    def _signed_in(self, handler: BaseHTTPRequestHandler) -> bool:
        cookie = handler.headers.get("Cookie", "") or ""
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "chart_sid":
                with self._lock:
                    return value in self._sessions
        return False

    def _record_page(self, handler: BaseHTTPRequestHandler, patient_id: str) -> None:
        display = self.display
        rows = self.rows_for(patient_id)
        if rows is None:
            _send_html(
                handler,
                404,
                _page(
                    "No such patient",
                    '<main id="chart-missing"><p id="chart-not-found">'
                    "No such patient.</p></main>",
                ),
            )
            return
        now = time.monotonic()
        visible: list[dict[str, Any]] = []
        with self._lock:
            for row in rows:
                seen = self._first_seen.setdefault(row.get("id"), now)
                if now - seen >= display.lag_s:
                    visible.append(row)
        row_class, type_class, note_class = (
            ("chart-row", "enc-type", "enc-note")
            if display.markup_version == 1
            else ("encounter", "col-kind", "col-text")
        )
        body_rows = []
        for row in visible:
            note = str(row.get("note", ""))
            shown = note
            if display.truncate_at is not None and len(note) > display.truncate_at:
                shown = note[: display.truncate_at] + "…"
            body_rows.append(
                f'<tr class="{row_class}" data-record-id="{html.escape(str(row["id"]))}">'
                f'<td class="{type_class}">{html.escape(str(row.get("type", "")))}</td>'
                f'<td class="{note_class}" title="{html.escape(note, quote=True)}">'
                f"{html.escape(shown)}</td></tr>"
            )
        table = (
            '<table id="chart-encounters"><thead><tr><th>Type</th><th>Note</th>'
            "</tr></thead><tbody>" + "".join(body_rows) + "</tbody></table>"
            if body_rows
            else '<p id="chart-empty">No encounters.</p>'
        )
        beacon = _beacon_script(display.beacon_transport, patient_id)
        if not display.view_beacon:
            beacon = ""
        content = (
            f'<main id="chart" data-patient-id="{html.escape(patient_id)}">'
            f'<h1>Chart <span id="chart-patient">{html.escape(patient_id)}</span></h1>'
            f"{table}</main>{beacon}"
        )
        _send_html(handler, 200, _page("Chart", content))


def _beacon_script(transport: str, patient_id: str) -> str:
    target = "/chart/viewed/" + html.escape(patient_id, quote=True)
    if transport == "beacon":
        return f"<script>navigator.sendBeacon('{target}', 'viewed');</script>"
    if transport == "xhr":
        return (
            "<script>try { var x = new XMLHttpRequest(); "
            f"x.open('POST', '{target}'); x.send('viewed'); }} catch (e) {{}}</script>"
        )
    if transport == "form":
        return (
            f'<form id="chart-viewed" method="post" action="{target}"></form>'
            "<script>document.getElementById('chart-viewed').submit();</script>"
        )
    return (
        f"<script>fetch('{target}', {{method: 'POST'}}).catch(function () {{}});"
        "</script>"
    )


_SIGNIN_FORM = (
    '<form id="chart-signin-form" method="post" action="/chart/signin">'
    '<label for="chart-username">User name</label>'
    '<input id="chart-username" name="username" autocomplete="off">'
    '<label for="chart-password">Password</label>'
    '<input id="chart-password" name="password" type="password" autocomplete="off">'
    '<button id="chart-signin" type="submit">Sign in</button></form>'
)


def _page(title: str, content: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title></head><body>{content}</body></html>"
    )


_SIGNIN_PAGE = _page("Sign in", "<h1>Sign in to the chart</h1>" + _SIGNIN_FORM)


def _send_html(handler: BaseHTTPRequestHandler, status: int, text: str) -> None:
    body = text.encode("utf-8")
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        if body:
            handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        pass


def _redirect(handler: BaseHTTPRequestHandler, location: str) -> None:
    handler.send_response(303)
    handler.send_header("Location", location)
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def chart_readback_spec(
    base_url: str,
    *,
    patient_param: str = "patient_id",
    username_env: str = "CHART_READER_USERNAME",
    password_env: str = "CHART_READER_PASSWORD",
    note_attribute: Optional[str] = None,
) -> dict[str, Any]:
    """The ``effects.ui_readback`` section that reads these fixture pages.

    ``note_attribute="title"`` reads the full note from the cell's attribute
    instead of its visible text. Returned as plain data so a test can load it
    exactly as a deployment YAML would.
    """

    base = base_url.rstrip("/")
    note: dict[str, Any] = {"selector": "td.enc-note"}
    if note_attribute:
        note["attribute"] = note_attribute
    return {
        "record_url": base + "/chart/patient/{patient}",
        "url_params": {"patient": {"param": patient_param}},
        "sign_in": {
            "url": base + "/chart/signin",
            "username_selector": "#chart-username",
            "password_selector": "#chart-password",
            "submit_selector": "#chart-signin",
            "signed_in_selector": "#chart-signed-in",
            "username_env": username_env,
            "password_env": password_env,
        },
        "ready_selector": "#chart",
        "not_found_selector": "#chart-not-found",
        "empty_selector": "#chart-empty",
        "row_selector": "tr.chart-row",
        "fields": {
            "id": {"attribute": "data-record-id"},
            "type": {"selector": "td.enc-type"},
            "note": note,
        },
        "identity": {
            "reader": {"selector": "#chart-patient"},
            "value": {"param": patient_param},
            "field": "patient_id",
        },
    }


__all__ = [
    "READER_PASSWORD",
    "READER_USERNAME",
    "ChartDisplay",
    "ChartPages",
    "RowSource",
    "chart_readback_spec",
]

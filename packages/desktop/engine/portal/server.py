"""The runner-local HTTP portal: pairing, the task shell, and the Flow relay.

This server is transport only.  It serves the responsive task shell, claims
one-use pairing secrets, and relays allowlisted requests to the attended
console in :mod:`engine.portal.flow_client`.  It contains no decision logic:
the question, the allowed actions, the evidence, and the outcome are produced
by ``openadapt-flow`` and passed through unmodified.

Two boundaries matter and are enforced here rather than by convention:

* **Every protected response is ``no-store``.**  Task projections, decision
  outcomes, and evidence images all carry ``Cache-Control: no-store`` plus a
  ``no-cache`` ``Pragma``, and the shell's service worker is structurally
  unable to cache them (see ``shell/sw.js``).
* **The phone never receives Flow's console capability.**  It authenticates
  with a portal session token minted by :mod:`engine.portal.pairing`; the
  loopback console bearer token stays in this process.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from engine.portal.flow_client import (
    FlowConsoleClient,
    FlowConsoleUnavailable,
)
from engine.portal.ingress import PortalIngress
from engine.portal.pairing import DevicePairingStore, DeviceSession, PairingRefused

SHELL_DIR = Path(__file__).parent / "shell"

#: Prefix under which every protected route lives.  The service worker refuses
#: to cache anything beginning with this, and the test suite asserts it.
PROTECTED_PREFIX = "/api/portal/"

#: Shell assets the phone may fetch without a session, and the only paths the
#: service worker is permitted to precache.
SHELL_ASSETS: dict[str, tuple[str, str]] = {
    "/app.js": ("shell/app.js", "application/javascript; charset=utf-8"),
    "/styles.css": ("shell/styles.css", "text/css; charset=utf-8"),
    "/manifest.webmanifest": (
        "shell/manifest.webmanifest",
        "application/manifest+json; charset=utf-8",
    ),
    "/sw.js": ("shell/sw.js", "application/javascript; charset=utf-8"),
}

_MAX_BODY_BYTES = 16 * 1024
# Flow's attention identifiers are opaque hex (`_ATTENTION_ID_RE` in
# openadapt_flow/console/attention.py). A path separator is never part of one,
# so excluding it removes an entire class of route-confusion and traversal.
_RUN_ID = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
_ACTION_ID = r"[a-z][a-z_]{0,39}"
_ARTIFACT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_ROUTE_TASKS = re.compile(r"^/api/portal/tasks$")
_ROUTE_TASK_ACTION = re.compile(
    rf"^/api/portal/tasks/(?P<run_id>{_RUN_ID})/actions/(?P<action_id>{_ACTION_ID})$"
)
_ROUTE_TASK_EVIDENCE = re.compile(
    rf"^/api/portal/tasks/(?P<run_id>{_RUN_ID})/evidence$"
)
_ROUTE_TASK_DETAIL = re.compile(rf"^/api/portal/tasks/(?P<run_id>{_RUN_ID})$")

_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' blob: data:; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'; "
        "worker-src 'self'; manifest-src 'self'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}

#: Applied to every protected response.  Never weakened, never conditional.
_NO_STORE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, private, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


class PortalApp:
    """Request routing and policy for one portal instance.

    Separated from the socket server so the whole surface -- including the
    loopback default, the single-use claim, and the ``no-store`` guarantee --
    is testable without binding a port.
    """

    def __init__(
        self,
        ingress: PortalIngress,
        pairings: DevicePairingStore,
        flow: FlowConsoleClient | None,
    ) -> None:
        self.ingress = ingress
        self.pairings = pairings
        self.flow = flow

    # -------------------------------------------------------------- responses

    @staticmethod
    def _json(
        status: int, payload: Any, *, protected: bool = True
    ) -> tuple[int, dict[str, str], bytes]:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        headers.update(_SECURITY_HEADERS)
        if protected:
            headers.update(_NO_STORE_HEADERS)
        return status, headers, json.dumps(payload).encode("utf-8")

    @staticmethod
    def _refusal(status: int, reason: str, message: str):
        return PortalApp._json(status, {"reason": reason, "message": message})

    def _asset(self, path: str) -> tuple[int, dict[str, str], bytes]:
        relative, media_type = SHELL_ASSETS[path]
        body = (SHELL_DIR.parent / relative).read_bytes()
        headers = {"Content-Type": media_type}
        headers.update(_SECURITY_HEADERS)
        # The shell is the application, not the evidence. It may be revalidated
        # and precached; nothing under the protected prefix ever may.
        headers["Cache-Control"] = "no-cache"
        if path == "/sw.js":
            headers["Service-Worker-Allowed"] = "/"
        return 200, headers, body

    def _shell(self) -> tuple[int, dict[str, str], bytes]:
        headers = {"Content-Type": "text/html; charset=utf-8"}
        headers.update(_SECURITY_HEADERS)
        headers["Cache-Control"] = "no-cache"
        return 200, headers, (SHELL_DIR / "index.html").read_bytes()

    # ----------------------------------------------------------------- policy

    def _authenticate(self, headers: Any) -> DeviceSession:
        raw = str(headers.get("authorization", "") or "")
        scheme, separator, token = raw.partition(" ")
        if separator != " " or scheme.lower() != "bearer":
            raise PairingRefused("unauthorized", "This device is not paired.")
        return self.pairings.authenticate(token.strip())

    def _check_origin(self, headers: Any) -> None:
        """Refuse a cross-origin mutation.

        A phone's own requests carry the portal's public origin.  Anything else
        -- another site's script, a different hostname bound to this runner --
        is refused before the pairing session is even consulted.
        """
        origin = headers.get("origin")
        if origin is None:
            raise PairingRefused(
                "cross_origin", "A decision requires a same-origin request."
            )
        if str(origin).rstrip("/") != self.ingress.public_origin:
            raise PairingRefused(
                "cross_origin", "A decision requires a same-origin request."
            )

    def _relay(self, route: str, **kwargs: Any):
        if self.flow is None:
            return self._refusal(
                503,
                "portal_upstream_unavailable",
                "The local OpenAdapt decision service is not running. Start the "
                "portal from OpenAdapt Desktop on this computer.",
            )
        try:
            response = self.flow.request(route, **kwargs)
        except FlowConsoleUnavailable as exc:
            return self._refusal(503, "portal_upstream_unavailable", str(exc))
        if response.content is not None:
            headers = {"Content-Type": response.media_type}
            headers.update(_SECURITY_HEADERS)
            headers.update(_NO_STORE_HEADERS)
            return response.status, headers, response.content
        # Verbatim pass-through: Desktop does not reinterpret a task, an
        # allowed action, or a decision outcome.
        return self._json(response.status, response.json)

    # ---------------------------------------------------------------- routing

    def handle(
        self, method: str, raw_path: str, headers: Any, body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        """Route one request.  Returns ``(status, headers, body)``."""
        split = urlsplit(raw_path)
        path = split.path
        query = parse_qs(split.query)

        if method == "GET" and path in ("/", "/pair"):
            return self._shell()
        if method == "GET" and path in SHELL_ASSETS:
            return self._asset(path)

        if not path.startswith(PROTECTED_PREFIX):
            return self._refusal(404, "not_found", "No such page.")

        if method == "POST" and path == "/api/portal/pair/claim":
            return self._claim(headers, body)

        try:
            session = self._authenticate(headers)
        except PairingRefused as exc:
            status = 202 if exc.reason == "pending_approval" else 401
            return self._refusal(status, exc.reason, str(exc))

        if method == "GET" and path == "/api/portal/session":
            return self._json(
                200,
                {
                    "state": "paired",
                    "device_label": session.device_label,
                    "runner_id": session.runner_id,
                    "public_origin": self.ingress.public_origin,
                },
            )

        if method == "GET" and _ROUTE_TASKS.fullmatch(path):
            return self._relay("tasks")

        evidence = _ROUTE_TASK_EVIDENCE.fullmatch(path)
        if method == "GET" and evidence:
            run_id = self._safe_run_id(evidence.group("run_id"))
            if run_id is None:
                return self._refusal(404, "not_found", "No such decision.")
            artifact_id = (query.get("id") or [""])[0]
            if not _ARTIFACT_ID.fullmatch(artifact_id) or ".." in artifact_id:
                return self._refusal(400, "bad_request", "Invalid evidence reference.")
            return self._relay(
                "task_evidence", run_id=run_id, params={"id": artifact_id}
            )

        action = _ROUTE_TASK_ACTION.fullmatch(path)
        if method == "POST" and action:
            return self._decide(
                headers,
                body,
                session,
                self._safe_run_id(action.group("run_id")),
                action.group("action_id"),
            )

        detail = _ROUTE_TASK_DETAIL.fullmatch(path)
        if method == "GET" and detail:
            run_id = self._safe_run_id(detail.group("run_id"))
            if run_id is None:
                return self._refusal(404, "not_found", "No such decision.")
            return self._relay("task_detail", run_id=run_id)

        return self._refusal(404, "not_found", "No such page.")

    @staticmethod
    def _safe_run_id(value: str | None) -> str | None:
        if not value or ".." in value or "/" in value:
            return None
        return value

    def _claim(self, headers: Any, body: bytes):
        """Claim one pairing secret.  Unauthenticated by design: the one-use,
        five-minute, runner-bound secret *is* the credential, and it buys only
        an unapproved session."""
        if len(body) > _MAX_BODY_BYTES:
            return self._refusal(413, "bad_request", "Request body is too large.")
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            return self._refusal(400, "bad_request", "Invalid request.")
        if not isinstance(payload, dict):
            return self._refusal(400, "bad_request", "Invalid request.")
        try:
            self._check_origin(headers)
            result = self.pairings.claim(
                payload.get("secret"), payload.get("device_label")
            )
        except PairingRefused as exc:
            status = 403 if exc.reason == "cross_origin" else 410
            return self._refusal(status, exc.reason, str(exc))
        return self._json(200, result)

    def _decide(
        self,
        headers: Any,
        body: bytes,
        session: DeviceSession,
        run_id: str | None,
        action_id: str,
    ):
        """Relay one decision to Flow.  Desktop adds no authority of its own."""
        if run_id is None:
            return self._refusal(404, "not_found", "No such decision.")
        if len(body) > _MAX_BODY_BYTES:
            return self._refusal(413, "bad_request", "Request body is too large.")
        try:
            self._check_origin(headers)
        except PairingRefused as exc:
            return self._refusal(403, exc.reason, str(exc))
        content_type = str(headers.get("content-type", "") or "").lower()
        if not content_type.startswith("application/json"):
            return self._refusal(415, "bad_request", "Decisions must be JSON.")
        if not self.pairings.verify_csrf(
            session, headers.get("x-openadapt-portal-csrf", "")
        ):
            return self._refusal(403, "csrf", "This session needs to pair again.")
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            return self._refusal(400, "bad_request", "Invalid request.")
        if not isinstance(payload, dict):
            return self._refusal(400, "bad_request", "Invalid request.")
        # The phone's payload is forwarded as-is. Flow validates it against the
        # signed task digest, the engine capability, and the allowed-action
        # list; Desktop must not pre-approve or rewrite any of that.
        return self._relay(
            "task_action", run_id=run_id, action_id=action_id, json_body=payload
        )


class _Handler(BaseHTTPRequestHandler):
    server_version = "OpenAdaptPortal"
    sys_version = ""

    # socketserver only calls settimeout() when this is not None, so without it
    # every read blocks forever and a handful of half-open sockets from a
    # hostile network can pin every worker thread indefinitely.
    timeout = 15

    # HTTP/1.0 semantics close each connection, so an over-long body that we
    # refuse cannot desync into the next request on the same socket.
    protocol_version = "HTTP/1.0"

    @property
    def _app(self) -> PortalApp:
        return self.server.app  # type: ignore[attr-defined]

    def _respond(self, method: str) -> None:
        try:
            length = int(self.headers.get("content-length") or 0)
        except (TypeError, ValueError):
            # A malformed Content-Length must be a refusal, not a traceback on
            # stderr from socketserver's error handler.
            length = -1
        if length < 0:
            status, headers, payload = PortalApp._refusal(
                400, "bad_request", "Invalid request."
            )
            self._write(method, status, headers, payload)
            return
        if length > _MAX_BODY_BYTES:
            # Refuse before reading. The connection closes either way, so the
            # unread bytes cannot become the next request.
            status, headers, payload = PortalApp._refusal(
                413, "bad_request", "Request body is too large."
            )
            self._write(method, status, headers, payload)
            return
        try:
            body = self.rfile.read(length) if length else b""
        except OSError:
            return
        try:
            status, headers, payload = self._app.handle(
                method, self.path, self.headers, body
            )
        except Exception:  # pragma: no cover - defensive
            status, headers, payload = PortalApp._refusal(
                500, "error", "The portal could not complete that request."
            )
        self._write(method, status, headers, payload)

    def _write(
        self, method: str, status: int, headers: dict[str, str], payload: bytes
    ) -> None:
        try:
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(payload)
        except OSError:  # pragma: no cover - client vanished mid-response
            pass

    def do_GET(self) -> None:  # noqa: N802
        self._respond("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._respond("POST")

    def log_message(self, fmt: str, *args: Any) -> None:
        """Never write a request line to a log.

        A pairing link's secret lives in its fragment and never reaches the
        server, but run identifiers and evidence artifact identifiers do appear
        in paths.  Those are protected local references, so the portal keeps no
        access log at all.
        """


class PortalServer:
    """Owns the portal's listening socket for the lifetime of the portal."""

    def __init__(self, app: PortalApp) -> None:
        self.app = app
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int | None = None

    def start(self) -> int:
        """Bind the validated ingress address and serve.  Returns the port."""
        ingress = self.app.ingress
        httpd = ThreadingHTTPServer((ingress.bind_host, ingress.port), _Handler)
        httpd.daemon_threads = True
        httpd.app = self.app  # type: ignore[attr-defined]
        self._httpd = httpd
        self.port = int(httpd.server_address[1])
        self._thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        self._thread = None
        self.port = None

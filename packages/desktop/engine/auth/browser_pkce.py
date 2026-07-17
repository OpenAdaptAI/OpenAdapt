"""BrowserPkceProvider -- v1 "click Login" for interactive desktop users.

Flow (spec section 3a):
    1. Generate a PKCE verifier/challenge and bind an ephemeral loopback
       listener on ``127.0.0.1:<port>`` at path ``/callback``.
    2. Open the hosted login page in the SYSTEM browser (so Google / magic-link
       "just work" with zero forked auth), passing the loopback redirect URI +
       PKCE challenge.
    3. The browser redirects back to the loopback with ``?code=…``; the listener
       captures it and serves a minimal "you can close this tab" page.
    4. Exchange the code for a Supabase session via PKCE (using the code
       verifier), then MINT an ingest token via the hosted API.
    5. Store a single active credential whose bearer ``token`` is the minted
       ingest token (so the headless push / count path always has a bearer),
       carrying the Supabase ``refresh_token`` so the session can be renewed.

``is_available()`` is False on a headless server (no browser / no loopback), so
the UI falls back to :class:`~engine.auth.paste.PasteTokenProvider`.

Reconciliation note: the shared store (:mod:`engine.auth.store`) keys ONE active
credential per host and ``auth_header()`` returns exactly one bearer. We
therefore fold the Supabase session and the minted ingest token into a single
stored ``Credential`` (``kind="ingest_token"``, ``token`` = ingest token,
``refresh_token`` = Supabase refresh) rather than two competing entries.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
from loguru import logger

from engine.auth.provider import Credential
from engine.auth.store import DEFAULT_HOST, store_credential

# Hosted login page opened in the system browser.
LOGIN_PATH = "/login"
# Path the loopback listener serves for the OAuth redirect.
CALLBACK_PATH = "/callback"
# Hosted endpoint that mints an ingest token from an authenticated session.
MINT_PATH = "/api/ingest-tokens"

# Supabase project config (coordinator-provisioned). Absent in CI/tests.
SUPABASE_URL_ENV = "OPENADAPT_SUPABASE_URL"
SUPABASE_ANON_KEY_ENV = "OPENADAPT_SUPABASE_ANON_KEY"

_CALLBACK_HTML = (
    b"<!doctype html><html><head><meta charset='utf-8'>"
    b"<title>OpenAdapt login complete</title></head>"
    b"<body style='font-family:system-ui;text-align:center;padding-top:3rem'>"
    b"<h2>You're signed in.</h2>"
    b"<p>You can close this tab and return to OpenAdapt.</p>"
    b"<script>window.setTimeout(function(){window.close();},800);</script>"
    b"</body></html>"
)


def generate_pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for the S256 PKCE method."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


class _LoopbackReceiver:
    """Ephemeral 127.0.0.1 listener that captures the OAuth ``code``."""

    def __init__(self) -> None:
        self.code: str | None = None
        self.state: str | None = None
        self.error: str | None = None
        self._event = threading.Event()
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # noqa: D401 - silence stdlib logging
                pass

            def do_GET(self) -> None:  # noqa: N802 - stdlib naming
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path.rstrip("/") not in ("", CALLBACK_PATH.rstrip("/")):
                    self.send_response(404)
                    self.end_headers()
                    return
                params = urllib.parse.parse_qs(parsed.query)
                parent.code = (params.get("code") or [None])[0]
                parent.state = (params.get("state") or [None])[0]
                parent.error = (params.get("error") or [None])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(_CALLBACK_HTML)
                parent._event.set()

        self._server = HTTPServer(("127.0.0.1", 0), Handler)

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.port}{CALLBACK_PATH}"

    def serve_until_code(self, timeout: float) -> None:
        thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        thread.start()
        try:
            self._event.wait(timeout=timeout)
        finally:
            self._server.shutdown()
            self._server.server_close()

    def close(self) -> None:
        try:
            self._server.server_close()
        except Exception:
            pass


class BrowserPkceProvider:
    """System-browser + loopback PKCE provider.

    Args:
        host: Hosted base URL.
        open_browser: Callable that opens a URL in the system browser.
        supabase_url: Supabase project URL (defaults to the ``OPENADAPT_SUPABASE_URL`` env).
        supabase_anon_key: Supabase anon key (defaults to the env var).
        timeout: Seconds to wait for the browser redirect.
    """

    name = "browser_pkce"

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        open_browser=None,
        supabase_url: str | None = None,
        supabase_anon_key: str | None = None,
        timeout: float = 180.0,
    ) -> None:
        self.host = host.rstrip("/")
        self._open_browser = open_browser or self._default_open_browser
        self._supabase_url = (supabase_url or os.environ.get(SUPABASE_URL_ENV, "")).rstrip("/")
        self._supabase_anon_key = supabase_anon_key or os.environ.get(SUPABASE_ANON_KEY_ENV, "")
        self._timeout = timeout

    @staticmethod
    def _default_open_browser(url: str) -> None:
        import webbrowser

        webbrowser.open(url)

    def is_available(self) -> bool:
        """False on a headless box (no browser / no display)."""
        if os.environ.get("OPENADAPT_HEADLESS", "").strip():
            return False
        if sys.platform.startswith("linux"):
            # No X11 / Wayland display -> no system browser to drive.
            return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        # macOS / Windows always have a default browser + loopback.
        return True

    def login(self) -> Credential:
        """Run the browser-PKCE flow and store the resulting credential.

        Returns:
            The stored ``Credential`` (bearer = minted ingest token).

        Raises:
            RuntimeError: If the flow cannot complete (headless, denied, timeout,
                or a code/token/mint failure).
        """
        if not self.is_available():
            raise RuntimeError(
                "Browser login is unavailable on this machine; use token paste."
            )

        verifier, challenge = generate_pkce_pair()
        state = secrets.token_urlsafe(24)
        receiver = _LoopbackReceiver()

        auth_url = self._build_login_url(receiver.redirect_uri, challenge, state)
        logger.info("Opening system browser for hosted login")
        self._open_browser(auth_url)

        receiver.serve_until_code(self._timeout)

        if receiver.error:
            raise RuntimeError(f"Login was denied: {receiver.error}")
        if not receiver.code:
            raise RuntimeError("Timed out waiting for the browser login to complete.")
        if receiver.state and receiver.state != state:
            raise RuntimeError("Login state mismatch (possible CSRF); aborting.")

        session = self._exchange_code(receiver.code, verifier, receiver.redirect_uri)
        access_token = session["access_token"]
        refresh_token = session.get("refresh_token")
        expires_at = session.get("expires_at")

        ingest_token, org_id = self._mint_ingest_token(access_token)

        cred: Credential = {
            "kind": "ingest_token",
            "token": ingest_token,
            "refresh_token": refresh_token,
            "org_id": org_id,
            "host": self.host,
            "expires_at": expires_at,
        }
        store_credential(cred)
        logger.info("Browser login complete; ingest token stored for {host}", host=self.host)
        return cred

    def _build_login_url(self, redirect_uri: str, challenge: str, state: str) -> str:
        """Build the hosted login URL carrying the loopback redirect + PKCE."""
        query = urllib.parse.urlencode(
            {
                "redirect_to": redirect_uri,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
            }
        )
        return f"{self.host}{LOGIN_PATH}?{query}"

    def _exchange_code(self, code: str, verifier: str, redirect_uri: str) -> dict:
        """Exchange the auth code for a Supabase session via PKCE.

        Raises:
            RuntimeError: If Supabase is not configured or the exchange fails.
        """
        if not (self._supabase_url and self._supabase_anon_key):
            raise RuntimeError(
                "Supabase is not configured "
                f"(set {SUPABASE_URL_ENV} / {SUPABASE_ANON_KEY_ENV}); "
                "browser login is unavailable."
            )
        url = f"{self._supabase_url}/auth/v1/token"
        try:
            resp = httpx.post(
                url,
                params={"grant_type": "pkce"},
                headers={"apikey": self._supabase_anon_key},
                json={"auth_code": code, "code_verifier": verifier, "redirect_to": redirect_uri},
                timeout=30.0,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Token exchange failed: {exc}") from exc
        if resp.status_code >= 400:
            raise RuntimeError(f"Token exchange rejected ({resp.status_code}).")
        return resp.json()

    def _mint_ingest_token(self, access_token: str) -> tuple[str, str | None]:
        """Mint an ingest token from an authenticated Supabase session.

        Returns:
            ``(ingest_token, org_id)``.

        Raises:
            RuntimeError: If minting fails.
        """
        url = f"{self.host}{MINT_PATH}"
        try:
            resp = httpx.post(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
                json={"label": "desktop"},
                timeout=30.0,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Could not mint an ingest token: {exc}") from exc
        if resp.status_code >= 400:
            raise RuntimeError(f"Ingest-token mint rejected ({resp.status_code}).")
        body = resp.json()
        token = body.get("token") or body.get("ingest_token")
        if not token:
            raise RuntimeError("Mint response did not include an ingest token.")
        return token, body.get("org_id")

"""BrowserPkceProvider -- "click Login" for interactive desktop users.

An RFC 8252 native-app login. The desktop cannot keep a client secret, so it
uses the SYSTEM browser (where Google and magic-link sign-in already work) plus
an ephemeral loopback listener, and binds the exchange with S256 PKCE.

Flow:
    1. Generate a PKCE verifier/challenge and bind a listener on
       ``127.0.0.1:<ephemeral port>`` that serves ONLY ``/callback``.
    2. Open ``{host}/login`` in the system browser, passing the loopback
       redirect URI, the challenge, and an opaque ``state``.
    3. The user signs in normally. OpenAdapt's own authenticated
       ``/auth/loopback`` page then redirects to the listener with
       ``?code=oap_...&state=...``.
    4. The listener validates ``state`` and redeems the code through the
       EXISTING pairing path (:func:`engine.auth.pairing.claim_pairing`),
       sending the verifier. Cloud consumes the one-use code, checks the PKCE
       binding, and mints a user/org-bound ingest credential.
    5. That path stages, independently validates, atomically promotes, and
       confirms the credential in the OS keychain, rolling back on any failure.

Why the redirect comes from our own page rather than from Supabase: Supabase's
``uri_allow_list`` is rewritten by ``scripts/setup-supabase.mjs`` on every
deploy, and RFC 8252 section 7.3 requires the authorization server to accept an
arbitrary loopback port -- a promise that allow-list glob grammar cannot make.
Routing the redirect through an authenticated OpenAdapt page keeps Supabase
configuration untouched and reuses the hardened pairing redemption whole. It
also means browser login introduces NO new credential format: the ``code`` is
an ``oap_`` pairing secret, and the result is the same ``oai_ingest_...``
credential every other path produces.

``is_available()`` is False on a headless box (no browser, no user at the
keyboard) and False when no OS keychain can hold the result, so the UI falls
back to :class:`~engine.auth.paste.PasteTokenProvider`.
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

from loguru import logger

from engine.auth.pairing import (
    PAIRING_SECRET_RE,
    PairingError,
    _validate_destination,
    claim_pairing,
)
from engine.auth.provider import Credential
from engine.auth.store import DEFAULT_HOST, load_credential, secure_store_available

# Hosted login page opened in the system browser.
LOGIN_PATH = "/login"
# The ONLY path the loopback listener serves.
CALLBACK_PATH = "/callback"

# How long to wait for the browser round trip. The user can complete a magic
# link and then explicitly choose Connect before Cloud mints the five-minute
# pairing code. No server secret exists during this wait.
DEFAULT_TIMEOUT_S = 300.0

_CALLBACK_HTML = (
    b"<!doctype html><html><head><meta charset='utf-8'>"
    b"<title>OpenAdapt login complete</title></head>"
    b"<body style='font-family:system-ui;text-align:center;padding-top:3rem'>"
    b"<h2>You're signed in.</h2>"
    b"<p>You can close this tab and return to OpenAdapt.</p>"
    b"<script>window.setTimeout(function(){window.close();},800);</script>"
    b"</body></html>"
)

_ERROR_HTML = (
    b"<!doctype html><html><head><meta charset='utf-8'>"
    b"<title>OpenAdapt login failed</title></head>"
    b"<body style='font-family:system-ui;text-align:center;padding-top:3rem'>"
    b"<h2>Sign-in did not complete.</h2>"
    b"<p>Return to OpenAdapt for the reason.</p>"
    b"</body></html>"
)


def _write_callback_body(stream, body: bytes, event: threading.Event) -> None:
    """Signal callback completion even when the browser closes before reading."""
    try:
        stream.write(body)
    finally:
        # The callback parameters were already retained. A browser disconnect
        # during this courtesy page must not make login wait for the full
        # timeout or discard a valid one-use code.
        event.set()


def generate_pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for the S256 PKCE method."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


class _LoopbackReceiver:
    """Ephemeral 127.0.0.1 listener that captures one authorization code.

    Binds ``127.0.0.1`` explicitly -- never ``0.0.0.0`` -- so nothing outside
    this machine can reach it, and takes an ephemeral port because RFC 8252
    section 7.3 requires the client to choose one at request time.
    """

    def __init__(self) -> None:
        self.code: str | None = None
        self.state: str | None = None
        self.error_code: str | None = None
        self.error: str | None = None
        self._event = threading.Event()
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # noqa: D401 - silence stdlib logging
                pass

            def do_GET(self) -> None:  # noqa: N802 - stdlib naming
                parsed = urllib.parse.urlparse(self.path)
                # Exactly one path. A scan of the ephemeral port finds nothing
                # else, and no other route can deliver a code.
                if parsed.path != CALLBACK_PATH:
                    self.send_response(404)
                    self.end_headers()
                    return
                params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

                def single(key: str) -> str | None:
                    values = params.get(key) or []
                    return values[0] if len(values) == 1 else None

                # First delivery wins. A later request cannot overwrite a
                # captured code with a different one.
                if not parent._event.is_set():
                    parent.state = single("state")
                    code = single("code")
                    error_code = single("error")
                    description = single("error_description")
                    repeated = any(len(values) != 1 for values in params.values())
                    success_shape = set(params) == {"code", "state"}
                    error_shape = set(params) in (
                        {"error", "state"},
                        {"error", "error_description", "state"},
                    )
                    safe_description = description is None or (
                        len(description) <= 256
                        and not any(ord(char) < 32 or ord(char) == 127 for char in description)
                    )
                    if repeated or not safe_description:
                        parent.error = "The login callback was malformed."
                    elif success_shape and code is not None:
                        parent.code = code
                    elif error_shape and error_code is not None:
                        parent.error_code = error_code
                        parent.error = description or error_code
                    else:
                        parent.error = "The login callback was malformed."
                failed = parent.error is not None or parent.code is None
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                _write_callback_body(
                    self.wfile,
                    _ERROR_HTML if failed else _CALLBACK_HTML,
                    parent._event,
                )

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.port}{CALLBACK_PATH}"

    def __enter__(self) -> "_LoopbackReceiver":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def serve_until_code(self, timeout: float) -> None:
        """Serve until a callback arrives or ``timeout`` elapses."""
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self._event.wait(timeout=timeout)

    def close(self) -> None:
        """Tear the listener down. Safe to call twice, and on any exit path."""
        # HTTPServer.shutdown() deadlocks when serve_forever() never started,
        # for example when the browser launcher itself raises.
        if self._thread is not None:
            try:
                self._server.shutdown()
            except Exception:  # pragma: no cover - defensive stdlib boundary
                pass
        try:
            self._server.server_close()
        except Exception:  # pragma: no cover - already closed
            pass
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None


class BrowserPkceProvider:
    """System-browser + loopback PKCE provider.

    Args:
        host: Hosted base URL.
        open_browser: Callable that opens a URL in the system browser.
        timeout: Seconds to wait for the browser redirect.
    """

    name = "browser_pkce"

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        open_browser=None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        raw_host = host.rstrip("/")
        kind = self._kind_for_host(raw_host)
        try:
            self.host = _validate_destination(raw_host, kind)
            self._host_is_valid = True
        except PairingError:
            # Retain the safe, secret-free input only so `is_available()` can
            # refuse it. No login URL or claim uses an invalid destination.
            self.host = raw_host
            self._host_is_valid = False
        self._uses_system_browser = open_browser is None
        self._open_browser = open_browser or self._default_open_browser
        self._timeout = timeout

    @staticmethod
    def _default_open_browser(url: str) -> None:
        import webbrowser

        if not webbrowser.open(url):
            raise RuntimeError("OpenAdapt could not open the system browser.")

    def is_available(self) -> bool:
        """False on a headless box, and False without a usable OS keychain.

        The keychain check is not cosmetic: this flow spends a one-use server
        secret, so it must refuse BEFORE claiming when the resulting credential
        could not be stored securely.
        """
        if os.environ.get("OPENADAPT_HEADLESS", "").strip():
            return False
        if not self._host_is_valid:
            return False
        if not secure_store_available():
            return False
        if self._uses_system_browser:
            import webbrowser

            try:
                webbrowser.get()
            except webbrowser.Error:
                return False
        if sys.platform.startswith("linux"):
            # No X11 / Wayland display -> no system browser to drive.
            return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        # macOS / Windows always have a default browser + loopback.
        return True

    def login(self) -> Credential:
        """Run the browser login and return the stored credential.

        Returns:
            The stored ``Credential`` (bearer = the minted ingest token).

        Raises:
            RuntimeError: If the flow cannot complete (headless, denied,
                timeout, state mismatch, or a claim failure).
        """
        if not self.is_available():
            raise RuntimeError("Browser login is unavailable on this machine; use token paste.")

        verifier, challenge = generate_pkce_pair()
        state = secrets.token_urlsafe(24)

        # `with` rather than a caller-side try/finally: the listener is closed
        # on success, on failure, and on an exception raised inside the block.
        with _LoopbackReceiver() as receiver:
            auth_url = self._build_login_url(receiver.redirect_uri, challenge, state)
            logger.info("Opening the system browser to sign in to {host}", host=self.host)
            self._open_browser(auth_url)
            receiver.serve_until_code(self._timeout)
            code = receiver.code
            received_state = receiver.state
            error_code = receiver.error_code
            error = receiver.error

        # Every delivered callback, including a user refusal, must bind the
        # exact request. A missing state never degrades into an accepted error.
        if (code is not None or error is not None) and not secrets.compare_digest(
            received_state or "", state
        ):
            raise RuntimeError("Login state mismatch (possible CSRF); aborting.")
        if error:
            if error_code not in {"access_denied", "server_error"}:
                raise RuntimeError("Login failed because the callback was malformed.")
            raise RuntimeError(f"Login was denied: {error}")
        if not code:
            raise RuntimeError(
                "Timed out waiting for the browser login to complete. "
                "Finish sign-in and choose Connect, then try again. "
                "You can also paste an ingest token."
            )
        # This is our one-use pairing grant, not a Supabase authorization code
        # and not either runner-local portal credential. Refuse the confusion
        # before any network claim can spend or disclose it.
        if not PAIRING_SECRET_RE.fullmatch(code):
            raise RuntimeError("Login returned an invalid OpenAdapt pairing code.")

        try:
            claim_pairing(
                self.host,
                code,
                code_verifier=verifier,
                destination_kind=self._destination_kind(),
            )
        except PairingError as exc:
            raise RuntimeError(f"Login failed: {exc}") from exc

        cred = load_credential(self.host)
        if cred is None:
            raise RuntimeError(
                "Login completed but the credential could not be read back from the keychain."
            )
        logger.info("Browser login complete; credential stored for {host}", host=self.host)
        return cred

    def _destination_kind(self) -> str | None:
        """Classify the host for the pairing destination policy."""
        return self._kind_for_host(self.host)

    @staticmethod
    def _kind_for_host(host: str) -> str | None:
        hostname = urllib.parse.urlparse(host).hostname
        return "local" if hostname in {"localhost", "127.0.0.1", "::1"} else None

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

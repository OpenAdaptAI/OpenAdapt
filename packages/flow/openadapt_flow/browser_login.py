"""Sign in to OpenAdapt Cloud from the terminal with the system browser.

``openadapt login`` with no token runs this. It is an RFC 8252 native-app
login, the same one OpenAdapt Desktop uses, and it needs no Cloud change:

1. Make an S256 PKCE verifier and challenge, and listen on
   ``127.0.0.1:<ephemeral port>/callback`` (loopback only, one path).
2. Print ``{host}/login?redirect_to=…&code_challenge=…&state=…``. The browser
   is opened only when the caller passes ``open_browser`` (``--open``).
3. The person signs in (Google or a magic link). A first-time user gets a
   workspace from onboarding on the way. Cloud's ``/auth/loopback`` page asks
   them to approve this computer, mints a five-minute pairing code bound to
   the challenge, and redirects to the listener with ``?code=oap_…&state=…``.
4. :func:`openadapt_flow.hosted.connect` redeems the code with the verifier.
   It stages, validates, promotes and confirms the new ``oai_ingest_``
   credential in the OS keychain, the entry ``push`` and ``report-run`` read,
   and rolls back on any failure.

No secret is ever in a URL this module builds: the URL carries only the
challenge and an opaque state. The one-use code reaches only the loopback
listener, and it is worthless without the verifier, which never leaves this
process.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Optional

from openadapt_flow.hosted import (
    PAIRING_SECRET_RE,
    HostedError,
    connect,
    resolve_host,
)

LOGIN_PATH = "/login"
CALLBACK_PATH = "/callback"
#: Long enough for a magic-link email and the approval click.
DEFAULT_TIMEOUT_S = 300.0

_DONE_HTML = (
    b"<!doctype html><html><head><meta charset='utf-8'>"
    b"<title>OpenAdapt sign-in</title></head>"
    b"<body style='font-family:system-ui;text-align:center;padding-top:3rem'>"
    b"<h2>Signed in.</h2><p>You can close this tab and go back to the terminal.</p>"
    b"</body></html>"
)
_FAILED_HTML = (
    b"<!doctype html><html><head><meta charset='utf-8'>"
    b"<title>OpenAdapt sign-in</title></head>"
    b"<body style='font-family:system-ui;text-align:center;padding-top:3rem'>"
    b"<h2>Sign-in did not finish.</h2><p>The terminal says why.</p>"
    b"</body></html>"
)


class BrowserLoginError(HostedError):
    """Why the browser sign-in did not finish, in one plain line."""


def pkce_pair() -> tuple[str, str]:
    """Return ``(verifier, challenge)`` for the S256 method (RFC 7636)."""

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


class LoopbackReceiver:
    """A ``127.0.0.1`` listener that keeps the first callback it receives.

    It binds 127.0.0.1, never 0.0.0.0, on a port the OS picks, and serves only
    ``/callback``. A callback must carry exactly ``code`` and ``state``, or
    ``error`` (with an optional short ``error_description``) and ``state``;
    anything else counts as malformed. A later request cannot replace the
    first one.
    """

    def __init__(self) -> None:
        self.code: Optional[str] = None
        self.state: Optional[str] = None
        self.error_code: Optional[str] = None
        self.error: Optional[str] = None
        self._received = threading.Event()
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802 - stdlib name
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != CALLBACK_PATH:
                    self.send_response(404)
                    self.end_headers()
                    return
                if not receiver._received.is_set():
                    receiver._take(
                        urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                    )
                failed = receiver.code is None
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                try:
                    self.wfile.write(_FAILED_HTML if failed else _DONE_HTML)
                finally:
                    receiver._received.set()

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread: Optional[threading.Thread] = None

    def _take(self, params: dict[str, list[str]]) -> None:
        if any(len(values) != 1 for values in params.values()):
            self.error = "the sign-in callback was malformed"
            return
        flat = {key: values[0] for key, values in params.items()}
        self.state = flat.get("state")
        keys = set(flat)
        if keys == {"code", "state"}:
            self.code = flat["code"]
        elif keys in ({"error", "state"}, {"error", "error_description", "state"}):
            description = flat.get("error_description") or flat["error"]
            if len(description) > 256 or any(
                ord(char) < 32 or ord(char) == 127 for char in description
            ):
                self.error = "the sign-in callback was malformed"
            else:
                self.error_code = flat["error"]
                self.error = description
        else:
            self.error = "the sign-in callback was malformed"

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}{CALLBACK_PATH}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def wait(self, timeout: float) -> bool:
        return self._received.wait(timeout=timeout)

    def close(self) -> None:
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(timeout=5.0)
            self._thread = None
        self._server.server_close()

    def __enter__(self) -> "LoopbackReceiver":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def login_url(host: str, redirect_uri: str, challenge: str, state: str) -> str:
    query = urllib.parse.urlencode(
        {
            "redirect_to": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    return f"{host}{LOGIN_PATH}?{query}"


def browser_login(
    host: Optional[str] = None,
    *,
    show_url: Callable[[str], None],
    open_browser: Optional[Callable[[str], Any]] = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    device_name: Optional[str] = None,
    destination_kind: Optional[str] = None,
    trusted_hosts: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Sign in through the browser and keep the credential in the OS keychain.

    ``show_url`` always receives the sign-in URL before anything waits.
    ``open_browser``, when given, is then called with the same URL; without it
    no window opens. Returns :func:`~openadapt_flow.hosted.connect`'s result.

    Raises:
        BrowserLoginError: denied, timed out, a state mismatch, or a malformed
            callback.
        HostedError: the claim, validation or keychain step failed (nothing
            is left half-stored; ``connect`` rolls back).
    """

    resolved_host = resolve_host(host)
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    with LoopbackReceiver() as receiver:
        url = login_url(resolved_host, receiver.redirect_uri, challenge, state)
        receiver.start()
        show_url(url)
        if open_browser is not None:
            try:
                open_browser(url)
            except Exception:  # noqa: BLE001 - the URL is already printed
                pass
        receiver.wait(timeout)
        code, received_state = receiver.code, receiver.state
        error_code, error = receiver.error_code, receiver.error

    if code is None and error is None:
        raise BrowserLoginError(
            f"No sign-in arrived within {int(timeout)} seconds. Run the command again."
        )
    if not secrets.compare_digest(received_state or "", state):
        raise BrowserLoginError("The sign-in callback did not match this request.")
    if error is not None:
        if error_code == "access_denied":
            raise BrowserLoginError(f"Sign-in was not approved: {error}")
        raise BrowserLoginError(f"Sign-in failed: {error}")
    if not PAIRING_SECRET_RE.fullmatch(code or ""):
        raise BrowserLoginError("The sign-in callback carried a malformed code.")
    return connect(
        code or "",
        host=resolved_host,
        device_name=device_name,
        destination_kind=destination_kind,
        trusted_hosts=trusted_hosts,
        code_verifier=verifier,
    )


# ---------------------------------------------------------------------------
# `openadapt-flow login` (and `openadapt login`) command-line wiring
# ---------------------------------------------------------------------------


def add_cli_arguments(parser: Any) -> None:
    """Add ``--browser``/``--no-browser``, ``--open`` and ``--timeout`` to ``login``."""

    choice = parser.add_mutually_exclusive_group()
    choice.add_argument(
        "--browser",
        dest="browser",
        action="store_true",
        default=None,
        help=(
            "Sign in through the browser even when a token is stored. This is "
            "the default when no token is given or stored"
        ),
    )
    choice.add_argument(
        "--no-browser",
        dest="browser",
        action="store_false",
        help="Use only --token, OPENADAPT_INGEST_TOKEN or the stored token",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open the sign-in link in the default browser (it is printed either way)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help=f"Seconds to wait for the browser sign-in (default {int(DEFAULT_TIMEOUT_S)})",
    )


def uses_browser(args: Any) -> bool:
    """Browser sign-in unless a token was given or one is already stored.

    ``--browser`` forces it and ``--no-browser`` keeps the token path, so
    ``login`` with a stored token still re-validates it as before.
    """

    from openadapt_flow.hosted import resolve_token

    if getattr(args, "browser", None) is not None:
        return bool(args.browser)
    if args.token or args.no_save or args.allow_plaintext_token:
        return False
    try:
        resolve_token(None, host=args.host)
    except HostedError:
        return True
    return False


def run_cli(args: Any) -> int:
    """``login`` with no token: print the sign-in link, wait, store the credential."""

    def show(url: str) -> None:
        print(
            "Sign in to OpenAdapt Cloud. Open this link in a browser on this computer:"
        )
        print(f"  {url}")
        if not args.open:
            print("(Add --open to open it for you.)")
        print(
            "Waiting for you to approve this computer... (Ctrl-C to cancel)", flush=True
        )

    opener = None
    if args.open:
        import webbrowser

        opener = webbrowser.open
    try:
        result = browser_login(
            args.host,
            show_url=show,
            open_browser=opener,
            timeout=args.timeout or DEFAULT_TIMEOUT_S,
            destination_kind=args.destination_kind,
            trusted_hosts=args.trusted_host,
        )
    except KeyboardInterrupt:
        print("login cancelled; nothing was stored.")
        return 130
    except HostedError as e:
        print(f"login failed: {e}")
        return 1
    print(
        f"Signed in to {result['host']} as {result['device_name']}. "
        "The credential is in the OS keychain."
    )
    print(f"Manage or revoke it at {result['settings_url']}")
    return 0

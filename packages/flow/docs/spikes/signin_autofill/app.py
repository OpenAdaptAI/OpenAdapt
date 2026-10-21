"""Tiny localhost sign-in app for the Chrome autofill spike.

One login form at ``/`` and one page behind it at ``/protected``. The account
comes from ``test_account.local.json`` (gitignored; run ``make_account.py``).
The server never logs request bodies, so the password does not reach its log.
``/status`` reports sign-in results as counts, for the harness to read.

    python app.py --port 47811   # the taught origin
    python app.py --port 47812   # a second origin serving the same form
    python app.py --port 47813 --title-port 47811   # look-alike title
"""

from __future__ import annotations

import argparse
import hmac
import html
import json
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

HERE = Path(__file__).resolve().parent

FORM = """<!doctype html>
<html><head><meta charset="utf-8"><title>{title_prefix} {port}</title>
<style>
body {{ font: 16px -apple-system, sans-serif; margin: 48px; }}
label {{ display: block; margin-top: 16px; }}
input {{ font-size: 16px; padding: 6px; width: 280px; }}
button {{ margin-top: 20px; font-size: 16px; padding: 8px 20px; }}
</style></head>
<body>
<h1>Spike Login</h1>
<p>{message}</p>
<form method="post" action="/login">
<label for="username">Username</label>
<input id="username" name="username" type="text" autocomplete="username">
<label for="password">Password</label>
<input id="password" name="password" type="password"
 autocomplete="current-password">
<br><button id="submit" type="submit">Sign in</button>
</form>
</body></html>
"""

PROTECTED = """<!doctype html>
<html><head><meta charset="utf-8"><title>{protected_prefix} {port}</title></head>
<body style="font: 16px -apple-system, sans-serif; margin: 48px">
<h1>Protected page</h1><p>Signed in as {user}.</p>
<p><a href="/logout">Sign out</a></p>
</body></html>
"""


def load_account() -> dict[str, str]:
    path = HERE / "test_account.local.json"
    if not path.exists():
        sys.exit("no test account yet; run make_account.py first")
    return json.loads(path.read_text())


def make_handler(
    port: int, account: dict[str, str], title_port: int, titles: tuple[str, str]
) -> type:
    cookie = f"spike_sid_{port}"  # cookies ignore ports, so name them per port
    sessions: set[str] = set()
    stats = {"ok": 0, "fail": 0, "protected_views": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            # Request line only: method and path, never a body.
            sys.stderr.write(f"[{port}] {self.command} {self.path}\n")

        def _send(self, code: int, body: str, ctype: str = "text/html") -> None:
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, where: str, set_cookie: str | None = None) -> None:
            self.send_response(303)
            self.send_header("Location", where)
            if set_cookie is not None:
                self.send_header("Set-Cookie", set_cookie)
            self.end_headers()

        def _session(self) -> str | None:
            raw = self.headers.get("Cookie", "")
            for part in raw.split(";"):
                name, _, value = part.strip().partition("=")
                if name == cookie and value in sessions:
                    return value
            return None

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/status":
                self._send(200, json.dumps(stats), "application/json")
            elif self.path == "/protected":
                if self._session() is None:
                    self._redirect("/")
                    return
                stats["protected_views"] += 1
                user = html.escape(account["username"])
                self._send(
                    200,
                    PROTECTED.format(
                        protected_prefix=titles[1], port=title_port, user=user
                    ),
                )
            elif self.path == "/logout":
                sid = self._session()
                if sid is not None:
                    sessions.discard(sid)
                self._redirect("/", f"{cookie}=; Max-Age=0; Path=/")
            elif self.path in ("/", "/login"):
                self._send(
                    200,
                    FORM.format(
                        title_prefix=titles[0],
                        port=title_port,
                        message="Please sign in.",
                    ),
                )
            else:
                self._send(404, "not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/login":
                self._send(404, "not found", "text/plain")
                return
            length = int(self.headers.get("Content-Length", "0") or 0)
            fields = parse_qs(self.rfile.read(length).decode())
            user = (fields.get("username") or [""])[0]
            password = (fields.get("password") or [""])[0]
            good = hmac.compare_digest(
                user.encode(), account["username"].encode()
            ) and hmac.compare_digest(password.encode(), account["password"].encode())
            del password
            if not good:
                stats["fail"] += 1
                self._send(
                    401,
                    FORM.format(
                        title_prefix=titles[0],
                        port=title_port,
                        message="Sign-in failed. Try again.",
                    ),
                )
                return
            stats["ok"] += 1
            sid = secrets.token_urlsafe(24)
            sessions.add(sid)
            self._redirect(
                "/protected", f"{cookie}={sid}; Path=/; HttpOnly; SameSite=Lax"
            )

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=47811)
    parser.add_argument(
        "--title-port",
        type=int,
        default=None,
        help="show another port in the page title (a look-alike page)",
    )
    parser.add_argument(
        "--stable-title",
        action="store_true",
        help="title both pages 'Spike App <port>' so a title-bound recorder "
        "keeps the window across the sign-in navigation",
    )
    args = parser.parse_args()
    account = load_account()
    titles = (
        ("Spike App", "Spike App")
        if args.stable_title
        else ("Spike Login", "Protected")
    )
    handler = make_handler(args.port, account, args.title_port or args.port, titles)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    sys.stderr.write(f"serving http://localhost:{args.port}/\n")
    server.serve_forever()


if __name__ == "__main__":
    main()

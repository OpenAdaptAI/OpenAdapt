"""A synthetic notes app behind a sign-in form, for the wrap sign-in tests.

``GET /`` shows the sign-in form until the browser holds a session cookie;
after a correct username and password it shows a notes form. ``GET
/api/notes`` is an unauthenticated read of the saved notes, which the
program's read-back uses. Made-up data only; it listens on 127.0.0.1.
"""

from __future__ import annotations

import html
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs

SIGN_IN = """<!doctype html><html><head><title>Notes sign in</title>
<style>td{{padding:6px 10px}}td.ctx{{padding-left:120px}}</style></head><body>
<h1>Notes</h1>{msg}<form method="post" action="/login"><fieldset><legend>Sign in</legend><table>
<tr><td><label for="user">Username</label></td><td><input id="user" name="user" type="text"></td><td class="ctx">Sign in: staging notebook account</td></tr>
<tr><td><label for="pass">Password</label></td><td><input id="pass" name="pass" type="password"></td><td class="ctx">Sign in: staging notebook account</td></tr>
<tr><td></td><td><button type="submit">Sign in</button></td><td class="ctx">Sign in: staging notebook account</td></tr>
</table></fieldset></form></body></html>"""

NOTES = """<!doctype html><html><head><title>Notes</title>
<style>td{{padding:6px 10px}}td.ctx{{padding-left:120px}}</style></head><body>
<h1>Notes</h1><form method="post" action="/save"><fieldset><legend>Notes</legend><table>
<tr><td><label for="note">Note</label></td><td><input id="note" name="note" type="text"></td><td class="ctx">Notes: staging notebook, list demo</td></tr>
<tr><td></td><td><button type="submit">Save</button></td><td class="ctx">Notes: staging notebook, list demo</td></tr>
</table></fieldset></form>{msg}<ul>{items}</ul></body></html>"""


class SignInApp:
    def __init__(self, username: str, password: str) -> None:
        self.username = username
        self.password = password
        self.records: list[dict[str, object]] = []
        self.sessions: set[str] = set()
        self.sign_ins = 0
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self.records.clear()
            self.sessions.clear()

    def handler(self) -> type[BaseHTTPRequestHandler]:
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                return None

            def _send(self, status: int, body: str, **headers: str) -> None:
                data = body.encode()
                self.send_response(status)
                self.send_header(
                    "content-type", headers.pop("content_type", "text/html")
                )
                for key, value in headers.items():
                    self.send_header(key.replace("_", "-"), value)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _signed_in(self) -> bool:
                cookie = self.headers.get("cookie") or ""
                for part in cookie.split(";"):
                    name, _, value = part.strip().partition("=")
                    if name == "session" and value in app.sessions:
                        return True
                return False

            def _form(self) -> dict[str, str]:
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length).decode()
                return {k: v[0] for k, v in parse_qs(raw).items()}

            def _notes(self, msg: str = "") -> None:
                items = "".join(
                    f"<li>{html.escape(str(r['note']))}</li>" for r in app.records
                )
                self._send(200, NOTES.format(msg=msg, items=items))

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/api/notes":
                    self._send(
                        200,
                        json.dumps({"records": app.records}),
                        content_type="application/json",
                    )
                elif self._signed_in():
                    self._notes()
                else:
                    self._send(200, SIGN_IN.format(msg=""))

            def do_POST(self) -> None:  # noqa: N802
                form = self._form()
                if self.path == "/login":
                    if (
                        form.get("user") == app.username
                        and form.get("pass") == app.password
                    ):
                        token = secrets.token_hex(16)
                        with app._lock:
                            app.sessions.add(token)
                            app.sign_ins += 1
                        self._send(
                            303,
                            "",
                            location="/",
                            set_cookie=f"session={token}; Path=/; HttpOnly",
                        )
                    else:
                        self._send(
                            401,
                            SIGN_IN.format(msg="<p>Wrong username or password.</p>"),
                        )
                elif self.path == "/save" and self._signed_in():
                    note = form.get("note", "")[:200]
                    with app._lock:
                        app.records.append(
                            {
                                "id": len(app.records) + 1,
                                "key": "staging-demo",
                                "list": "demo",
                                "note": note,
                            }
                        )
                    self._notes("<p>Saved!</p>")
                else:
                    self._send(403, SIGN_IN.format(msg="<p>Sign in first.</p>"))

        return Handler


def serve(username: str, password: str) -> tuple[str, SignInApp, Callable[[], None]]:
    app = SignInApp(username, password)
    server = ThreadingHTTPServer(("127.0.0.1", 0), app.handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def stop() -> None:
        server.shutdown()
        server.server_close()

    return f"http://127.0.0.1:{server.server_address[1]}/", app, stop

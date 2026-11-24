"""Synthetic notes app for the customer-run runner end-to-end check.

It stands in for an internal application: it is reachable only on the private
Docker network, under a DNS name that does not resolve on the public internet,
over HTTPS signed by a local CA. It holds made-up records that include canary
strings; the check then searches everything the runner sent for them.

* ``GET /``           the notes form (what the program types into)
* ``POST /save``      saves a note
* ``GET /api/notes``  the records, for the program's independent read-back;
                      requires ``X-Api-Key`` (the runner's local secret)

Synthetic data only.
"""

from __future__ import annotations

import html
import json
import os
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

API_KEY = os.environ["NOTES_READER_KEY"]
STORE = Path(os.environ.get("NOTES_STORE", "/data/notes.json"))
CANARY = os.environ.get("NOTES_CANARY", "ZQX-CANARY-RECORD")
LOCK = threading.Lock()

PAGE = """<!doctype html><html><head><title>Notes</title>
<style>td{{padding:6px 10px}}td.ctx{{padding-left:120px}}</style></head><body>
<h1>Notes</h1><form method="post" action="/save"><fieldset><legend>Notes</legend><table>
<tr><td><label for="note">Note</label></td><td><input id="note" name="note" type="text"></td><td class="ctx">Notes: clinic notebook, list demo</td></tr>
<tr><td></td><td><button type="submit">Save</button></td><td class="ctx">Notes: clinic notebook, list demo</td></tr>
</table></fieldset></form>{msg}<ul>{items}</ul></body></html>"""


def records() -> list[dict]:
    if not STORE.exists():
        STORE.parent.mkdir(parents=True, exist_ok=True)
        STORE.write_text("[]")
    return json.loads(STORE.read_text())


def seed_canaries() -> None:
    """Add made-up records that hold canary strings. Run after the program is
    published, as real records would exist only at run time."""
    with LOCK:
        current = records()
        next_id = (current[-1]["id"] + 1) if current else 1
        for n, text in enumerate(
            (
                f"{CANARY}-1 synthetic patient Jane Roe DOB 1970-01-01",
                f"{CANARY}-2 synthetic MRN 000-111-222",
            )
        ):
            current.append(
                {"id": next_id + n, "note": text, "list": "demo", "key": "clinic"}
            )
        STORE.write_text(json.dumps(current))


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _page(self, msg: str = "") -> None:
        items = "".join(f"<li>{html.escape(r['note'])}</li>" for r in records())
        self._send(
            200, PAGE.format(msg=msg, items=items).encode(), "text/html; charset=utf-8"
        )

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/":
            return self._page()
        if self.path == "/api/notes":
            if self.headers.get("x-api-key") != API_KEY:
                return self._send(401, b'{"error":"unauthorized"}', "application/json")
            return self._send(
                200, json.dumps({"records": records()}).encode(), "application/json"
            )
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/save":
            return self._send(404, b"not found", "text/plain")
        length = min(int(self.headers.get("content-length") or 0), 2000)
        note = (parse_qs(self.rfile.read(length).decode()).get("note") or [""])[0][:200]
        with LOCK:
            current = records()
            if note:
                next_id = (current[-1]["id"] + 1) if current else 1
                current.append(
                    {"id": next_id, "note": note, "list": "demo", "key": "clinic"}
                )
                STORE.write_text(json.dumps(current[-500:]))
        self._page("<p>Saved!</p>")

    def log_message(self, fmt: str, *args: object) -> None:
        print("notes:", self.command, self.path, flush=True)


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", 443), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(os.environ["NOTES_CERT"], os.environ["NOTES_KEY"])
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    print("notes app listening on 443", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["seed-canaries"]:
        seed_canaries()
    else:
        main()

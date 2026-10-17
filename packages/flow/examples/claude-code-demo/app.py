"""A small expenses app backed by SQLite, used as the demo target.

It is an ordinary web form and knows nothing about OpenAdapt. Adding an
expense inserts one row into the ``expenses`` table of a SQLite file.

    python app.py --db expenses.db --port 8642

``--drop-writes`` starts a broken variant for the halt demo: the page still
says "Expense saved.", but the row is never written.

Standard library only.
"""

from __future__ import annotations

import argparse
import html
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

#: The employee every expense in this demo is filed under.
EMPLOYEE_ID = "E-1042"

SCHEMA = """
CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    description TEXT NOT NULL,
    amount TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    status TEXT NOT NULL
);
"""

SEED = [
    ("Train ticket to client site", "38.20"),
    ("Printer paper", "12.99"),
]

STYLE = """
* { box-sizing: border-box; }
body { margin: 0; font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI",
  Roboto, sans-serif; color: #1f2328; background: #f6f8fa; }
header { display: flex; justify-content: space-between; background: #24292f;
  color: #fff; padding: 14px 24px; font-weight: 600; }
nav a { color: #d0d7de; margin-left: 18px; font-weight: 400; text-decoration: none; }
main { max-width: 760px; margin: 24px auto; padding: 0 16px; }
.card { background: #fff; border: 1px solid #d0d7de; border-radius: 8px;
  padding: 20px; margin-bottom: 20px; }
h2 { margin: 0 0 14px; font-size: 17px; }
.field { display: grid; grid-template-columns: 190px 1fr; align-items: center;
  gap: 12px; margin-bottom: 12px; }
label { font-weight: 600; }
input { width: 100%; padding: 8px 10px; font-size: 15px; border: 1px solid #d0d7de;
  border-radius: 6px; }
.actions { display: flex; align-items: center; justify-content: space-between;
  margin-top: 16px; }
.owner { color: #57606a; }
button { padding: 9px 18px; font-size: 15px; font-weight: 600; color: #fff;
  background: #1f883d; border: 0; border-radius: 6px; cursor: pointer; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 8px 6px; border-bottom: 1px solid #eaeef2; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.banner { background: #dafbe1; border: 1px solid #4ac26b; color: #116329;
  border-radius: 6px; padding: 10px 14px; margin-bottom: 20px; }
"""


@contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open the database, commit on success, and always close."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db(db_path: Path, *, reset: bool = False) -> None:
    """Create the table. With ``reset``, drop existing rows and reseed."""
    with connect(db_path) as conn:
        if reset:
            conn.execute("DROP TABLE IF EXISTS expenses")
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM expenses").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO expenses (description, amount, submitted_by, status)"
                " VALUES (?, ?, ?, 'submitted')",
                [(d, a, EMPLOYEE_ID) for d, a in SEED],
            )


def page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>{STYLE}</style></head>
<body><header>Expenses <nav><a href="/">New expense</a>
<a href="/expenses">All expenses</a></nav></header><main>{body}</main></body></html>"""


def render_form(saved: bool) -> str:
    banner = (
        '<div class="banner" id="saved-banner">Expense saved.</div>' if saved else ""
    )
    return page(
        "New expense",
        f"""{banner}
<section class="card"><h2>New expense</h2>
<form method="post" action="/expenses" autocomplete="off">
<div class="field"><label for="description">Expense description</label>
<input id="description" name="description" required></div>
<div class="field"><label for="amount">Amount in US dollars</label>
<input id="amount" name="amount" inputmode="decimal" required></div>
<div class="actions"><button id="save" type="submit">Add expense</button>
<span class="owner">Filed under Dana Demo, Engineering</span></div>
</form></section>""",
    )


def render_list(db_path: Path) -> str:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, description, amount, status FROM expenses ORDER BY id DESC"
        ).fetchall()
    body_rows = "".join(
        f"<tr><td>{r['id']}</td><td>{html.escape(r['description'])}</td>"
        f"<td class=num>{html.escape(r['amount'])}</td>"
        f"<td>{html.escape(r['status'])}</td></tr>"
        for r in rows
    )
    return page(
        "All expenses",
        f"""<section class="card"><h2>All expenses</h2>
<table><thead><tr><th>#</th><th>Description</th><th class=num>Amount</th>
<th>Status</th></tr></thead><tbody>{body_rows}</tbody></table></section>""",
    )


def make_handler(
    db_path: Path, *, drop_writes: bool = False
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        #: Broken mode: report "Expense saved." without writing the row.
        drop_writes = False

        def log_message(self, format: str, *args: object) -> None:
            return

        def _send(self, status: int, body: str, content_type: str) -> None:
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            url = urlparse(self.path)
            if url.path == "/":
                saved = parse_qs(url.query).get("saved") == ["1"]
                self._send(200, render_form(saved), "text/html; charset=utf-8")
            elif url.path == "/expenses":
                self._send(200, render_list(db_path), "text/html; charset=utf-8")
            else:
                self._send(404, "not found", "text/plain")

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/expenses":
                self._send(404, "not found", "text/plain")
                return
            length = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(length).decode())
            description = (form.get("description") or [""])[0].strip()
            amount = (form.get("amount") or [""])[0].strip()
            if not description or not amount:
                self._send(400, "description and amount are required", "text/plain")
                return
            if not self.drop_writes:
                with connect(db_path) as conn:
                    conn.execute(
                        "INSERT INTO expenses"
                        " (description, amount, submitted_by, status)"
                        " VALUES (?, ?, ?, 'submitted')",
                        (description, amount, EMPLOYEE_ID),
                    )
            self.send_response(303)
            self.send_header("Location", "/?saved=1")
            self.end_headers()

    Handler.drop_writes = drop_writes
    return Handler


def serve(
    db_path: Path, host: str = "127.0.0.1", port: int = 0
) -> tuple[str, ThreadingHTTPServer]:
    """Start the app in a background thread. Returns (base_url, server)."""
    init_db(db_path)
    server = ThreadingHTTPServer((host, port), make_handler(db_path))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://{host}:{server.server_address[1]}", server


def main() -> None:
    parser = argparse.ArgumentParser(description="Expenses demo app (SQLite).")
    parser.add_argument("--db", default="expenses.db")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8642)
    parser.add_argument("--reset", action="store_true", help="drop rows and reseed")
    parser.add_argument(
        "--drop-writes",
        action="store_true",
        help="broken mode: show 'Expense saved.' without writing the row",
    )
    args = parser.parse_args()
    db_path = Path(args.db).resolve()
    init_db(db_path, reset=args.reset)
    server = ThreadingHTTPServer(
        (args.host, args.port), make_handler(db_path, drop_writes=args.drop_writes)
    )
    mode = " (broken: saves are dropped)" if args.drop_writes else ""
    print(f"Expenses app on http://{args.host}:{args.port}{mode}  database: {db_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

"""A small reports portal to teach a first browser task on.

``openadapt-agent example-app`` serves it at http://127.0.0.1:8765. It has
made-up sales orders from 2026-01-01 to 2026-06-30, a report page filtered by
date range and region, and an Export CSV link that downloads the filtered
orders. The data never changes between runs, so a download can be checked.

The report page also has an "Email this report" form that sends a POST. The
read-only teach lane must refuse it; tests read ``/_stats`` to check that no
POST ever reached the app.

For taught writes it has a sign-in (``/login``, user ``clerk``, password
``portal-demo``) and a New Order form (``/orders/new``) that saves by POST.
``GET /api/orders`` returns the saved orders, so a write contract can read
them back.

Standard library only. It listens on 127.0.0.1 and nowhere else.
"""

from __future__ import annotations

import csv
import html
import io
import json
import random
import secrets
import threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlsplit

__all__ = ["COLUMNS", "ORDERS", "DEFAULT_PORT", "filter_orders", "serve"]

DEFAULT_PORT = 8765
COLUMNS = ("order_id", "date", "region", "product", "units", "revenue")
REGIONS = ("East", "West", "North", "South")
_PRODUCTS = (
    ("Desk lamp", 24),
    ("Office chair", 189),
    ("Monitor arm", 69),
    ("Notebook pack", 12),
    ("Standing desk", 420),
    ("USB-C dock", 145),
)
_FIRST_DAY = date(2026, 1, 1)
_LAST_DAY = date(2026, 6, 30)


def _make_orders() -> list[dict[str, str]]:
    rng = random.Random(7)
    orders = []
    day = _FIRST_DAY
    number = 1000
    while day <= _LAST_DAY:
        for _ in range(rng.randint(1, 3)):
            number += 1
            product, price = rng.choice(_PRODUCTS)
            units = rng.randint(1, 8)
            orders.append(
                {
                    "order_id": f"SO-{number}",
                    "date": day.isoformat(),
                    "region": rng.choice(REGIONS),
                    "product": product,
                    "units": str(units),
                    "revenue": f"{units * price:.2f}",
                }
            )
        day += timedelta(days=1)
    return orders


ORDERS: tuple[dict[str, str], ...] = tuple(_make_orders())


def _valid_day(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return len(value) == 10


def filter_orders(start: str, end: str, region: str = "") -> list[dict[str, str]]:
    """Orders dated from ``start`` to ``end`` (inclusive, ISO dates), in
    ``region`` when one is given."""

    return [
        row
        for row in ORDERS
        if start <= row["date"] <= end and (not region or row["region"] == region)
    ]


_STYLE = """
body{font:15px/1.45 system-ui,sans-serif;margin:0;color:#1d2433;background:#f6f7f9}
header{background:#1d2433;color:#fff;padding:12px 24px}
header a{color:#fff;margin-right:16px}
main{max-width:960px;margin:24px auto;padding:0 16px}
form{display:flex;gap:12px;align-items:end;flex-wrap:wrap;margin:12px 0}
label{display:flex;flex-direction:column;font-size:13px}
input,select,button{font:inherit;padding:6px 8px}
table{border-collapse:collapse;width:100%;background:#fff}
td,th{border:1px solid #d9dde5;padding:4px 8px;text-align:left}
.error{color:#a3121b}
section{margin-top:28px;padding-top:12px;border-top:1px solid #d9dde5}
"""


def _page(title: str, body: str) -> bytes:
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head><body>"
        '<header><a href="/">Home</a><a href="/reports">Sales report</a></header>'
        f"<main>{body}</main></body></html>"
    ).encode()


def _home() -> bytes:
    return _page(
        "Reports portal",
        "<h1>Reports portal</h1>"
        "<p>Made-up sales orders from 2026-01-01 to 2026-06-30.</p>"
        '<p><a href="/reports">Sales report</a></p>',
    )


def _report(query: dict[str, str]) -> bytes:
    start = query.get("from", "")
    end = query.get("to", "")
    region = query.get("region", "")
    options = "".join(
        f'<option value="{value}"{" selected" if value == region else ""}>'
        f"{label}</option>"
        for value, label in (("", "All regions"), *((r, r) for r in REGIONS))
    )
    body = (
        "<h1>Sales report</h1>"
        '<form method="get" action="/reports">'
        f'<label for="from">From date<input id="from" name="from" type="date" value="{html.escape(start)}"></label>'
        f'<label for="to">To date<input id="to" name="to" type="date" value="{html.escape(end)}"></label>'
        f'<label for="region">Region<select id="region" name="region">{options}</select></label>'
        '<button type="submit">Show report</button>'
        "</form>"
    )
    if start or end:
        if not (_valid_day(start) and _valid_day(end)):
            body += '<p class="error" role="alert">Enter both dates.</p>'
        elif start > end:
            body += (
                '<p class="error" role="alert">The from date is after the to date.</p>'
            )
        elif region and region not in REGIONS:
            body += '<p class="error" role="alert">Unknown region.</p>'
        else:
            rows = filter_orders(start, end, region)
            revenue = sum(float(row["revenue"]) for row in rows)
            link = "/reports/export.csv?" + urlencode(
                {"from": start, "to": end, "region": region}
            )
            body += (
                f"<p>{len(rows)} orders from {start} to {end}"
                f"{' in ' + region if region else ''}, revenue "
                f"${revenue:,.2f}.</p>"
                f'<p><a href="{html.escape(link)}">Export CSV</a></p>'
                "<table><thead><tr>"
                + "".join(f"<th>{c}</th>" for c in COLUMNS)
                + "</tr></thead><tbody>"
                + "".join(
                    "<tr>"
                    + "".join(f"<td>{html.escape(row[c])}</td>" for c in COLUMNS)
                    + "</tr>"
                    for row in rows[:20]
                )
                + "</tbody></table>"
                + (f"<p>Showing 20 of {len(rows)}.</p>" if len(rows) > 20 else "")
            )
    body += (
        "<section><h2>Email this report</h2>"
        '<form method="post" action="/reports/email">'
        '<label for="email">Email address<input id="email" name="email" type="email"></label>'
        '<button type="submit">Send email</button>'
        "</form></section>"
    )
    return _page("Sales report", body)


def _csv(rows: list[dict[str, str]]) -> bytes:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode()


#: The demo sign-in for the order form. Made up; the app listens on 127.0.0.1.
DEMO_USER = "clerk"
DEMO_PASSWORD = "portal-demo"


def _login(error: str = "") -> bytes:
    return _page(
        "Sign in",
        "<h1>Sign in</h1>"
        + (f'<p class="error" role="alert">{html.escape(error)}</p>' if error else "")
        + '<form method="post" action="/login">'
        '<label for="username">Username<input id="username" name="username"></label>'
        '<label for="password">Password<input id="password" name="password" '
        'type="password"></label>'
        '<button type="submit">Sign in</button></form>',
    )


def _new_order(orders: list[dict[str, str]], error: str = "") -> bytes:
    options = "".join(
        f'<option value="{html.escape(name)}">{html.escape(name)}</option>'
        for name, _price in _PRODUCTS
    )
    recent = "".join(
        "<tr>"
        + "".join(f"<td>{html.escape(o[c])}</td>" for c in ORDER_FIELDS)
        + "</tr>"
        for o in orders[-5:]
    )
    return _page(
        "New order",
        "<h1>New order</h1>"
        + (f'<p class="error" role="alert">{html.escape(error)}</p>' if error else "")
        + '<form method="post" action="/orders">'
        '<label for="customer">Customer<input id="customer" name="customer"></label>'
        f'<label for="product">Product<select id="product" name="product">{options}</select></label>'
        '<label for="units">Units<input id="units" name="units" type="number" min="1"></label>'
        '<button type="submit">Save order</button></form>'
        + (
            "<section><h2>Recent orders</h2><table><thead><tr>"
            + "".join(f"<th>{c}</th>" for c in ORDER_FIELDS)
            + f"</tr></thead><tbody>{recent}</tbody></table></section>"
            if orders
            else ""
        ),
    )


ORDER_FIELDS = ("order_id", "customer", "product", "units")


def _handler(
    stats: dict[str, int],
    lock: threading.Lock,
    orders: list[dict[str, str]],
    sessions: set[str],
    fault: dict[str, str],
) -> type:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ReportsPortal/1"

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def _send(
            self, status: int, body: bytes, content_type: str, **headers: str
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for name, value in headers.items():
                self.send_header(name.replace("_", "-"), value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            parts = urlsplit(self.path)
            query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
            with lock:
                stats["get"] += 1
            if parts.path == "/":
                self._send(200, _home(), "text/html; charset=utf-8")
            elif parts.path == "/reports":
                self._send(200, _report(query), "text/html; charset=utf-8")
            elif parts.path == "/reports/export.csv":
                start, end = query.get("from", ""), query.get("to", "")
                region = query.get("region", "")
                if not (_valid_day(start) and _valid_day(end)) or start > end:
                    self._send(400, b"bad date range\n", "text/plain")
                    return
                with lock:
                    stats["exports"] += 1
                self._send(
                    200,
                    _csv(filter_orders(start, end, region)),
                    "text/csv; charset=utf-8",
                    Content_Disposition=(
                        f'attachment; filename="sales_{start}_to_{end}.csv"'
                    ),
                )
            elif parts.path == "/_stats":
                with lock:
                    body = json.dumps(stats).encode()
                self._send(200, body, "application/json")
            elif parts.path == "/login":
                self._send(200, _login(), "text/html; charset=utf-8")
            elif parts.path == "/orders/new":
                if not self._signed_in():
                    self._send(303, b"", "text/plain", Location="/login")
                    return
                with lock:
                    page = _new_order(list(orders))
                self._send(200, page, "text/html; charset=utf-8")
            elif parts.path.startswith("/orders/") and self._signed_in():
                order_id = parts.path.rsplit("/", 1)[-1]
                self._send(
                    200,
                    _page(
                        "Order saved",
                        f"<h1>Order {html.escape(order_id)} saved</h1>"
                        '<p><a href="/orders/new">New order</a></p>',
                    ),
                    "text/html; charset=utf-8",
                )
            elif parts.path == "/api/orders":
                # The order store, for a write contract to read back.
                with lock:
                    body = json.dumps({"records": list(orders)}).encode()
                self._send(200, body, "application/json")
            elif parts.path == "/_fault":
                # Tests only: "no_persist" answers saved but keeps nothing,
                # "twice" keeps the order twice.
                with lock:
                    fault["mode"] = query.get("mode", "")
                self._send(200, b"ok", "text/plain")
            else:
                self._send(404, _page("Not found", "<h1>Not found</h1>"), "text/html")

        def _signed_in(self) -> bool:
            cookie = self.headers.get("Cookie", "")
            token = next(
                (
                    part.split("=", 1)[1]
                    for part in cookie.split(";")
                    if part.strip().startswith("session=")
                ),
                "",
            )
            with lock:
                return bool(token) and token in sessions

        def _form(self) -> dict[str, str]:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8", "replace")
            return {k: v[-1] for k, v in parse_qs(body).items()}

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            path = urlsplit(self.path).path
            if path == "/login":
                form = self._form()
                if (form.get("username"), form.get("password")) != (
                    DEMO_USER,
                    DEMO_PASSWORD,
                ):
                    self._send(
                        200,
                        _login("Wrong username or password."),
                        "text/html; charset=utf-8",
                    )
                    return
                token = secrets.token_hex(16)
                with lock:
                    sessions.add(token)
                self._send(
                    303,
                    b"",
                    "text/plain",
                    Location="/orders/new",
                    Set_Cookie=f"session={token}; HttpOnly; Path=/",
                )
                return
            if path == "/orders":
                if not self._signed_in():
                    self._send(303, b"", "text/plain", Location="/login")
                    return
                form = self._form()
                customer = form.get("customer", "").strip()
                product = form.get("product", "")
                units = form.get("units", "").strip()
                if (
                    not customer
                    or product not in dict(_PRODUCTS)
                    or not units.isdigit()
                ):
                    with lock:
                        page = _new_order(list(orders), "Fill in every field.")
                    self._send(200, page, "text/html; charset=utf-8")
                    return
                with lock:
                    order_id = f"WO-{1001 + len(orders)}"
                    record = {
                        "order_id": order_id,
                        "customer": customer,
                        "product": product,
                        "units": units,
                    }
                    if fault.get("mode") != "no_persist":
                        orders.append(record)
                    if fault.get("mode") == "twice":
                        orders.append(
                            {**record, "order_id": f"WO-{1001 + len(orders)}"}
                        )
                self._send(303, b"", "text/plain", Location=f"/orders/{order_id}")
                return
            self._refuse_write()

        def _refuse_write(self) -> None:
            with lock:
                stats["writes"] += 1
            self._send(
                200,
                _page("Sent", "<h1>Report sent</h1><p>(Nothing is really sent.)</p>"),
                "text/html; charset=utf-8",
            )

        do_PUT = do_PATCH = do_DELETE = _refuse_write  # noqa: N815

    return Handler


def serve(port: int = 0) -> tuple[str, Callable[[], None]]:
    """Start the portal on 127.0.0.1 in a background thread.

    Returns its base URL (no trailing slash) and a function that stops it.
    ``port=0`` picks a free port.
    """

    stats = {"get": 0, "exports": 0, "writes": 0}
    server = ThreadingHTTPServer(
        ("127.0.0.1", port),
        _handler(stats, threading.Lock(), [], set(), {"mode": ""}),
    )
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def stop() -> None:
        server.shutdown()
        server.server_close()

    return f"http://127.0.0.1:{server.server_address[1]}", stop


def main(port: int = DEFAULT_PORT) -> int:
    """Serve until Ctrl-C."""

    try:
        url, stop = serve(port)
    except OSError as exc:
        print(f"example-app: can't listen on 127.0.0.1:{port} ({exc}); try --port")
        return 2
    print(f"Reports portal running at {url}/reports (Ctrl-C stops it)", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        stop()
    return 0

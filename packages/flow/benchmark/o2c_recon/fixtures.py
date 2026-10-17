"""Deterministic two-system fixture for the O2C reconciliation benchmark.

Two separate local applications with separate on-disk SQLite systems of
record, plus two spreadsheet surfaces on disk:

1. **Billing service** (system A, the revenue side): owns ``billed_orders``
   and, at reset, drops the nightly EXPORT spreadsheet
   (``export/billing_export.csv``) into a shared folder -- the worklist input.
   It also hosts the workbook gateway: ``POST /api/workbook/writeback``
   appends a result row to ``workbook/recon_results.csv`` (the written-back
   results sheet). The ``drop_writeback`` fault acknowledges the row (banner +
   HTTP 200) without persisting it -- the phantom-file-write class.

2. **Ledger service** (system B, the accounting side): owns
   ``ledger_entries`` and ``adjustments``. Adjustments are accepted ONLY
   through the UI gateway (``POST /ui/adjustment/new``; the API answers 405)
   and carry optimistic concurrency: a stale ``expected_prior`` is refused
   with 409 BEFORE anything is written. Reconciliation marks go through the
   REST API. The ``stale_snapshot`` fault makes the read API report a lagged
   amount for one order while the database has already moved on.

All data is synthetic. Localhost only. Zero model calls.
"""

from __future__ import annotations

import csv
import json
import sqlite3
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

#: (order_id, customer, amount_billed, amount_posted, period)
#: The first five agree across systems; the next five differ (adjustments);
#: the 9xxx orders exist for the exception scenarios.
ORDER_SEEDS: tuple[tuple[str, str, str, Optional[str], str], ...] = (
    ("ORD-9001", "Northwind Retail", "250.00", "250.00", "2026-06"),
    ("ORD-9002", "Cobalt Foods", "120.40", "120.40", "2026-06"),
    ("ORD-9003", "Juniper Health Supply", "78.25", "78.25", "2026-06"),
    ("ORD-9004", "Atlas Manufacturing", "1310.00", "1310.00", "2026-06"),
    ("ORD-9005", "Harbor Freight Lines", "99.00", "99.00", "2026-06"),
    ("ORD-9006", "Northwind Retail", "400.00", "380.00", "2026-06"),
    ("ORD-9007", "Cobalt Foods", "155.10", "165.10", "2026-06"),
    ("ORD-9008", "Atlas Manufacturing", "720.00", "700.00", "2026-06"),
    ("ORD-9009", "Juniper Health Supply", "88.88", "80.88", "2026-06"),
    ("ORD-9010", "Harbor Freight Lines", "505.50", "500.00", "2026-06"),
    # billing-only: no ledger entry exists (missing-record exception).
    ("ORD-9101", "Meridian Parts", "64.00", None, "2026-06"),
    # duplicated in the ledger (ambiguous-duplicate exception).
    ("ORD-9201", "Cobalt Foods", "310.00", "300.00", "2026-06"),
    # stale-snapshot target: DB holds 500.00, the lagged read reports 480.00.
    ("ORD-9301", "Atlas Manufacturing", "520.00", "500.00", "2026-06"),
    # phantom-writeback target: amounts agree; only the results sheet is at risk.
    ("ORD-9401", "Northwind Retail", "212.00", "212.00", "2026-06"),
)

SCENARIO_ORDER_IDS: dict[str, tuple[str, ...]] = {
    "healthy": tuple(f"ORD-90{i:02d}" for i in range(1, 11)),
    "missing_in_ledger": ("ORD-9001", "ORD-9101"),
    "ambiguous_duplicate": ("ORD-9201",),
    "stale_snapshot": ("ORD-9301",),
    "phantom_writeback": ("ORD-9401",),
}

STALE_ORDER = "ORD-9301"
STALE_REPORTED_AMOUNT = "480.00"
DUPLICATE_ORDER = "ORD-9201"

EXPORT_NAME = "billing_export.csv"
RESULTS_NAME = "recon_results.csv"
RESULTS_FIELDS = ("order_id", "disposition", "delta", "status")


@dataclass(frozen=True)
class BillingHandle:
    base_url: str
    db_path: Path
    export_path: Path
    results_path: Path
    stop: Callable[[], None]


@dataclass(frozen=True)
class LedgerHandle:
    base_url: str
    db_path: Path
    stop: Callable[[], None]


def _json_handler(
    route: Callable[[str, str, dict[str, Any]], tuple[int, dict[str, Any]]],
) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:  # noqa: A002
            pass

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if not length:
                return {}
            try:
                loaded = json.loads(self.rfile.read(length).decode("utf-8"))
                return loaded if isinstance(loaded, dict) else {}
            except (ValueError, UnicodeDecodeError):
                return {}

        def do_GET(self) -> None:  # noqa: N802
            status, payload = route("GET", urlparse(self.path).path, {})
            self._send(status, payload)

        def do_POST(self) -> None:  # noqa: N802
            status, payload = route("POST", urlparse(self.path).path, self._body())
            self._send(status, payload)

    return _Handler


def _serve(
    name: str,
    route: Callable[[str, str, dict[str, Any]], tuple[int, dict[str, Any]]],
    *,
    host: str = "127.0.0.1",
    port: int = 0,
) -> tuple[str, Callable[[], None]]:
    httpd = ThreadingHTTPServer((host, port), _json_handler(route))
    actual_port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, name=name, daemon=True)
    thread.start()

    def stop() -> None:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)

    return f"http://{host}:{actual_port}", stop


# -- billing service (system A) ----------------------------------------------


class BillingStore:
    def __init__(self, db_path: Path, export_dir: Path, workbook_dir: Path) -> None:
        self.db_path = db_path
        self.export_path = export_dir / EXPORT_NAME
        self.results_path = workbook_dir / RESULTS_NAME
        self._lock = threading.Lock()
        self.drop_writeback = False

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.db_path), timeout=5.0)

    def reset(self, *, order_ids: list[str], faults: list[str]) -> None:
        with self._lock:
            self.drop_writeback = "drop_writeback" in faults
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    DROP TABLE IF EXISTS billed_orders;
                    DROP TABLE IF EXISTS banner;
                    CREATE TABLE billed_orders (
                        order_id TEXT PRIMARY KEY,
                        customer TEXT,
                        amount_billed TEXT,
                        period TEXT
                    );
                    CREATE TABLE banner (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        order_id TEXT,
                        event TEXT,
                        detail TEXT
                    );
                    """
                )
                selected = [s for s in ORDER_SEEDS if s[0] in order_ids]
                conn.executemany(
                    "INSERT INTO billed_orders "
                    "(order_id, customer, amount_billed, period) VALUES (?, ?, ?, ?)",
                    [(s[0], s[1], s[2], s[4]) for s in selected],
                )
                conn.commit()
            finally:
                conn.close()
            # Drop the nightly export spreadsheet into the shared folder.
            self.export_path.parent.mkdir(parents=True, exist_ok=True)
            with self.export_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["order_id", "customer", "amount_billed", "period"])
                for seed in selected:
                    writer.writerow([seed[0], seed[1], seed[2], seed[4]])
            # Fresh results workbook folder (no results sheet yet).
            self.results_path.parent.mkdir(parents=True, exist_ok=True)
            if self.results_path.exists():
                self.results_path.unlink()

    def writeback(self, body: dict[str, Any]) -> tuple[int, str]:
        row = {name: str(body.get(name, "")) for name in RESULTS_FIELDS}
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO banner (order_id, event, detail) VALUES (?, ?, ?)",
                    (row["order_id"], "writeback_recorded", row["disposition"]),
                )
                conn.commit()
            finally:
                conn.close()
            if self.drop_writeback:
                # Acknowledged, painted, and never persisted.
                return 200, "recorded"
            exists = self.results_path.exists()
            with self.results_path.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(RESULTS_FIELDS))
                if not exists:
                    writer.writeheader()
                writer.writerow(row)
            return 200, "recorded"

    def read_banner(self) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            conn.row_factory = sqlite3.Row
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT id, order_id, event, detail FROM banner"
                ).fetchall()
            ]
        finally:
            conn.close()


def serve_billing(db_path: Path, export_dir: Path, workbook_dir: Path) -> BillingHandle:
    store = BillingStore(db_path, export_dir, workbook_dir)

    def route(
        method: str, path: str, body: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        if method == "GET":
            if path == "/api/ui/banner":
                return 200, {"records": store.read_banner()}
            return 404, {"error": "not found"}
        if path == "/api/reset":
            store.reset(
                order_ids=[str(o) for o in body.get("order_ids", [])],
                faults=[str(f) for f in body.get("faults", [])],
            )
            return 200, {"ok": True}
        if path == "/api/workbook/writeback":
            status, detail = store.writeback(body)
            return status, {"ok": 200 <= status < 300, "detail": detail}
        return 404, {"error": "not found"}

    base_url, stop = _serve("o2c-billing", route)
    return BillingHandle(
        base_url=base_url,
        db_path=db_path,
        export_path=store.export_path,
        results_path=store.results_path,
        stop=stop,
    )


# -- ledger service (system B) ------------------------------------------------


class LedgerStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self.stale_snapshot = False

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.db_path), timeout=5.0)

    def reset(self, *, order_ids: list[str], faults: list[str]) -> None:
        with self._lock:
            self.stale_snapshot = "stale_snapshot" in faults
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    DROP TABLE IF EXISTS ledger_entries;
                    DROP TABLE IF EXISTS adjustments;
                    DROP TABLE IF EXISTS banner;
                    CREATE TABLE ledger_entries (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        order_id TEXT,
                        customer TEXT,
                        amount_posted TEXT,
                        status TEXT
                    );
                    CREATE TABLE adjustments (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        order_id TEXT,
                        delta TEXT,
                        reason TEXT,
                        status TEXT
                    );
                    CREATE TABLE banner (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        order_id TEXT,
                        event TEXT,
                        detail TEXT
                    );
                    """
                )
                for seed in ORDER_SEEDS:
                    order_id, customer, _billed, posted, _period = seed
                    if order_id not in order_ids or posted is None:
                        continue
                    conn.execute(
                        "INSERT INTO ledger_entries "
                        "(order_id, customer, amount_posted, status) "
                        "VALUES (?, ?, ?, 'open')",
                        (order_id, customer, posted),
                    )
                    if order_id == DUPLICATE_ORDER:
                        # A second, conflicting posting for the same order.
                        conn.execute(
                            "INSERT INTO ledger_entries "
                            "(order_id, customer, amount_posted, status) "
                            "VALUES (?, ?, ?, 'open')",
                            (order_id, customer, "305.00"),
                        )
                conn.commit()
            finally:
                conn.close()

    def _banner(
        self, conn: sqlite3.Connection, order_id: str, event: str, detail: str
    ) -> None:
        conn.execute(
            "INSERT INTO banner (order_id, event, detail) VALUES (?, ?, ?)",
            (order_id, event, detail),
        )

    def ui_adjustment_new(self, body: dict[str, Any]) -> tuple[int, str]:
        order_id = str(body.get("order_id", ""))
        delta = str(body.get("delta", ""))
        expected_prior = str(body.get("expected_prior", ""))
        expected_new = str(body.get("expected_new", ""))
        reason = str(body.get("reason", ""))
        with self._lock:
            conn = self._connect()
            try:
                # Optimistic UI acknowledgement first, like a real form.
                self._banner(conn, order_id, "adjustment_entered", delta)
                conn.row_factory = sqlite3.Row
                entries = conn.execute(
                    "SELECT * FROM ledger_entries WHERE order_id = ?", (order_id,)
                ).fetchall()
                if not entries:
                    conn.commit()
                    return 404, f"order {order_id} not found in the ledger"
                if len(entries) > 1:
                    conn.commit()
                    return (
                        409,
                        f"order {order_id} has {len(entries)} ledger entries "
                        "(ambiguous duplicate); refusing to adjust",
                    )
                if str(entries[0]["amount_posted"]) != expected_prior:
                    conn.commit()
                    return (
                        409,
                        "ledger amount changed since the reconciliation "
                        "snapshot (stale worklist); refusing to adjust",
                    )
                conn.execute(
                    "INSERT INTO adjustments (order_id, delta, reason, status) "
                    "VALUES (?, ?, ?, 'applied')",
                    (order_id, delta, reason),
                )
                conn.execute(
                    "UPDATE ledger_entries SET amount_posted = ?, "
                    "status = 'adjusted' WHERE order_id = ?",
                    (expected_new, order_id),
                )
                conn.commit()
                return 200, "adjusted"
            finally:
                conn.close()

    def api_mark_reconciled(self, body: dict[str, Any]) -> tuple[int, str]:
        order_id = str(body.get("order_id", ""))
        amount = str(body.get("amount", ""))
        with self._lock:
            conn = self._connect()
            try:
                self._banner(conn, order_id, "marked_reconciled", amount)
                conn.row_factory = sqlite3.Row
                entries = conn.execute(
                    "SELECT * FROM ledger_entries WHERE order_id = ?", (order_id,)
                ).fetchall()
                if len(entries) != 1:
                    conn.commit()
                    return 409, f"order {order_id} is missing or ambiguous"
                if str(entries[0]["amount_posted"]) != amount:
                    conn.commit()
                    return 409, "amounts do not agree; cannot mark reconciled"
                conn.execute(
                    "UPDATE ledger_entries SET status = 'reconciled' "
                    "WHERE order_id = ?",
                    (order_id,),
                )
                conn.commit()
                return 200, "reconciled"
            finally:
                conn.close()

    def read_entries(self) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            conn.row_factory = sqlite3.Row
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT id, order_id, customer, amount_posted, status "
                    "FROM ledger_entries"
                ).fetchall()
            ]
        finally:
            conn.close()
        if self.stale_snapshot:
            for row in rows:
                if row["order_id"] == STALE_ORDER:
                    # The lagged replica the reconciliation snapshot read.
                    row["amount_posted"] = STALE_REPORTED_AMOUNT
        return rows

    def read_banner(self) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            conn.row_factory = sqlite3.Row
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT id, order_id, event, detail FROM banner"
                ).fetchall()
            ]
        finally:
            conn.close()


def serve_ledger(db_path: Path) -> LedgerHandle:
    store = LedgerStore(db_path)

    def route(
        method: str, path: str, body: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        if method == "GET":
            if path == "/api/ledger":
                return 200, {"records": store.read_entries()}
            if path == "/api/ui/banner":
                return 200, {"records": store.read_banner()}
            return 404, {"error": "not found"}
        if path == "/api/reset":
            store.reset(
                order_ids=[str(o) for o in body.get("order_ids", [])],
                faults=[str(f) for f in body.get("faults", [])],
            )
            return 200, {"ok": True}
        if path == "/api/adjustment/new":
            # The API-vs-UI ladder: adjustments are UI-only in this system.
            return 405, {"error": "adjustments are only available through the UI"}
        handlers = {
            "/ui/adjustment/new": store.ui_adjustment_new,
            "/api/reconcile/mark": store.api_mark_reconciled,
        }
        handler = handlers.get(path)
        if handler is None:
            return 404, {"error": "not found"}
        status, detail = handler(body)
        return status, {"ok": 200 <= status < 300, "detail": detail}

    base_url, stop = _serve("o2c-ledger", route)
    return LedgerHandle(base_url=base_url, db_path=db_path, stop=stop)

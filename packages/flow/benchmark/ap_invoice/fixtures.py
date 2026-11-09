"""Deterministic multi-application fixture for the AP invoice benchmark.

Two separate local applications, each with its own process-independent system
of record, plus a document store:

1. **ERP service** (accounts-payable ledger): an on-disk SQLite database
   behind a threaded HTTP server. It exposes TWO write surfaces on purpose:

   - ``/ui/...``  -- the "UI gateway": the only surface that accepts invoice
     entry and approval (the fixture's stand-in for screens that have no API;
     ``POST /api/invoice/new`` answers 405). Its handlers paint an optimistic
     UI banner BEFORE/REGARDLESS of what actually persisted, exactly like a
     real thick-client form.
   - ``/api/...`` -- the real REST surface for document attachment, 3-way
     match, discounts, payments, exceptions, and batch completion.

   Fault hooks are injected at the persistence boundary (never visible in the
   HTTP status or the banner) so the benchmark can measure what each oracle
   arm actually catches.

2. **Mailer service**: a maildir-backed mail gateway. The INBOX maildir is
   seeded with synthetic vendor request emails (RFC822, each with a PDF
   invoice attachment); ``POST /api/send`` delivers a confirmation message
   into the OUTBOX maildir. Out-of-band email verification reads the OUTBOX
   maildir directly from disk.

All data is synthetic (fake vendors, fake amounts). Localhost only. Zero model
calls.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from benchmark.multiapp_common import init_maildir, make_pdf, write_maildir_message

#: The seeded ADJACENT record every scenario carries: a draft invoice sitting
#: one row away from the target in the AP grid. The collateral fault corrupts
#: it while the target write succeeds and the banner still paints success.
ADJACENT_INVOICE = "INV-2090"
ADJACENT_VENDOR = "V-100"
ADJACENT_AMOUNT = "450.00"

#: The pre-existing posted invoice the ambiguous-duplicate scenario re-presents.
DUPLICATE_INVOICE = "INV-1201"

# Immutable source fixtures.  The workflow intake parser and the direct
# persisted-state adjudicator consume these independently; the adjudicator
# never trusts the worklist produced by the system under test.
INVOICE_SOURCE_SEEDS: dict[str, tuple[str, str, str, str, str]] = {
    "INV-1001": ("V-100", "Acme Supply Co", "PO-501", "1200.00", "2/10 NET 30"),
    "INV-1002": ("V-200", "Beta Industrial", "PO-502", "860.00", "NET 30"),
    "INV-1003": ("V-300", "Gamma Logistics", "PO-503", "415.25", "NET 30"),
    "INV-1004": ("V-100", "Acme Supply Co", "PO-504", "99.90", "2/10 NET 30"),
    "INV-1005": ("V-200", "Beta Industrial", "PO-505", "729.00", "NET 30"),
    "INV-1101": ("V-100", "Acme Supply Co", "PO-599", "55.00", "NET 30"),
    "INV-1201": ("V-200", "Beta Industrial", "PO-505", "729.00", "NET 30"),
    "INV-1301": ("V-100", "Acme Supply Co", "PO-506", "320.00", "NET 30"),
    "INV-1401": ("V-300", "Gamma Logistics", "PO-507", "212.75", "NET 30"),
}

SCENARIO_INVOICE_IDS: dict[str, tuple[str, ...]] = {
    "healthy": ("INV-1001", "INV-1002", "INV-1003", "INV-1004", "INV-1005"),
    "missing_po": ("INV-1101",),
    "duplicate_invoice": ("INV-1201",),
    "collateral_approve": ("INV-1301",),
    "payment_confirm_outage": ("INV-1401",),
}


def invoice_source_pdf(invoice_id: str) -> bytes:
    """The immutable PDF bytes delivered to the intake pipeline."""
    vendor_id, vendor_name, po_number, amount, terms = INVOICE_SOURCE_SEEDS[invoice_id]
    return make_pdf(
        [
            f"INVOICE: {invoice_id}",
            f"VENDOR: {vendor_id}",
            f"VENDOR-NAME: {vendor_name}",
            f"PO: {po_number}",
            f"AMOUNT: {amount}",
            f"TERMS: {terms}",
            "CURRENCY: USD",
        ]
    )


VENDORS = (
    ("V-100", "Acme Supply Co"),
    ("V-200", "Beta Industrial"),
    ("V-300", "Gamma Logistics"),
)

#: (po_number, vendor_id, item, qty, unit_price, amount) -- what Purchasing
#: ordered and Receiving booked; the 3-way match compares invoices against it.
PURCHASE_ORDERS = (
    ("PO-501", "V-100", "safety gloves", "40", "30.00", "1200.00"),
    ("PO-502", "V-200", "bearing sets", "20", "43.00", "860.00"),
    ("PO-503", "V-300", "freight", "1", "380.00", "380.00"),
    ("PO-504", "V-100", "label rolls", "9", "11.10", "99.90"),
    ("PO-505", "V-200", "coolant drums", "6", "121.50", "729.00"),
    ("PO-506", "V-100", "hex fasteners", "50", "6.40", "320.00"),
    ("PO-507", "V-300", "freight", "1", "212.75", "212.75"),
)


@dataclass(frozen=True)
class ErpHandle:
    base_url: str
    db_path: Path
    stop: Callable[[], None]


@dataclass(frozen=True)
class MailerHandle:
    base_url: str
    inbox: Path
    outbox: Path
    stop: Callable[[], None]


@dataclass
class _Faults:
    """Persistence-boundary fault switches for one scenario."""

    collateral_adjacent_on_approve: bool = False
    payments_read_down_after_write: bool = False
    payments_read_down: bool = field(default=False, compare=False)


def _init_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            DROP TABLE IF EXISTS vendors;
            DROP TABLE IF EXISTS purchase_orders;
            DROP TABLE IF EXISTS receipts;
            DROP TABLE IF EXISTS invoices;
            DROP TABLE IF EXISTS payments;
            DROP TABLE IF EXISTS ap_exceptions;
            DROP TABLE IF EXISTS batches;
            DROP TABLE IF EXISTS banner;
            CREATE TABLE vendors (vendor_id TEXT PRIMARY KEY, name TEXT);
            CREATE TABLE purchase_orders (
                po_number TEXT PRIMARY KEY,
                vendor_id TEXT,
                item TEXT,
                qty TEXT,
                unit_price TEXT,
                amount TEXT,
                status TEXT
            );
            CREATE TABLE receipts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                po_number TEXT,
                qty_received TEXT
            );
            CREATE TABLE invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id TEXT,
                vendor_id TEXT,
                po_number TEXT,
                amount TEXT,
                doc_sha256 TEXT,
                status TEXT,
                discount_applied TEXT,
                amount_payable TEXT
            );
            CREATE TABLE payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id TEXT,
                amount TEXT,
                status TEXT
            );
            CREATE TABLE ap_exceptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id TEXT,
                reason TEXT
            );
            CREATE TABLE batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT,
                processed TEXT
            );
            CREATE TABLE banner (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id TEXT,
                event TEXT,
                detail TEXT
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


class ErpStore:
    """Write-side wrapper over the ERP database, with fault injection.

    Fresh connection per operation; writes serialized under a lock. Faults are
    applied at the persistence boundary AFTER the handler has decided its HTTP
    status and painted the banner, so a fault is invisible to a screen-echo
    oracle by construction.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.faults = _Faults()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.path), timeout=5.0)

    # -- lifecycle -----------------------------------------------------------

    def reset(
        self, *, faults: Optional[list[str]] = None, seed_duplicate: bool = False
    ) -> None:
        with self._lock:
            _init_db(self.path)
            self.faults = _Faults(
                collateral_adjacent_on_approve="collateral_adjacent_on_approve"
                in (faults or []),
                payments_read_down_after_write="payments_read_down_after_write"
                in (faults or []),
            )
            conn = self._connect()
            try:
                conn.executemany(
                    "INSERT INTO vendors (vendor_id, name) VALUES (?, ?)", VENDORS
                )
                conn.executemany(
                    "INSERT INTO purchase_orders "
                    "(po_number, vendor_id, item, qty, unit_price, amount, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'open')",
                    PURCHASE_ORDERS,
                )
                conn.executemany(
                    "INSERT INTO receipts (po_number, qty_received) VALUES (?, ?)",
                    [(po[0], po[3]) for po in PURCHASE_ORDERS],
                )
                # The always-present adjacent draft the collateral fault targets.
                conn.execute(
                    "INSERT INTO invoices (invoice_id, vendor_id, po_number, "
                    "amount, doc_sha256, status, discount_applied, amount_payable) "
                    "VALUES (?, ?, ?, ?, '', 'draft', 'none', '')",
                    (ADJACENT_INVOICE, ADJACENT_VENDOR, "PO-506", ADJACENT_AMOUNT),
                )
                if seed_duplicate:
                    conn.execute(
                        "INSERT INTO invoices (invoice_id, vendor_id, po_number, "
                        "amount, doc_sha256, status, discount_applied, "
                        "amount_payable) "
                        "VALUES (?, 'V-200', 'PO-505', '729.00', '', 'posted', "
                        "'none', '729.00')",
                        (DUPLICATE_INVOICE,),
                    )
                conn.commit()
            finally:
                conn.close()

    # -- helpers -------------------------------------------------------------

    def _banner(
        self, conn: sqlite3.Connection, invoice_id: str, event: str, detail: str
    ) -> None:
        conn.execute(
            "INSERT INTO banner (invoice_id, event, detail) VALUES (?, ?, ?)",
            (invoice_id, event, detail),
        )

    def _invoice(
        self, conn: sqlite3.Connection, invoice_id: str
    ) -> Optional[sqlite3.Row]:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM invoices WHERE invoice_id = ?", (invoice_id,)
        ).fetchone()

    # -- UI gateway writes ---------------------------------------------------

    def ui_invoice_new(self, body: dict[str, Any]) -> tuple[int, str]:
        invoice_id = str(body.get("invoice_id", ""))
        vendor_id = str(body.get("vendor_id", ""))
        po_number = str(body.get("po_number", ""))
        amount = str(body.get("amount", ""))
        with self._lock:
            conn = self._connect()
            try:
                # The optimistic UI paints the acknowledgement FIRST (like a
                # form that shows "Saved" as soon as the button is pressed).
                self._banner(conn, invoice_id, "invoice_created", amount)
                po = conn.execute(
                    "SELECT po_number FROM purchase_orders WHERE po_number = ?",
                    (po_number,),
                ).fetchone()
                if po is None:
                    conn.commit()
                    return 404, f"purchase order {po_number} not found"
                existing = conn.execute(
                    "SELECT COUNT(*) FROM invoices WHERE invoice_id = ?",
                    (invoice_id,),
                ).fetchone()[0]
                if existing:
                    conn.commit()
                    return (
                        409,
                        f"invoice {invoice_id} already exists (ambiguous "
                        "duplicate: possible resubmission)",
                    )
                conn.execute(
                    "INSERT INTO invoices (invoice_id, vendor_id, po_number, "
                    "amount, doc_sha256, status, discount_applied, amount_payable) "
                    "VALUES (?, ?, ?, ?, '', 'draft', 'none', '')",
                    (invoice_id, vendor_id, po_number, amount),
                )
                conn.commit()
                return 200, "created"
            finally:
                conn.close()

    def ui_invoice_approve(self, body: dict[str, Any]) -> tuple[int, str]:
        invoice_id = str(body.get("invoice_id", ""))
        amount_payable = str(body.get("amount_payable", ""))
        with self._lock:
            conn = self._connect()
            try:
                self._banner(conn, invoice_id, "invoice_approved", amount_payable)
                row = self._invoice(conn, invoice_id)
                if row is None:
                    conn.commit()
                    return 404, f"invoice {invoice_id} not found"
                if row["status"] not in ("matched", "discounted"):
                    conn.commit()
                    return 409, f"invoice {invoice_id} is not matched"
                expected = (
                    row["amount_payable"]
                    if row["discount_applied"] not in ("", "none")
                    else row["amount"]
                )
                if amount_payable != expected:
                    conn.commit()
                    return 409, "amount payable does not match the invoice terms"
                conn.execute(
                    "UPDATE invoices SET status = 'approved', amount_payable = ? "
                    "WHERE invoice_id = ?",
                    (amount_payable, invoice_id),
                )
                if self.faults.collateral_adjacent_on_approve:
                    # The grid-row trap: the save handler ALSO commits the
                    # adjacent row (a real thick-client class of bug: shared
                    # dirty-row state). Status stays 200; the banner already
                    # painted success for the target only.
                    conn.execute(
                        "UPDATE invoices SET status = 'approved', "
                        "amount_payable = '999.00' WHERE invoice_id = ?",
                        (ADJACENT_INVOICE,),
                    )
                conn.commit()
                return 200, "approved"
            finally:
                conn.close()

    # -- API writes ----------------------------------------------------------

    def api_attach_document(self, body: dict[str, Any]) -> tuple[int, str]:
        invoice_id = str(body.get("invoice_id", ""))
        doc_sha256 = str(body.get("doc_sha256", ""))
        with self._lock:
            conn = self._connect()
            try:
                self._banner(conn, invoice_id, "document_attached", doc_sha256[:12])
                row = self._invoice(conn, invoice_id)
                if row is None:
                    conn.commit()
                    return 404, f"invoice {invoice_id} not found"
                conn.execute(
                    "UPDATE invoices SET doc_sha256 = ? WHERE invoice_id = ?",
                    (doc_sha256, invoice_id),
                )
                conn.commit()
                return 200, "attached"
            finally:
                conn.close()

    def api_match(self, body: dict[str, Any]) -> tuple[int, str]:
        invoice_id = str(body.get("invoice_id", ""))
        with self._lock:
            conn = self._connect()
            try:
                self._banner(conn, invoice_id, "match_run", "")
                row = self._invoice(conn, invoice_id)
                if row is None:
                    conn.commit()
                    return 404, f"invoice {invoice_id} not found"
                conn.row_factory = sqlite3.Row
                po = conn.execute(
                    "SELECT * FROM purchase_orders WHERE po_number = ?",
                    (row["po_number"],),
                ).fetchone()
                received = conn.execute(
                    "SELECT COALESCE(SUM(CAST(qty_received AS REAL)), 0) "
                    "FROM receipts WHERE po_number = ?",
                    (row["po_number"],),
                ).fetchone()[0]
                status = "price_mismatch"
                if (
                    po is not None
                    and po["amount"] == row["amount"]
                    and float(po["qty"]) <= float(received)
                ):
                    status = "matched"
                conn.execute(
                    "UPDATE invoices SET status = ? WHERE invoice_id = ?",
                    (status, invoice_id),
                )
                conn.commit()
                return 200, status
            finally:
                conn.close()

    def api_discount(self, body: dict[str, Any]) -> tuple[int, str]:
        invoice_id = str(body.get("invoice_id", ""))
        discount_applied = str(body.get("discount_applied", ""))
        amount_payable = str(body.get("amount_payable", ""))
        with self._lock:
            conn = self._connect()
            try:
                self._banner(conn, invoice_id, "discount_applied", discount_applied)
                row = self._invoice(conn, invoice_id)
                if row is None:
                    conn.commit()
                    return 404, f"invoice {invoice_id} not found"
                if row["status"] != "matched":
                    conn.commit()
                    return 409, f"invoice {invoice_id} is not matched"
                conn.execute(
                    "UPDATE invoices SET status = 'discounted', "
                    "discount_applied = ?, amount_payable = ? WHERE invoice_id = ?",
                    (discount_applied, amount_payable, invoice_id),
                )
                conn.commit()
                return 200, "discounted"
            finally:
                conn.close()

    def api_payment(self, body: dict[str, Any]) -> tuple[int, str]:
        invoice_id = str(body.get("invoice_id", ""))
        amount = str(body.get("amount", ""))
        with self._lock:
            conn = self._connect()
            try:
                self._banner(conn, invoice_id, "payment_scheduled", amount)
                row = self._invoice(conn, invoice_id)
                if row is None:
                    conn.commit()
                    return 404, f"invoice {invoice_id} not found"
                if row["status"] != "approved":
                    conn.commit()
                    return 409, f"invoice {invoice_id} is not approved"
                conn.execute(
                    "INSERT INTO payments (invoice_id, amount, status) "
                    "VALUES (?, ?, 'scheduled')",
                    (invoice_id, amount),
                )
                conn.commit()
            finally:
                conn.close()
        if self.faults.payments_read_down_after_write:
            # The payment COMMITTED, then the payment-status read surface went
            # down: the uncertain-delivery class. The client cannot distinguish
            # "landed" from "lost" by reading.
            self.faults.payments_read_down = True
        return 200, "scheduled"

    def api_exception(self, body: dict[str, Any]) -> tuple[int, str]:
        invoice_id = str(body.get("invoice_id", ""))
        reason = str(body.get("reason", ""))
        with self._lock:
            conn = self._connect()
            try:
                self._banner(conn, invoice_id, "exception_routed", reason)
                row = self._invoice(conn, invoice_id)
                if row is None:
                    conn.commit()
                    return 404, f"invoice {invoice_id} not found"
                conn.execute(
                    "INSERT INTO ap_exceptions (invoice_id, reason) VALUES (?, ?)",
                    (invoice_id, reason),
                )
                conn.execute(
                    "UPDATE invoices SET status = 'held' WHERE invoice_id = ?",
                    (invoice_id,),
                )
                conn.commit()
                return 200, "held"
            finally:
                conn.close()

    def api_batch_complete(self, body: dict[str, Any]) -> tuple[int, str]:
        batch_id = str(body.get("batch_id", ""))
        processed = str(body.get("processed", ""))
        with self._lock:
            conn = self._connect()
            try:
                self._banner(conn, batch_id, "batch_completed", processed)
                conn.execute(
                    "INSERT INTO batches (batch_id, processed) VALUES (?, ?)",
                    (batch_id, processed),
                )
                conn.commit()
                return 200, "completed"
            finally:
                conn.close()

    # -- reads ---------------------------------------------------------------

    def read_table(self, query: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(query).fetchall()]
        finally:
            conn.close()


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


def serve_erp(db_path: Path, *, host: str = "127.0.0.1", port: int = 0) -> ErpHandle:
    """Initialize and serve the ERP system of record on a loopback port."""
    _init_db(db_path)
    store = ErpStore(db_path)

    def route(
        method: str, path: str, body: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        if method == "GET":
            if path == "/api/payments":
                if store.faults.payments_read_down:
                    return 500, {"error": "payment status service unavailable"}
                return 200, {
                    "records": store.read_table(
                        "SELECT id, invoice_id, amount, status FROM payments"
                    )
                }
            if path == "/api/ui/banner":
                return 200, {
                    "records": store.read_table(
                        "SELECT id, invoice_id, event, detail FROM banner"
                    )
                }
            if path == "/api/purchase_orders":
                return 200, {
                    "records": store.read_table(
                        "SELECT po_number, vendor_id, item, qty, unit_price, "
                        "amount, status FROM purchase_orders"
                    )
                }
            return 404, {"error": "not found"}
        if path == "/api/reset":
            store.reset(
                faults=[str(f) for f in body.get("faults", [])],
                seed_duplicate=bool(body.get("seed_duplicate")),
            )
            return 200, {"ok": True}
        if path == "/api/invoice/new":
            # The API-vs-UI ladder: this system has NO invoice-entry API.
            return 405, {"error": "invoice entry is only available through the UI"}
        handlers = {
            "/ui/invoice/new": store.ui_invoice_new,
            "/ui/invoice/approve": store.ui_invoice_approve,
            "/api/invoice/document": store.api_attach_document,
            "/api/invoice/match": store.api_match,
            "/api/invoice/discount": store.api_discount,
            "/api/payment": store.api_payment,
            "/api/exception": store.api_exception,
            "/api/batch/complete": store.api_batch_complete,
        }
        handler = handlers.get(path)
        if handler is None:
            return 404, {"error": "not found"}
        status, detail = handler(body)
        return status, {"ok": 200 <= status < 300, "detail": detail}

    httpd = ThreadingHTTPServer((host, port), _json_handler(route))
    actual_port = httpd.server_address[1]
    thread = threading.Thread(
        target=httpd.serve_forever, name="ap-invoice-erp", daemon=True
    )
    thread.start()

    def stop() -> None:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)

    return ErpHandle(
        base_url=f"http://{host}:{actual_port}", db_path=db_path, stop=stop
    )


def serve_mailer(
    mail_root: Path, *, host: str = "127.0.0.1", port: int = 0
) -> MailerHandle:
    """Serve the maildir-backed mail gateway on a loopback port."""
    inbox = init_maildir(mail_root / "inbox")
    outbox = init_maildir(mail_root / "outbox")
    banner: list[dict[str, Any]] = []
    lock = threading.Lock()

    def route(
        method: str, path: str, body: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        if method == "GET":
            if path == "/api/ui/banner":
                with lock:
                    return 200, {"records": list(banner)}
            return 404, {"error": "not found"}
        if path == "/api/reset":
            with lock:
                banner.clear()
                for sub in ("new", "cur", "tmp"):
                    for item in (outbox / sub).glob("*"):
                        item.unlink()
            return 200, {"ok": True}
        if path == "/api/send":
            name = str(body.get("name", ""))
            invoice_id = str(body.get("invoice_id", ""))
            to_addr = str(body.get("to", ""))
            subject = str(body.get("subject", ""))
            text = str(body.get("body", ""))
            if not name or not to_addr:
                return 400, {"error": "name and to are required"}
            with lock:
                banner.append(
                    {
                        "id": len(banner) + 1,
                        "invoice_id": invoice_id,
                        "event": "mail_sent",
                        "detail": subject,
                    }
                )
                message = (
                    f"From: ap-bot@example-corp.test\r\nTo: {to_addr}\r\n"
                    f"Subject: {subject}\r\n"
                    f"X-OpenAdapt-Invoice: {invoice_id}\r\n\r\n{text}\r\n"
                ).encode("utf-8")
                write_maildir_message(outbox, name, message)
            return 200, {"ok": True}
        return 404, {"error": "not found"}

    httpd = ThreadingHTTPServer((host, port), _json_handler(route))
    actual_port = httpd.server_address[1]
    thread = threading.Thread(
        target=httpd.serve_forever, name="ap-invoice-mailer", daemon=True
    )
    thread.start()

    def stop() -> None:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)

    return MailerHandle(
        base_url=f"http://{host}:{actual_port}", inbox=inbox, outbox=outbox, stop=stop
    )

"""Transactional fault-injection server for the MockLoan demo app.

The bundled MockLoan app (``mockloan/static``) is a purely client-side SPA: an
"authorize" mutates an in-page JavaScript object, so the UI *is* the source of
truth and there is nothing to verify a write against. Real record systems have
a persistence boundary - a loan-servicing core a disbursement must actually
reach - and the dangerous failures for *consequential writes* (moving money to
a borrower) live at that boundary: partial saves, duplicate submissions,
commit-then-timeout, optimistic-UI success the core later rejects, session
expiry, and lost updates from a concurrent modification (a fraud hold).

This server adds that boundary WITHOUT changing the normal benchmark. It serves
the exact same static files (so recorded template crops and mined postconditions
are unchanged) and adds a small JSON API that the app talks to *only* when the
page is loaded with ``?fault=<mode>`` (a flag-gated hook that mirrors the
existing ``?drift=`` hooks). With no ``?fault`` query the app never calls the
API and behaves byte-for-byte as before.

The server keeps an isolated temporary SQLite ledger. Product-facing verifier
arms read it through the REST API, while the benchmark judge opens the SQLite
file read-only and independently observes what actually persisted. Neither path
trusts the replay's vision-based self-report.

Fault modes (selected by ``?fault=`` on the write POST, forwarded by the app):

- ``ok``          -- control: the disbursement is booked normally.
- ``partial``     -- the row is booked but the funding memo is dropped
                     (core only saved some fields). UI still says "authorized".
- ``optimistic``  -- the core REJECTS the write; the app already painted a
                     success banner optimistically, so the screen lies.
- ``timeout``     -- the core BOOKS the row, then hangs past the client
                     timeout; the app sees an error though the money moved.
- ``session``     -- the write returns 401 (session expired); nothing is booked
                     and the app bounces to the login screen.
- ``stale``       -- last-write-wins over a loan a concurrent officer changed
                     between record and replay: the other change (a fraud hold)
                     is lost.
- ``duplicate`` / ``double`` -- the write is accepted every time it arrives;
                     a double-submit / double-delivered click books TWO
                     disbursements (the borrower is paid twice).
- ``idempotent``  -- like ``duplicate`` but the app sends an idempotency key
                     and the core de-duplicates on it (the RECOMMENDED fix).
- ``collateral``  -- the CORRECT disbursement to the target loan is booked (the
                     disbursements ledger looks perfect), but a spurious
                     money-movement (an unauthorized servicing fee referencing
                     the same loan and funding memo) is ALSO written to a
                     SEPARATE fees / general-ledger surface. This is the lending
                     analog of the clinical ``collateral_unaudited`` fault (a
                     correct encounter plus a stray billing row). A
                     disbursements-only oracle certifies the write; only a
                     COMPLETE read path spanning both ledgers sees the extra row.

The ledger records two surfaces: ``disbursements`` (the money paid out to the
borrower) and ``fees`` (a general-ledger / charges surface). A record carries a
``surface`` field (default ``"disbursements"``). Two read paths expose them:

- ``GET /api/disbursements`` -- the SINGLE-surface read path: only the
  disbursements ledger. A single out-of-band oracle over this path is blind to a
  fees-surface write (the lending analog of the clinical single-surface REST
  oracle over encounters only).
- ``GET /api/db`` -- the COMPLETE read path: every mutable surface. The full
  read path an effect oracle needs to reach 0 residual.

A ``DELETE /api/disbursement/<id>`` route (additive; never used by a ``?fault=``
path) lets an EffectVerifier compensation hook reconcile a detected duplicate
against this same system of record.

All data is fake. Nothing here touches the network beyond localhost.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

STATIC_DIR = Path(__file__).resolve().parent / "static"

# How long ``timeout`` mode hangs after booking, in seconds. The app's
# client-side abort fires well before this, so the app sees a failed write
# even though the row landed. Kept short so the study runs quickly.
TIMEOUT_HANG_S = 3.0


class LedgerDB:
    """Thread-safe SQLite store of synthetic disbursement writes.

    A record is ``{"id", "loan_id", "product", "amount", "memo", "source",
    "key", "surface"}``. ``source`` is ``"replay"`` for writes made during a run
    and ``"other"`` for rows seeded to model a concurrent actor. ``surface`` is
    ``"disbursements"`` (money paid to the borrower) or ``"fees"`` (a
    general-ledger / charges surface); a single-surface oracle reads only the
    former. The store is deliberately dumb: it records exactly what the fault
    path did, so the study can judge the replay against effects rather than
    against the screen.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tempdir = tempfile.TemporaryDirectory(prefix="openadapt-mockloan-")
        self.database_path = Path(self._tempdir.name) / "ledger.sqlite3"
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    loan_id TEXT NOT NULL,
                    product TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    memo TEXT NOT NULL,
                    source TEXT NOT NULL,
                    record_key TEXT,
                    surface TEXT NOT NULL
                );
                CREATE UNIQUE INDEX records_record_key_unique
                    ON records(record_key)
                    WHERE record_key IS NOT NULL;
                CREATE TABLE metadata (
                    name TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                );
                INSERT INTO metadata(name, value) VALUES ('rejected_writes', 0);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.database_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _record(row: sqlite3.Row) -> dict:
        record = dict(row)
        record["key"] = record.pop("record_key")
        return record

    @property
    def rejected_writes(self) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM metadata WHERE name = 'rejected_writes'"
            ).fetchone()
            return int(row[0]) if row is not None else 0

    def reset(self, *, seed_concurrent: bool = False) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM records")
            conn.execute("DELETE FROM sqlite_sequence WHERE name = 'records'")
            conn.execute("UPDATE metadata SET value = 0 WHERE name = 'rejected_writes'")
            if seed_concurrent:
                # A concurrent loan officer placed an URGENT fraud hold /
                # adjustment on the same loan between record and replay. A blind
                # last-write-wins authorize (``stale`` mode) will lose it.
                conn.execute(
                    """
                    INSERT INTO records(
                        loan_id, product, amount, memo, source, record_key, surface
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "L1001",
                        "Hold",
                        "0",
                        "URGENT: fraud hold placed - do not disburse",
                        "other",
                        None,
                        "disbursements",
                    ),
                )

    def add(
        self,
        loan_id: str,
        product: str,
        amount: str,
        memo: str,
        *,
        key: Optional[str] = None,
        overwrite_loan: bool = False,
        surface: str = "disbursements",
    ) -> dict:
        with self._lock, self._connect() as conn:
            if key is not None:
                existing = conn.execute(
                    """
                    SELECT id, loan_id, product, amount, memo, source,
                           record_key, surface
                    FROM records WHERE record_key = ?
                    """,
                    (key,),
                ).fetchone()
                if existing is not None:
                    return self._record(existing)
            if overwrite_loan:
                # Last-write-wins: drop every existing row for this loan on the
                # SAME surface (including a concurrent officer's hold) before
                # writing ours.
                conn.execute(
                    "DELETE FROM records WHERE loan_id = ? AND surface = ?",
                    (loan_id, surface),
                )
            cursor = conn.execute(
                """
                INSERT INTO records(
                    loan_id, product, amount, memo, source, record_key, surface
                ) VALUES (?, ?, ?, ?, 'replay', ?, ?)
                """,
                (loan_id, product, amount, memo, key, surface),
            )
            row = conn.execute(
                """
                SELECT id, loan_id, product, amount, memo, source,
                       record_key, surface
                FROM records WHERE id = ?
                """,
                (cursor.lastrowid,),
            ).fetchone()
            assert row is not None
            return self._record(row)

    def note_rejected(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE metadata SET value = value + 1
                WHERE name = 'rejected_writes'
                """
            )

    def delete(self, record_id: int) -> bool:
        """Delete the record with ``record_id``. Returns True iff one was
        removed.

        Additive endpoint used ONLY by an EffectVerifier compensation hook to
        reconcile a detected DUPLICATE write against this same system of record.
        The fault-model study never issues a DELETE, so study behavior and every
        ``?fault=`` path are unchanged.
        """
        with self._lock, self._connect() as conn:
            cursor = conn.execute("DELETE FROM records WHERE id = ?", (record_id,))
            return cursor.rowcount > 0

    def snapshot(self, *, surface: Optional[str] = None) -> dict:
        """Return the ledger. ``surface=None`` is the COMPLETE read path (every
        mutable surface); ``surface="disbursements"`` is the SINGLE-surface read
        path a disbursements-only oracle sees (blind to a fees-surface write)."""
        with self._lock, self._connect() as conn:
            query = (
                "SELECT id, loan_id, product, amount, memo, source, "
                "record_key, surface FROM records"
            )
            params: tuple[str, ...] = ()
            if surface is not None:
                query += " WHERE surface = ?"
                params = (surface,)
            query += " ORDER BY id"
            records = [self._record(row) for row in conn.execute(query, params)]
            rejected = conn.execute(
                "SELECT value FROM metadata WHERE name = 'rejected_writes'"
            ).fetchone()
            return {
                "records": records,
                "rejected_writes": int(rejected[0]) if rejected is not None else 0,
            }

    def close(self) -> None:
        """Remove this harness's isolated temporary SQLite ledger."""
        self._tempdir.cleanup()


def _make_handler(db: LedgerDB, directory: str):
    class _Handler(SimpleHTTPRequestHandler):
        """Serves the static MockLoan app and a small fault-injection API."""

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

        # -- helpers ------------------------------------------------------
        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}

        # -- routing ------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/api/db":
                # Complete read path: every mutable surface (disbursements + fees).
                self._send_json(200, db.snapshot())
                return
            if path == "/api/disbursements":
                # Single-surface read path: the disbursements ledger only.
                self._send_json(200, db.snapshot(surface="disbursements"))
                return
            super().do_GET()

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/api/reset":
                body = self._read_body()
                db.reset(seed_concurrent=bool(body.get("seed_concurrent")))
                self._send_json(200, {"ok": True})
                return
            if path == "/api/disbursement":
                self._handle_disbursement(parsed)
                return
            self._send_json(404, {"ok": False, "error": "not found"})

        def do_DELETE(self) -> None:  # noqa: N802
            # Additive compensation route: remove one disbursement row by id so
            # an EffectVerifier compensation hook can reconcile a duplicate
            # against this system of record. Not used by any ?fault= path.
            path = urlparse(self.path).path
            prefix = "/api/disbursement/"
            if path.startswith(prefix):
                raw = path[len(prefix) :]
                try:
                    record_id = int(raw)
                except ValueError:
                    self._send_json(400, {"ok": False, "error": "bad id"})
                    return
                removed = db.delete(record_id)
                self._send_json(
                    200 if removed else 404,
                    {"ok": removed, "id": record_id},
                )
                return
            self._send_json(404, {"ok": False, "error": "not found"})

        def _handle_disbursement(self, parsed) -> None:
            qs = parse_qs(parsed.query)
            fault = (qs.get("fault", [""])[0] or "").strip()
            body = self._read_body()
            loan_id = str(body.get("loan_id", ""))
            product = str(body.get("product", "") or "Personal")
            amount = str(body.get("amount", ""))
            memo = str(body.get("memo", ""))
            key = body.get("key")

            if fault == "session":
                # Session expired mid-workflow: reject, book nothing.
                self._send_json(401, {"ok": False, "error": "session expired"})
                return
            if fault == "optimistic":
                # Core rejects a write the UI already reported as authorized.
                db.note_rejected()
                self._send_json(
                    409, {"ok": False, "error": "rejected after optimistic UI"}
                )
                return
            if fault == "partial":
                # Core booked the row but dropped the memo field.
                rec = db.add(loan_id, product, amount, "", key=key)
                self._send_json(200, {"ok": True, "id": rec["id"], "partial": True})
                return
            if fault == "timeout":
                # Book first, THEN hang past the client's abort window.
                rec = db.add(loan_id, product, amount, memo, key=key)
                time.sleep(TIMEOUT_HANG_S)
                self._send_json(200, {"ok": True, "id": rec["id"]})
                return
            if fault == "stale":
                # Last-write-wins over a concurrently-modified loan.
                rec = db.add(
                    loan_id, product, amount, memo, key=key, overwrite_loan=True
                )
                self._send_json(200, {"ok": True, "id": rec["id"]})
                return
            if fault == "collateral":
                # Book the CORRECT disbursement (the disbursements ledger looks
                # perfect), then ALSO book a spurious money-movement to a
                # SEPARATE fees / general-ledger surface: an unauthorized
                # servicing fee referencing the same loan and funding memo. A
                # disbursements-only oracle certifies the write; only a complete
                # read path over both surfaces sees the collateral row.
                rec = db.add(loan_id, product, amount, memo, key=key)
                db.add(loan_id, product, amount, memo, surface="fees")
                self._send_json(200, {"ok": True, "id": rec["id"], "collateral": True})
                return
            # ok / duplicate / double / idempotent: a plain accepted write.
            # ``idempotent`` de-duplicates because the app supplies ``key``.
            rec = db.add(loan_id, product, amount, memo, key=key)
            self._send_json(200, {"ok": True, "id": rec["id"]})

    return partial(_Handler, directory=directory)


def serve(
    port: int = 0, *, host: str = "127.0.0.1"
) -> tuple[str, LedgerDB, Callable[[], None]]:
    """Serve MockLoan with the fault-injection API in a background thread.

    Args:
        port: TCP port to bind; ``0`` (default) picks an ephemeral port.
        host: Interface to bind; defaults to localhost only.

    Returns:
        ``(url, db, stop)`` where ``url`` is the app's base URL (trailing
        slash), ``db`` is the ground-truth store to inspect after a run, and
        ``stop()`` shuts the server down and joins its thread.
    """
    db = LedgerDB()
    handler = _make_handler(db, str(STATIC_DIR))
    httpd = ThreadingHTTPServer((host, port), handler)
    actual_port = httpd.server_address[1]
    thread = threading.Thread(
        target=httpd.serve_forever, name="mockloan-fault-http", daemon=True
    )
    thread.start()
    url = f"http://{host}:{actual_port}/"

    def stop() -> None:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        db.close()

    return url, db, stop

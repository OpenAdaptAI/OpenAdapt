"""A real SQLite-backed system of record with a persistence-boundary fault hook.

This is the system of record the end-to-end silent-wrong-effect harness
(:mod:`benchmark.effect_e2e.run`) writes to THROUGH the governed replay path.
It exists to break the circularity of the older definitional benchmark
(``openadapt_flow.benchmark.silent_wrong_action``), whose effect verifier and
ground-truth judge read the SAME in-process Python object and whose write
bypassed the :class:`~openadapt_flow.runtime.replayer.Replayer`.

Three genuinely distinct data-access mechanisms meet here, and keeping them
distinct is the whole point:

1. **WRITE path** -- ``POST /api/encounter?fault=<mode>``. The governed
   replay's :class:`~openadapt_flow.runtime.actuators.ApiActuator` issues this
   request; the service's write handler opens its OWN sqlite connection and
   mutates the on-disk database. This is where the fault is injected, at the
   persistence boundary, AFTER the app has "painted success".
2. **Effect VERIFIER read-back (out-of-band oracle)** -- ``GET /api/records``.
   A DIFFERENT HTTP verb and endpoint than the write, served by a SEPARATE
   read handler that opens its OWN fresh sqlite connection and ``SELECT``s the
   encounters. The verifier never sees the POST's return value; it reads the
   record. This is what a deployment's REST connector does.
3. **The app's SELF-REPORTED banner** -- ``GET /api/ui/last-save``. What the
   application PAINTED after the save (the optimistic UI echo), which under a
   persistence fault lies. This is the surface a screen / vision postcondition
   reads, modelled as an endpoint so the screen arm is exercised through the
   same verifier machinery as the effect arm.

The independent GROUND TRUTH (path (c) in the harness) does NOT go through this
service at all: it opens the sqlite FILE directly, read-only, on its own
connection, and inspects EVERY table. A bug or lie in the service's read
handler therefore cannot fool it, and the write's HTTP success flag never
reaches it.

The database has two tables on purpose:

- ``encounters`` -- the target record surface the ``/api/records`` oracle reads.
- ``billing`` -- a SEPARATE mutable surface the ``/api/records`` oracle does
  NOT read. A collateral write here is invisible to an encounters-scoped
  oracle: the honest structural limit an out-of-band record oracle has (it
  catches exactly what its read path covers). The ground truth audits both.

All data is synthetic. Nothing here touches the network beyond localhost.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

#: How long ``timeout`` mode hangs after committing, in seconds. The API
#: actuator's per-request timeout fires well before this, so the write is
#: known-committed server-side while the client sees an unknown outcome and
#: HALTs (the no-double-write contract) -- exactly the committed-then-timed-out
#: fault. Kept just above the actuator timeout so the study runs quickly.
TIMEOUT_HANG_S = 1.5

#: The recorded target the whole suite attacks (mirrors the fault matrix).
TARGET_PATIENT = "p1"
TARGET_TYPE = "Triage"


@dataclass(frozen=True)
class ServiceHandle:
    """Everything a caller needs to drive and inspect the record service."""

    base_url: str
    db_path: Path
    stop: Callable[[], None]


def _init_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            DROP TABLE IF EXISTS encounters;
            DROP TABLE IF EXISTS billing;
            DROP TABLE IF EXISTS banner;
            CREATE TABLE encounters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id TEXT,
                type TEXT,
                note TEXT,
                source TEXT,
                key TEXT
            );
            CREATE TABLE billing (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id TEXT,
                amount TEXT
            );
            CREATE TABLE banner (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id TEXT,
                type TEXT,
                note TEXT
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


class _Store:
    """Thin write-side wrapper over the on-disk database.

    Serializes writes under a lock (sqlite would otherwise raise "database is
    locked" under the threaded HTTP server) and opens a fresh connection per
    operation so no connection is shared across handler threads.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.path), timeout=5.0)

    def reset(self, *, seed_concurrent: bool = False) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM encounters")
                conn.execute("DELETE FROM billing")
                conn.execute("DELETE FROM banner")
                if seed_concurrent:
                    # A concurrent clinician already charted an URGENT encounter
                    # on the same patient between record and replay. A blind
                    # last-write-wins save (``stale`` mode) will destroy it.
                    conn.execute(
                        "INSERT INTO encounters (patient_id, type, note, source, key) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            TARGET_PATIENT,
                            "Consult",
                            "URGENT: penicillin allergy -- do not prescribe",
                            "other",
                            None,
                        ),
                    )
                conn.commit()
            finally:
                conn.close()

    def _insert_encounter(
        self,
        conn: sqlite3.Connection,
        patient_id: str,
        enc_type: str,
        note: str,
        key: Optional[str],
    ) -> None:
        conn.execute(
            "INSERT INTO encounters (patient_id, type, note, source, key) "
            "VALUES (?, ?, ?, ?, ?)",
            (patient_id, enc_type, note, "replay", key),
        )

    def _set_banner(
        self,
        conn: sqlite3.Connection,
        rows: list[tuple[str, str, str]],
    ) -> None:
        conn.execute("DELETE FROM banner")
        for patient_id, enc_type, note in rows:
            conn.execute(
                "INSERT INTO banner (patient_id, type, note) VALUES (?, ?, ?)",
                (patient_id, enc_type, note),
            )

    def apply_write(
        self,
        fault: str,
        *,
        patient_id: str,
        enc_type: str,
        note: str,
        key: Optional[str],
    ) -> int:
        """Apply one write under ``fault`` at the persistence boundary.

        Returns the HTTP status the app-side write endpoint should report --
        the "did the app paint success?" signal, INDEPENDENT of whether the
        database actually holds the intended record.
        """
        with self._lock:
            conn = self._connect()
            try:
                painted = [(patient_id, enc_type, note)]
                if fault == "session":
                    # Session expired: reject, persist nothing, no banner.
                    self._set_banner(conn, [])
                    conn.commit()
                    return 401
                if fault == "optimistic":
                    # The UI already painted success; the server then rejects.
                    # (The governed actuator reads the status, not the banner.)
                    self._set_banner(conn, painted)
                    conn.commit()
                    return 409
                if fault == "no_persist":
                    # 2xx but nothing lands (phantom write).
                    self._set_banner(conn, painted)
                elif fault == "partial":
                    # Row persists but the note field is dropped.
                    self._insert_encounter(conn, patient_id, enc_type, "", key)
                    self._set_banner(conn, painted)
                elif fault in ("duplicate", "double"):
                    # A double-delivered write lands twice from one actuation.
                    self._insert_encounter(conn, patient_id, enc_type, note, key)
                    self._insert_encounter(conn, patient_id, enc_type, note, key)
                    self._set_banner(conn, painted)
                elif fault == "wrong_record":
                    # The write lands on the WRONG patient (data drift redirected
                    # the target); the banner still shows the intended patient.
                    self._insert_encounter(conn, "p2", enc_type, note, key)
                    self._set_banner(conn, painted)
                elif fault == "stale":
                    # Last-write-wins over a concurrently-modified row: destroy
                    # every existing row for this patient, then write ours.
                    conn.execute(
                        "DELETE FROM encounters WHERE patient_id = ?", (patient_id,)
                    )
                    self._insert_encounter(conn, patient_id, enc_type, note, key)
                    self._set_banner(conn, painted)
                elif fault == "collateral_unaudited":
                    # The target encounter lands correctly, BUT a stray row is
                    # also written to the billing surface the record oracle does
                    # not read -- a collateral write to an unaudited surface.
                    self._insert_encounter(conn, patient_id, enc_type, note, key)
                    conn.execute(
                        "INSERT INTO billing (patient_id, amount) VALUES (?, ?)",
                        (patient_id, "999.00"),
                    )
                    self._set_banner(conn, painted)
                elif fault == "timeout":
                    # Commit first, then hang past the client's timeout below.
                    self._insert_encounter(conn, patient_id, enc_type, note, key)
                    self._set_banner(conn, painted)
                else:
                    # ok / unknown: a plain accepted write.
                    self._insert_encounter(conn, patient_id, enc_type, note, key)
                    self._set_banner(conn, painted)
                conn.commit()
            finally:
                conn.close()
        if fault == "timeout":
            # Hang AFTER releasing the lock and committing, so the row is durably
            # persisted while the client observes an unknown outcome.
            time.sleep(TIMEOUT_HANG_S)
        return 200

    def read_encounters(self) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, patient_id, type, note, source, key FROM encounters "
                "ORDER BY id"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def read_banner(self) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT patient_id, type, note FROM banner ORDER BY id"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def _make_handler(store: _Store):
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:  # noqa: A002
            pass

        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # The ``timeout`` fault deliberately outlives the client's
                # request timeout, so the client socket may already be gone
                # when this late response is written. That is the fault being
                # modelled, not an error -- swallow it rather than spew a
                # background-thread traceback.
                pass

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/api/records":
                # The out-of-band effect oracle read path (encounters only).
                self._send_json(200, {"records": store.read_encounters()})
                return
            if path == "/api/ui/last-save":
                # The app's self-reported banner (what the screen shows).
                self._send_json(200, {"records": store.read_banner()})
                return
            self._send_json(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/reset":
                body = self._read_body()
                store.reset(seed_concurrent=bool(body.get("seed_concurrent")))
                self._send_json(200, {"ok": True})
                return
            if parsed.path == "/api/encounter":
                qs = parse_qs(parsed.query)
                fault = (qs.get("fault", [""])[0] or "").strip()
                body = self._read_body()
                status = store.apply_write(
                    fault,
                    patient_id=str(body.get("patient_id", "")),
                    enc_type=str(body.get("type", "") or TARGET_TYPE),
                    note=str(body.get("note", "")),
                    key=body.get("key"),
                )
                self._send_json(status, {"ok": 200 <= status < 300, "fault": fault})
                return
            self._send_json(404, {"ok": False, "error": "not found"})

    return _Handler


def serve(db_path: Path, *, host: str = "127.0.0.1", port: int = 0) -> ServiceHandle:
    """Initialize the database at ``db_path`` and serve it on a loopback port.

    Returns a :class:`ServiceHandle` with the base URL, the sqlite file path
    (for the independent ground-truth reader), and a ``stop`` callable.
    """
    _init_db(db_path)
    store = _Store(db_path)
    handler = _make_handler(store)
    httpd = ThreadingHTTPServer((host, port), handler)
    actual_port = httpd.server_address[1]
    thread = threading.Thread(
        target=httpd.serve_forever, name="effect-e2e-record-service", daemon=True
    )
    thread.start()

    def stop() -> None:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)

    return ServiceHandle(
        base_url=f"http://{host}:{actual_port}", db_path=db_path, stop=stop
    )

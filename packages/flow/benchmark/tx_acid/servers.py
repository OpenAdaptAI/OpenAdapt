"""High-realism multi-user SQLite-WAL HTTP Systems of Record and fault injectors.

Provides three production-archetype HTTP servers backed by SQLite in WAL mode,
capable of bulk-seeding 50,000 rows in <0.4s and serving 20+ concurrent writers
alongside agent commit verification without lock contention:

1. ``ClinicalChartServer``: EHR archetype with 200-row offset pagination cap,
   O(1) indexed reads, ETag/version/last_author metadata, Title Case names,
   UPPER addresses, bijective state codes, 2-3 char names, and confusable IDs.
2. ``ReplaceTaskServer``: Replace-style ERP/task archetype where ``PUT``
   overwrites omitted fields with ``""`` or ``0`` while returning ``200 OK``,
   and ``POST`` is non-idempotent.
3. ``ClaimsLedgerServer``: Held-out regulated ledger archetype enforcing
   ``ETag``/``If-Match`` OCC, server-computed ``updated_at`` and ``audit_seq``,
   E.164 phone normalization, and UTC ISO-8601 date normalization.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import chain, islice
from typing import Any, Iterator, Literal, Mapping, Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlsplit

from openadapt_flow.tx.types import EndpointTemplate, EntityLensSpec, FieldCodec

PAGE_LIMIT_CAP = 200

FaultKind = Literal[
    "partial_put_clobber",
    "silent_drop_200_ok",
    "wrong_value_mutation",
    "duplicate_post_on_retry",
    "collateral_extra_write",
    "wrong_target_record",
    "same_record_concurrent_race",
]

ALL_WRITE_FAULTS: tuple[FaultKind, ...] = (
    "partial_put_clobber",
    "silent_drop_200_ok",
    "wrong_value_mutation",
    "duplicate_post_on_retry",
    "collateral_extra_write",
    "wrong_target_record",
)

ALL_BENCHMARK_FAULTS: tuple[FaultKind, ...] = ALL_WRITE_FAULTS + (
    "same_record_concurrent_race",
)

STATE_BIJECTION_MAP: dict[str, str] = {
    "Massachusetts": "MA",
    "California": "CA",
    "Ontario": "ON",
    "New York": "NY",
    "Texas": "TX",
    "Illinois": "IL",
    "Florida": "FL",
    "Washington": "WA",
    "Pennsylvania": "PA",
    "Ohio": "OH",
    "Georgia": "GA",
    "Michigan": "MI",
    "Quebec": "QC",
    "British Columbia": "BC",
    "Alberta": "AB",
}

_STATE_CASEFOLD_MAP: dict[str, str] = {
    k.casefold(): v for k, v in STATE_BIJECTION_MAP.items()
}


def utc_iso_now() -> str:
    """Return a UTC ISO-8601 timestamp with microsecond precision and Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def normalize_title_words(value: str) -> str:
    """Normalize names via word-level capitalization (matches capitalize_words codec)."""
    return " ".join(part.capitalize() for part in str(value).strip().split())


def normalize_upper(value: str) -> str:
    """Normalize street/city strings via strip + UPPER."""
    return str(value).strip().upper()


def normalize_state(value: str) -> str:
    """Map full state/province names to 2-letter postal codes, else strip + UPPER."""
    cleaned = str(value).strip()
    mapped = _STATE_CASEFOLD_MAP.get(cleaned.casefold())
    if mapped is not None:
        return mapped
    return cleaned.upper()


def normalize_phone_e164(value: str) -> str:
    """Normalize North American / international phone strings to +E.164 format."""
    raw = str(value).strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if digits:
        return f"+{digits}"
    return raw


def normalize_iso_date_utc(value: str) -> str:
    """Normalize YYYY-MM-DD dates to midnight UTC ISO-8601 timestamps."""
    cleaned = str(value).strip()
    if len(cleaned) == 10 and cleaned[4:5] == "-" and cleaned[7:8] == "-":
        return f"{cleaned}T00:00:00Z"
    if cleaned.endswith("+00:00"):
        return f"{cleaned[:-6]}Z"
    return cleaned


def _etag_matches(
    if_match_header: str, current_etag: str, current_version: int
) -> bool:
    """Evaluate HTTP If-Match precondition against current ETag / version."""
    candidate_raw = if_match_header.strip()
    if candidate_raw == "*":
        return True
    norm_current = current_etag.strip()
    if norm_current.startswith("W/"):
        norm_current = norm_current[2:].strip()
    norm_current_unquoted = norm_current.strip('"')

    for token in candidate_raw.split(","):
        tok = token.strip()
        if tok.startswith("W/"):
            tok = tok[2:].strip()
        tok_unquoted = tok.strip('"')
        if (
            tok == current_etag
            or tok_unquoted == norm_current_unquoted
            or tok_unquoted == str(current_version)
            or tok_unquoted == f"v{current_version}"
        ):
            return True
    return False


class _FastThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class BaseSorServer:
    """Thread-safe SQLite-WAL HTTP System-of-Record base fixture."""

    app_name: str = "base"
    entity_name: str = "entity"
    collection_name: str = "entities"
    table_name: str = "entities"
    confusable_primary_id: str = "ENT-0O1l8"
    confusable_sibling_id: str = "ENT-0O118"

    def __init__(
        self,
        *,
        seed_rows: int = 50_000,
        host: str = "127.0.0.1",
        port: int = 0,
        db_path: Optional[str] = None,
    ) -> None:
        if seed_rows < 4:
            raise ValueError("seed_rows must be >= 4 to include edge-case records")
        self.seed_rows = seed_rows
        self.host = host
        self._requested_port = port

        self._temp_dir: Optional[tempfile.TemporaryDirectory[str]] = None
        if db_path is None:
            self._temp_dir = tempfile.TemporaryDirectory(
                prefix=f"tx_acid_{self.app_name}_"
            )
            self.db_path = os.path.join(self._temp_dir.name, "sor.sqlite3")
        else:
            self.db_path = db_path

        self._tls = threading.local()
        self._conns_lock = threading.Lock()
        self._conns: list[sqlite3.Connection] = []
        self._write_lock = threading.RLock()
        self._seq_counter = seed_rows
        self._audit_seq_counter = seed_rows

        self._stats_lock = threading.Lock()
        self.total_requests: int = 0
        self.total_rows_returned: int = 0
        self.seed_duration_ms: float = 0.0

        self._httpd: Optional[_FastThreadingHTTPServer] = None
        self._server_thread: Optional[threading.Thread] = None
        self.port: int = 0

        self._init_and_seed()

    @property
    def collection_path(self) -> str:
        return f"/api/{self.collection_name}"

    @property
    def base_url(self) -> str:
        if self._httpd is None or self.port == 0:
            raise RuntimeError(f"{self.__class__.__name__} has not been started")
        return f"http://{self.host}:{self.port}"

    def _conn(self) -> sqlite3.Connection:
        conn: Optional[sqlite3.Connection] = getattr(self._tls, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self.db_path,
                timeout=30.0,
                check_same_thread=False,
                isolation_level=None,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA temp_store=MEMORY;")
            with self._conns_lock:
                self._conns.append(conn)
            self._tls.conn = conn
        return conn

    def _init_and_seed(self) -> None:
        conn = self._conn()
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA temp_store=MEMORY;")
        conn.execute("PRAGMA cache_size=-32000;")
        self._create_schema(conn)

        t0 = time.perf_counter()
        conn.execute("BEGIN IMMEDIATE;")
        try:
            self._insert_seed_batches(conn)
            # Indexes are part of fixture initialization too. One commit avoids
            # repeated WAL synchronization and rolls back an incomplete seed.
            self._create_indexes(conn)
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        self.seed_duration_ms = (time.perf_counter() - t0) * 1000.0

    def _insert_seed_batches(self, conn: sqlite3.Connection) -> None:
        """Insert the same seed stream with bounded multi-row statements.

        All fixtures share this path. Bound parameters retain value types; the
        portable 999-variable ceiling also covers Python 3.10 SQLite builds.
        """
        prefix, separator, values = self._seed_insert_sql().partition("VALUES")
        if (
            not separator
            or re.fullmatch(r"\s*\(\s*\?(?:\s*,\s*\?)*\s*\)\s*;?\s*", values) is None
        ):
            raise ValueError("seed INSERT must have one positional VALUES row")
        values = values.strip().removesuffix(";").strip()
        width = values.count("?")
        variable_limit = 999
        if hasattr(conn, "getlimit"):
            variable_limit = min(
                variable_limit, conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
            )
        if width > variable_limit:
            raise ValueError("seed row exceeds the connection parameter limit")
        batch_rows = min(128, variable_limit // width)
        rows = iter(self._generate_seed_rows(self.seed_rows))
        while batch := list(islice(rows, batch_rows)):
            if any(len(row) != width for row in batch):
                raise ValueError("seed row shape differs from its INSERT")
            conn.execute(
                prefix + "VALUES " + ",".join([values] * len(batch)),
                tuple(chain.from_iterable(batch)),
            )

    def journal_mode(self) -> str:
        row = self._conn().execute("PRAGMA journal_mode;").fetchone()
        return str(row[0]) if row is not None else ""

    def count_rows(self) -> int:
        row = (
            self._conn().execute(f"SELECT COUNT(*) FROM {self.table_name};").fetchone()
        )
        return int(row[0]) if row is not None else 0

    def get_record_direct(self, entity_id: str) -> Optional[dict[str, Any]]:
        row = (
            self._conn()
            .execute(f"SELECT * FROM {self.table_name} WHERE id = ?;", (entity_id,))
            .fetchone()
        )
        return self._row_to_dict(row) if row is not None else None

    def find_records_direct(self, field: str, value: Any) -> list[dict[str, Any]]:
        if field not in self._allowed_filter_columns():
            raise ValueError(f"Unsupported filter column: {field}")
        rows = (
            self._conn()
            .execute(f"SELECT * FROM {self.table_name} WHERE {field} = ?;", (value,))
            .fetchall()
        )
        return [self._row_to_dict(r) for r in rows]

    def snapshot_records(
        self, entity_ids: tuple[str, ...] | list[str]
    ) -> dict[str, Optional[dict[str, Any]]]:
        return {eid: self.get_record_direct(eid) for eid in entity_ids}

    def restore_records(self, snapshot: Mapping[str, Optional[dict[str, Any]]]) -> None:
        with self._write_lock:
            conn = self._conn()
            conn.execute("BEGIN IMMEDIATE;")
            try:
                for eid, record in snapshot.items():
                    if record is None:
                        conn.execute(
                            f"DELETE FROM {self.table_name} WHERE id = ?;", (eid,)
                        )
                    else:
                        self._upsert_raw_record(conn, record)
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def delete_where_field(self, field: str, value: Any) -> int:
        if field not in self._allowed_filter_columns():
            raise ValueError(f"Unsupported filter column: {field}")
        with self._write_lock:
            conn = self._conn()
            conn.execute("BEGIN IMMEDIATE;")
            try:
                cur = conn.execute(
                    f"DELETE FROM {self.table_name} WHERE {field} = ?;",
                    (value,),
                )
                conn.execute("COMMIT;")
                return int(cur.rowcount)
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def next_entity_id(self, prefix: str) -> str:
        with self._write_lock:
            idx = self._seq_counter
            self._seq_counter += 1
            return f"{prefix}-{idx:06d}"

    def next_audit_seq(self) -> int:
        with self._write_lock:
            self._audit_seq_counter += 1
            return self._audit_seq_counter

    def start(self) -> "BaseSorServer":
        if self._httpd is not None:
            return self
        server_ref = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def _dispatch(self, method: str) -> None:
                content_len = int(self.headers.get("Content-Length", "0") or "0")
                raw_body = self.rfile.read(content_len) if content_len > 0 else b""
                headers_dict = {k: v for k, v in self.headers.items()}
                status, resp_headers, resp_obj, rows_out = (
                    server_ref.handle_http_request(
                        method=method,
                        raw_path=self.path,
                        headers=headers_dict,
                        raw_body=raw_body,
                    )
                )
                with server_ref._stats_lock:
                    server_ref.total_requests += 1
                    server_ref.total_rows_returned += rows_out

                payload = json.dumps(resp_obj, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                for hk, hv in resp_headers.items():
                    if any(ch in str(hk) + str(hv) for ch in "\r\n"):
                        raise ValueError("invalid HTTP response header")
                    self.send_header(
                        str(hk).replace("\r", "").replace("\n", ""),
                        str(hv).replace("\r", "").replace("\n", ""),
                    )
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:
                self._dispatch("GET")

            def do_POST(self) -> None:
                self._dispatch("POST")

            def do_PUT(self) -> None:
                self._dispatch("PUT")

            def do_PATCH(self) -> None:
                self._dispatch("PATCH")

            def do_DELETE(self) -> None:
                self._dispatch("DELETE")

        self._httpd = _FastThreadingHTTPServer(
            (self.host, self._requested_port), _Handler
        )
        self.port = int(self._httpd.server_address[1])
        self._server_thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.02},
            name=f"{self.app_name}-http-server",
            daemon=True,
        )
        self._server_thread.start()
        return self

    def close(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._server_thread is not None:
            self._server_thread.join(timeout=3.0)
            self._server_thread = None
        with self._conns_lock:
            for conn in self._conns:
                try:
                    conn.close()
                except Exception:
                    pass
            self._conns.clear()
        self._tls = threading.local()
        if self._temp_dir is not None:
            self._temp_dir.cleanup()
            self._temp_dir = None

    def __enter__(self) -> "BaseSorServer":
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def handle_http_request(
        self,
        *,
        method: str,
        raw_path: str,
        headers: Mapping[str, str],
        raw_body: bytes,
    ) -> tuple[int, dict[str, str], dict[str, Any], int]:
        parsed = urlparse(raw_path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query, keep_blank_values=True)

        if path == "/health":
            return 200, {}, {"ok": True, "app": self.app_name}, 0

        col_path = self.collection_path
        item_prefix = f"{col_path}/"

        body: dict[str, Any] = {}
        if raw_body:
            try:
                decoded = json.loads(raw_body.decode("utf-8"))
                if isinstance(decoded, dict):
                    body = decoded
            except json.JSONDecodeError:
                return 400, {}, {"error": "invalid_json"}, 0

        headers_ci = {k.lower(): v for k, v in headers.items()}
        actor = headers_ci.get("x-actor-id") or str(body.get("actor_id") or "agent")
        silent_drop = headers_ci.get("x-fault-silent-drop") == "1"

        if path == col_path:
            if method == "GET":
                return self._handle_collection_get(query)
            if method == "POST":
                if silent_drop:
                    fake = self._build_fake_drop_response(
                        entity_id=None, body=body, actor=actor
                    )
                    return 201, {"ETag": str(fake["etag"])}, fake, 0
                return self._handle_create(body=body, actor=actor)
            return 405, {}, {"error": "method_not_allowed"}, 0

        if path.startswith(item_prefix):
            entity_id = path[len(item_prefix) :]
            if not entity_id or "/" in entity_id:
                return 404, {}, {"error": "not_found"}, 0
            if method == "GET":
                rec = self.get_record_direct(entity_id)
                if rec is None:
                    return 404, {}, {"error": "not_found", "id": entity_id}, 0
                return 200, {"ETag": str(rec["etag"])}, rec, 1
            if method == "DELETE":
                return self._handle_delete(entity_id)
            if method in ("PUT", "PATCH"):
                if silent_drop:
                    fake = self._build_fake_drop_response(
                        entity_id=entity_id,
                        body=body,
                        actor=actor,
                    )
                    return 200, {"ETag": str(fake["etag"])}, fake, 0
                if_match = headers_ci.get("if-match")
                return self._handle_item_write(
                    method=method,
                    entity_id=entity_id,
                    body=body,
                    actor=actor,
                    if_match=if_match,
                )
            return 405, {}, {"error": "method_not_allowed"}, 0

        return 404, {}, {"error": "not_found"}, 0

    def _handle_collection_get(
        self, query: Mapping[str, list[str]]
    ) -> tuple[int, dict[str, str], dict[str, Any], int]:
        try:
            req_limit = int(query.get("_limit", [str(PAGE_LIMIT_CAP)])[0])
        except ValueError:
            req_limit = PAGE_LIMIT_CAP
        limit = max(1, min(PAGE_LIMIT_CAP, req_limit))

        try:
            offset = max(0, int(query.get("_offset", ["0"])[0]))
        except ValueError:
            offset = 0

        where_clauses: list[str] = []
        params: list[Any] = []
        filter_params_for_link: dict[str, str] = {}
        for col in self._allowed_filter_columns():
            if col in query and query[col]:
                val = query[col][0]
                where_clauses.append(f"{col} = ?")
                params.append(self._normalize_filter_param(col, val))
                filter_params_for_link[col] = val

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        # Fetch limit + 1 rows to know in O(limit) whether a next page exists without COUNT(*) table scan!
        sql = (
            f"SELECT * FROM {self.table_name} {where_sql} "
            f"ORDER BY rowid ASC LIMIT ? OFFSET ?;"
        )
        rows = self._conn().execute(sql, (*params, limit + 1, offset)).fetchall()
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        items = [self._row_to_dict(r) for r in page_rows]

        base = f"http://{self.host}:{self.port}{self.collection_path}"
        self_qs = urlencode(
            {**filter_params_for_link, "_limit": str(limit), "_offset": str(offset)}
        )
        links: dict[str, str] = {"self": f"{base}?{self_qs}"}
        if has_more:
            next_qs = urlencode(
                {
                    **filter_params_for_link,
                    "_limit": str(limit),
                    "_offset": str(offset + limit),
                }
            )
            links["next"] = f"{base}?{next_qs}"

        envelope: dict[str, Any] = {
            "items": items,
            "count": len(items),
            "limit": limit,
            "offset": offset,
            "links": links,
        }
        return 200, {}, envelope, len(items)

    def _handle_delete(
        self, entity_id: str
    ) -> tuple[int, dict[str, str], dict[str, Any], int]:
        with self._write_lock:
            conn = self._conn()
            conn.execute("BEGIN IMMEDIATE;")
            try:
                cur = conn.execute(
                    f"DELETE FROM {self.table_name} WHERE id = ?;",
                    (entity_id,),
                )
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
        if cur.rowcount == 0:
            return 404, {}, {"error": "not_found", "id": entity_id}, 0
        return 200, {}, {"deleted": True, "id": entity_id}, 0

    def _build_fake_drop_response(
        self,
        *,
        entity_id: Optional[str],
        body: Mapping[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        """Synthesize a plausible 2xx body for silent_drop_200_ok without writing SQLite."""
        if entity_id is not None:
            existing = self.get_record_direct(entity_id) or {
                "id": entity_id,
                "version": 1,
            }
            merged = dict(existing)
            merged.update(body)
            ver = int(existing.get("version", 1)) + 1
            merged["id"] = entity_id
            merged["version"] = ver
            merged["etag"] = f'"{entity_id}:v{ver}"'
            merged["updated_at"] = utc_iso_now()
            merged["last_author"] = actor
            return merged
        fake_id = "FAKE-DROPPED-0001"
        merged = dict(body)
        merged["id"] = fake_id
        merged["version"] = 1
        merged["etag"] = f'"{fake_id}:v1"'
        merged["updated_at"] = utc_iso_now()
        merged["last_author"] = actor
        return merged

    # --- Subclass hooks ---

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        raise NotImplementedError

    def _create_indexes(self, conn: sqlite3.Connection) -> None:
        raise NotImplementedError

    def _seed_insert_sql(self) -> str:
        raise NotImplementedError

    def _generate_seed_rows(self, count: int) -> Iterator[tuple[Any, ...]]:
        raise NotImplementedError

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    def _allowed_filter_columns(self) -> tuple[str, ...]:
        raise NotImplementedError

    def _normalize_filter_param(self, column: str, value: str) -> str:
        return value

    def _upsert_raw_record(
        self, conn: sqlite3.Connection, record: Mapping[str, Any]
    ) -> None:
        raise NotImplementedError

    def _handle_create(
        self,
        *,
        body: Mapping[str, Any],
        actor: str,
    ) -> tuple[int, dict[str, str], dict[str, Any], int]:
        raise NotImplementedError

    def _handle_item_write(
        self,
        *,
        method: str,
        entity_id: str,
        body: Mapping[str, Any],
        actor: str,
        if_match: Optional[str],
    ) -> tuple[int, dict[str, str], dict[str, Any], int]:
        raise NotImplementedError

    def lens_spec(self, base_url: Optional[str] = None) -> EntityLensSpec:
        raise NotImplementedError

    def sample_har_entries(
        self, base_url: Optional[str] = None
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def background_id_at(self, offset: int) -> str:
        raise NotImplementedError

    def make_background_update_payload(
        self, worker_id: int, seq: int
    ) -> tuple[str, dict[str, Any]]:
        """Return (http_method, json_body) for a background worker update."""
        raise NotImplementedError

    def make_background_create_payload(
        self, worker_id: int, seq: int
    ) -> dict[str, Any]:
        """Return json_body for a background worker POST create."""
        raise NotImplementedError

    def apply_concurrent_race_mutation(
        self, entity_id: str, actor: str = "concurrent-clinician"
    ) -> None:
        """Apply an out-of-band concurrent update to entity_id to simulate a race."""
        raise NotImplementedError


class ClinicalChartServer(BaseSorServer):
    """EHR System-of-Record archetype (/api/patients)."""

    app_name = "clinical_chart_ehr"
    entity_name = "patient"
    collection_name = "patients"
    table_name = "patients"
    confusable_primary_id = "PT-0O1l8"
    confusable_sibling_id = "PT-0O118"

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS patients (
                id TEXT PRIMARY KEY,
                mrn TEXT NOT NULL,
                fname TEXT NOT NULL,
                lname TEXT NOT NULL,
                email TEXT NOT NULL,
                street TEXT NOT NULL,
                city TEXT NOT NULL,
                state TEXT NOT NULL,
                phone TEXT NOT NULL,
                allergies TEXT NOT NULL,
                notes TEXT NOT NULL,
                version INTEGER NOT NULL,
                etag TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_author TEXT NOT NULL
            );
            """
        )

    def _create_indexes(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_patients_email ON patients(email);"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_patients_mrn ON patients(mrn);")

    def _seed_insert_sql(self) -> str:
        return (
            "INSERT INTO patients "
            "(id, mrn, fname, lname, email, street, city, state, phone, "
            "allergies, notes, version, etag, updated_at, last_author) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);"
        )

    def _generate_seed_rows(self, count: int) -> Iterator[tuple[Any, ...]]:
        ts = "2026-11-01T00:00:00.000000Z"
        special = (
            (
                "PT-0O1l8",
                "MRN-000000",
                "Bo",
                "Li",
                "bo.li@clinic.example",
                "10 OAK ST",
                "BOSTON",
                "MA",
                "+16175550100",
                "Penicillin",
                "Baseline intake verified",
                1,
                '"PT-0O1l8:v1"',
                ts,
                "seed",
            ),
            (
                "PT-0O118",
                "MRN-000001",
                "Bo",
                "Lin",
                "bo.lin@clinic.example",
                "12 OAK ST",
                "BOSTON",
                "MA",
                "+16175550101",
                "Latex",
                "Confusable sibling chart",
                1,
                '"PT-0O118:v1"',
                ts,
                "seed",
            ),
            (
                "PT-000002",
                "MRN-000002",
                "Amy",
                "Wu",
                "amy.wu@clinic.example",
                "44 RIVER AVE",
                "SAN FRANCISCO",
                "CA",
                "+14155550102",
                "Sulfa",
                "Annual physical complete",
                1,
                '"PT-000002:v1"',
                ts,
                "seed",
            ),
            (
                "PT-000003",
                "MRN-000003",
                "Al",
                "Ng",
                "al.ng@clinic.example",
                "88 KING ST W",
                "TORONTO",
                "ON",
                "+14165550103",
                "None",
                "Cardiology follow-up",
                1,
                '"PT-000003:v1"',
                ts,
                "seed",
            ),
        )
        for row in special:
            yield row

        fnames = (
            "Bo",
            "Li",
            "Amy",
            "Elena",
            "Marcus",
            "Jo",
            "Priya",
            "Noah",
            "Maya",
            "Al",
            "Hannah",
            "Lucas",
            "Zoe",
            "Ty",
            "claire",
            "Owen",
        )
        lnames = (
            "Li",
            "Wu",
            "Chen",
            "Patel",
            "Rivera",
            "Ng",
            "Brooks",
            "Kim",
            "Morgan",
            "Shah",
            "Bell",
            "Yu",
            "Park",
            "Reed",
            "Ho",
            "Diaz",
        )
        cities = (
            "BOSTON",
            "LOS ANGELES",
            "TORONTO",
            "NEW YORK",
            "AUSTIN",
            "CHICAGO",
            "SEATTLE",
            "MIAMI",
        )
        states = ("MA", "CA", "ON", "NY", "TX", "IL", "WA", "FL")
        allergies_pool = ("None", "Penicillin", "Sulfa", "Peanuts")

        for i in range(4, count):
            pid = f"PT-{i:06d}"
            fn = fnames[i & 15]
            if fn == "claire":
                fn = "Claire"
            ln = lnames[(i >> 2) & 15]
            idx8 = i & 7
            yield (
                pid,
                f"MRN-{i:06d}",
                fn,
                ln,
                f"patient.{i}@clinic.example",
                f"{100 + (i % 900)} MAIN ST",
                cities[idx8],
                states[idx8],
                f"+1617555{i % 10000:04d}",
                allergies_pool[i & 3],
                "Routine chart record",
                1,
                f'"{pid}:v1"',
                ts,
                "seed",
            )

    def _allowed_filter_columns(self) -> tuple[str, ...]:
        return ("email", "mrn", "lname")

    def _upsert_raw_record(
        self, conn: sqlite3.Connection, record: Mapping[str, Any]
    ) -> None:
        conn.execute(
            """
            INSERT OR REPLACE INTO patients
            (id, mrn, fname, lname, email, street, city, state, phone,
             allergies, notes, version, etag, updated_at, last_author)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                record["id"],
                record["mrn"],
                record["fname"],
                record["lname"],
                record["email"],
                record["street"],
                record["city"],
                record["state"],
                record["phone"],
                record["allergies"],
                record["notes"],
                int(record["version"]),
                record["etag"],
                record["updated_at"],
                record["last_author"],
            ),
        )

    def _normalize_patient_fields(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        out = dict(fields)
        if "fname" in out and out["fname"] is not None:
            out["fname"] = normalize_title_words(str(out["fname"]))
        if "lname" in out and out["lname"] is not None:
            out["lname"] = normalize_title_words(str(out["lname"]))
        if "street" in out and out["street"] is not None:
            out["street"] = normalize_upper(str(out["street"]))
        if "city" in out and out["city"] is not None:
            out["city"] = normalize_upper(str(out["city"]))
        if "state" in out and out["state"] is not None:
            out["state"] = normalize_state(str(out["state"]))
        if "email" in out and out["email"] is not None:
            out["email"] = str(out["email"]).strip().lower()
        return out

    def _handle_create(
        self,
        *,
        body: Mapping[str, Any],
        actor: str,
    ) -> tuple[int, dict[str, str], dict[str, Any], int]:
        norm = self._normalize_patient_fields(body)
        pid = str(norm.get("id") or self.next_entity_id("PT"))
        mrn = str(norm.get("mrn") or f"MRN-{pid}")
        version = 1
        etag = f'"{pid}:v{version}"'
        now = utc_iso_now()
        record: dict[str, Any] = {
            "id": pid,
            "mrn": mrn,
            "fname": str(norm.get("fname", "")),
            "lname": str(norm.get("lname", "")),
            "email": str(norm.get("email", f"{pid.lower()}@clinic.example")),
            "street": str(norm.get("street", "")),
            "city": str(norm.get("city", "")),
            "state": str(norm.get("state", "")),
            "phone": str(norm.get("phone", "+16175550000")),
            "allergies": str(norm.get("allergies", "None")),
            "notes": str(norm.get("notes", "")),
            "version": version,
            "etag": etag,
            "updated_at": now,
            "last_author": actor,
        }
        with self._write_lock:
            conn = self._conn()
            conn.execute("BEGIN IMMEDIATE;")
            try:
                self._upsert_raw_record(conn, record)
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
        return 201, {"ETag": etag}, record, 1

    def _handle_item_write(
        self,
        *,
        method: str,
        entity_id: str,
        body: Mapping[str, Any],
        actor: str,
        if_match: Optional[str],
    ) -> tuple[int, dict[str, str], dict[str, Any], int]:
        if method != "PATCH":
            return 405, {}, {"error": "use_patch_for_patients"}, 0
        norm = self._normalize_patient_fields(body)
        mutable = (
            "mrn",
            "fname",
            "lname",
            "email",
            "street",
            "city",
            "state",
            "phone",
            "allergies",
            "notes",
        )
        with self._write_lock:
            conn = self._conn()
            conn.execute("BEGIN IMMEDIATE;")
            try:
                row = conn.execute(
                    "SELECT * FROM patients WHERE id = ?;",
                    (entity_id,),
                ).fetchone()
                if row is None:
                    conn.execute("ROLLBACK;")
                    return 404, {}, {"error": "not_found", "id": entity_id}, 0
                current = dict(row)
                if if_match is not None and not _etag_matches(
                    if_match,
                    str(current["etag"]),
                    int(current["version"]),
                ):
                    conn.execute("ROLLBACK;")
                    return (
                        412,
                        {"ETag": str(current["etag"])},
                        {"error": "precondition_failed", "current": current},
                        0,
                    )
                updated = dict(current)
                for key in mutable:
                    if key in norm:
                        updated[key] = str(norm[key])
                new_ver = int(current["version"]) + 1
                new_etag = f'"{entity_id}:v{new_ver}"'
                updated["version"] = new_ver
                updated["etag"] = new_etag
                updated["updated_at"] = utc_iso_now()
                updated["last_author"] = actor
                self._upsert_raw_record(conn, updated)
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
        return 200, {"ETag": new_etag}, updated, 1

    def lens_spec(self, base_url: Optional[str] = None) -> EntityLensSpec:
        resolved_base = base_url or self.base_url
        return EntityLensSpec(
            entity_name="patient",
            base_url=resolved_base,
            id_field="id",
            write_mode="patch",
            get_one=EndpointTemplate(
                method="GET",
                path_template="/api/patients/{id}",
                id_field="id",
                etag_header="ETag",
                version_field="version",
                author_field="last_author",
            ),
            write_endpoint=EndpointTemplate(
                method="PATCH",
                path_template="/api/patients/{id}",
                id_field="id",
                etag_header="ETag",
                version_field="version",
                author_field="last_author",
            ),
            create_endpoint=EndpointTemplate(
                method="POST",
                path_template="/api/patients",
                id_field="id",
                etag_header="ETag",
                version_field="version",
                author_field="last_author",
            ),
            duplicate_search=EndpointTemplate(
                method="GET",
                path_template="/api/patients",
                query_params={"email": "{email}"},
                records_key="items",
                id_field="id",
            ),
            delete_endpoint=EndpointTemplate(
                method="DELETE",
                path_template="/api/patients/{id}",
                id_field="id",
            ),
            identity_fields=("fname", "lname"),
            volatile_fields=("updated_at", "etag", "version", "last_author", "mrn"),
            field_codecs={
                "fname": FieldCodec(kind="capitalize_words"),
                "lname": FieldCodec(kind="capitalize_words"),
                "street": FieldCodec(kind="upper"),
                "city": FieldCodec(kind="upper"),
                "state": FieldCodec(
                    kind="bijection", mapping=dict(STATE_BIJECTION_MAP)
                ),
                "email": FieldCodec(kind="lower"),
            },
            duplicate_keys=("email",),
        )

    def sample_har_entries(
        self, base_url: Optional[str] = None
    ) -> list[dict[str, Any]]:
        b = base_url or self.base_url
        return [
            {
                "request": {"method": "GET", "url": f"{b}/api/patients/PT-0O1l8"},
                "response": {
                    "status": 200,
                    "headers": [{"name": "ETag", "value": '"PT-0O1l8:v1"'}],
                    "content": {"text": json.dumps(self.get_record_direct("PT-0O1l8"))},
                },
            },
            {
                "request": {
                    "method": "PATCH",
                    "url": f"{b}/api/patients/PT-0O1l8",
                    "postData": {
                        "text": json.dumps(
                            {"street": "7 mill rd", "state": "Massachusetts"}
                        )
                    },
                },
                "response": {
                    "status": 200,
                    "headers": [{"name": "ETag", "value": '"PT-0O1l8:v2"'}],
                    "content": {
                        "text": json.dumps(
                            {
                                **(self.get_record_direct("PT-0O1l8") or {}),
                                "street": "7 MILL RD",
                                "state": "MA",
                                "version": 2,
                            }
                        )
                    },
                },
            },
        ]

    def background_id_at(self, offset: int) -> str:
        start = max(4, self.seed_rows // 2)
        span = max(1, self.seed_rows - start)
        idx = start + (offset % span)
        return f"PT-{idx:06d}"

    def make_background_update_payload(
        self, worker_id: int, seq: int
    ) -> tuple[str, dict[str, Any]]:
        return (
            "PATCH",
            {"notes": f"bg-worker-{worker_id}-op-{seq}", "city": "cambridge"},
        )

    def make_background_create_payload(
        self, worker_id: int, seq: int
    ) -> dict[str, Any]:
        return {
            "fname": "bg",
            "lname": "worker",
            "email": f"bg.w{worker_id}.s{seq}@clinic.example",
            "street": "100 health way",
            "city": "boston",
            "state": "Massachusetts",
            "notes": f"bg-created-{worker_id}-{seq}",
        }

    def apply_concurrent_race_mutation(
        self, entity_id: str, actor: str = "concurrent-clinician"
    ) -> None:
        self._handle_item_write(
            method="PATCH",
            entity_id=entity_id,
            body={"notes": "STAT concurrent clinician addendum"},
            actor=actor,
            if_match=None,
        )


class ReplaceTaskServer(BaseSorServer):
    """Replace-style ERP/task System-of-Record archetype (/api/tasks)."""

    app_name = "replace_task_erp"
    entity_name = "task"
    collection_name = "tasks"
    table_name = "tasks"
    confusable_primary_id = "TSK-0O1l8"
    confusable_sibling_id = "TSK-0O118"

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                external_ref TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                description TEXT NOT NULL,
                assignee TEXT NOT NULL,
                due_date TEXT NOT NULL,
                labels TEXT NOT NULL,
                budget_cents INTEGER NOT NULL,
                department TEXT NOT NULL,
                version INTEGER NOT NULL,
                etag TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_author TEXT NOT NULL
            );
            """
        )

    def _create_indexes(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_external_ref ON tasks(external_ref);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee);"
        )

    def _seed_insert_sql(self) -> str:
        return (
            "INSERT INTO tasks "
            "(id, external_ref, title, status, description, assignee, due_date, "
            "labels, budget_cents, department, version, etag, updated_at, last_author) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);"
        )

    def _generate_seed_rows(self, count: int) -> Iterator[tuple[Any, ...]]:
        ts = "2026-11-01T00:00:00.000000Z"
        special = (
            (
                "TSK-0O1l8",
                "REF-000000",
                "Q4 Formulary Reconciliation",
                "open",
                "Reconcile inpatient pharmacy formulary codes prior to audit.",
                "Bo Li",
                "2026-12-15",
                "regulated,pharmacy,q4",
                125000,
                "PHARMACY_OPS",
                1,
                '"TSK-0O1l8:v1"',
                ts,
                "seed",
            ),
            (
                "TSK-0O118",
                "REF-000001",
                "Q4 Formulary Sibling Check",
                "open",
                "Sibling ERP record with confusable identifier.",
                "Bo Lin",
                "2026-12-16",
                "regulated,sibling",
                95000,
                "PHARMACY_OPS",
                1,
                '"TSK-0O118:v1"',
                ts,
                "seed",
            ),
            (
                "TSK-000002",
                "REF-000002",
                "Cold-Chain Sensor Calibration",
                "in_progress",
                "Verify biologics cold-storage telemetry thresholds.",
                "Amy Wu",
                "2026-12-20",
                "cold-chain,qa",
                240000,
                "SUPPLY_CHAIN",
                1,
                '"TSK-000002:v1"',
                ts,
                "seed",
            ),
            (
                "TSK-000003",
                "REF-000003",
                "Vendor Rebate Settlement",
                "open",
                "Settle quarterly contract rebate ledger lines.",
                "Al Ng",
                "2026-12-31",
                "finance,rebate",
                510000,
                "FINANCE_ERP",
                1,
                '"TSK-000003:v1"',
                ts,
                "seed",
            ),
        )
        for row in special:
            yield row

        assignees = (
            "Bo Li",
            "Amy Wu",
            "Al Ng",
            "Elena Chen",
            "Marcus Patel",
            "Jo Kim",
            "Priya Shah",
            "Ty Yu",
        )
        depts = ("PHARMACY_OPS", "SUPPLY_CHAIN", "FINANCE_ERP", "CLINICAL_QA")
        statuses = ("open", "in_progress", "review", "open")

        for i in range(4, count):
            tid = f"TSK-{i:06d}"
            yield (
                tid,
                f"REF-{i:06d}",
                f"ERP Procurement Workorder #{i}",
                statuses[i & 3],
                f"Mandatory compliance workorder description for item {i}.",
                assignees[i & 7],
                f"2026-12-{(i % 28) + 1:02d}",
                "erp,regulated,active",
                50000 + ((i % 25) * 10000),
                depts[i & 3],
                1,
                f'"{tid}:v1"',
                ts,
                "seed",
            )

    def _allowed_filter_columns(self) -> tuple[str, ...]:
        return ("external_ref", "assignee", "title")

    def _upsert_raw_record(
        self, conn: sqlite3.Connection, record: Mapping[str, Any]
    ) -> None:
        conn.execute(
            """
            INSERT OR REPLACE INTO tasks
            (id, external_ref, title, status, description, assignee, due_date,
             labels, budget_cents, department, version, etag, updated_at, last_author)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                record["id"],
                record["external_ref"],
                record["title"],
                record["status"],
                record["description"],
                record["assignee"],
                record["due_date"],
                record["labels"],
                int(record["budget_cents"]),
                record["department"],
                int(record["version"]),
                record["etag"],
                record["updated_at"],
                record["last_author"],
            ),
        )

    def _handle_create(
        self,
        *,
        body: Mapping[str, Any],
        actor: str,
    ) -> tuple[int, dict[str, str], dict[str, Any], int]:
        # Non-idempotent POST: every call allocates a fresh task ID and inserts a row.
        tid = self.next_entity_id("TSK")
        version = 1
        etag = f'"{tid}:v{version}"'
        labels_val = body.get("labels", "")
        if isinstance(labels_val, list):
            labels_str = ",".join(str(x) for x in labels_val)
        else:
            labels_str = str(labels_val)
        record: dict[str, Any] = {
            "id": tid,
            "external_ref": str(body.get("external_ref", f"REF-{tid}")),
            "title": str(body.get("title", "")),
            "status": str(body.get("status", "open")),
            "description": str(body.get("description", "")),
            "assignee": str(body.get("assignee", "")),
            "due_date": str(body.get("due_date", "")),
            "labels": labels_str,
            "budget_cents": int(body.get("budget_cents", 0) or 0),
            "department": str(body.get("department", "")),
            "version": version,
            "etag": etag,
            "updated_at": utc_iso_now(),
            "last_author": actor,
        }
        with self._write_lock:
            conn = self._conn()
            conn.execute("BEGIN IMMEDIATE;")
            try:
                self._upsert_raw_record(conn, record)
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
        return 201, {"ETag": etag}, record, 1

    def _handle_item_write(
        self,
        *,
        method: str,
        entity_id: str,
        body: Mapping[str, Any],
        actor: str,
        if_match: Optional[str],
    ) -> tuple[int, dict[str, str], dict[str, Any], int]:
        if method != "PUT":
            return 405, {}, {"error": "use_put_for_tasks"}, 0

        labels_raw = body.get("labels", "")
        if isinstance(labels_raw, list):
            labels_str = ",".join(str(x) for x in labels_raw)
        else:
            labels_str = str(labels_raw) if labels_raw is not None else ""

        with self._write_lock:
            conn = self._conn()
            conn.execute("BEGIN IMMEDIATE;")
            try:
                row = conn.execute(
                    "SELECT * FROM tasks WHERE id = ?;",
                    (entity_id,),
                ).fetchone()
                if row is None:
                    conn.execute("ROLLBACK;")
                    return 404, {}, {"error": "not_found", "id": entity_id}, 0
                current = dict(row)
                if if_match is not None and not _etag_matches(
                    if_match,
                    str(current["etag"]),
                    int(current["version"]),
                ):
                    conn.execute("ROLLBACK;")
                    return (
                        412,
                        {"ETag": str(current["etag"])},
                        {"error": "precondition_failed", "current": current},
                        0,
                    )

                new_ver = int(current["version"]) + 1
                new_etag = f'"{entity_id}:v{new_ver}"'
                # Replace-style PUT quirk: omitted fields silently overwritten with "" or 0!
                replaced: dict[str, Any] = {
                    "id": entity_id,
                    "external_ref": str(body.get("external_ref", "")),
                    "title": str(body.get("title", "")),
                    "status": str(body.get("status", "")),
                    "description": str(body.get("description", "")),
                    "assignee": str(body.get("assignee", "")),
                    "due_date": str(body.get("due_date", "")),
                    "labels": labels_str,
                    "budget_cents": int(body.get("budget_cents", 0) or 0),
                    "department": str(body.get("department", "")),
                    "version": new_ver,
                    "etag": new_etag,
                    "updated_at": utc_iso_now(),
                    "last_author": actor,
                }
                self._upsert_raw_record(conn, replaced)
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
        return 200, {"ETag": new_etag}, replaced, 1

    def lens_spec(self, base_url: Optional[str] = None) -> EntityLensSpec:
        resolved_base = base_url or self.base_url
        return EntityLensSpec(
            entity_name="task",
            base_url=resolved_base,
            id_field="id",
            write_mode="replace",
            get_one=EndpointTemplate(
                method="GET",
                path_template="/api/tasks/{id}",
                id_field="id",
                etag_header="ETag",
                version_field="version",
                author_field="last_author",
            ),
            write_endpoint=EndpointTemplate(
                method="PUT",
                path_template="/api/tasks/{id}",
                id_field="id",
                etag_header="ETag",
                version_field="version",
                author_field="last_author",
            ),
            create_endpoint=EndpointTemplate(
                method="POST",
                path_template="/api/tasks",
                id_field="id",
                etag_header="ETag",
                version_field="version",
                author_field="last_author",
            ),
            duplicate_search=EndpointTemplate(
                method="GET",
                path_template="/api/tasks",
                query_params={"external_ref": "{external_ref}"},
                records_key="items",
                id_field="id",
            ),
            delete_endpoint=EndpointTemplate(
                method="DELETE",
                path_template="/api/tasks/{id}",
                id_field="id",
            ),
            identity_fields=("external_ref",),
            volatile_fields=("updated_at", "etag", "version", "last_author"),
            field_codecs={},
            duplicate_keys=("external_ref",),
        )

    def sample_har_entries(
        self, base_url: Optional[str] = None
    ) -> list[dict[str, Any]]:
        b = base_url or self.base_url
        r0 = self.get_record_direct("TSK-0O1l8") or {}
        r1 = {**r0, "status": "completed", "version": 2}
        return [
            {
                "request": {"method": "GET", "url": f"{b}/api/tasks/TSK-0O1l8"},
                "response": {
                    "status": 200,
                    "headers": [{"name": "ETag", "value": '"TSK-0O1l8:v1"'}],
                    "content": {"text": json.dumps(r0)},
                },
            },
            {
                "request": {
                    "method": "PUT",
                    "url": f"{b}/api/tasks/TSK-0O1l8",
                    "postData": {"text": json.dumps(r1)},
                },
                "response": {
                    "status": 200,
                    "headers": [{"name": "ETag", "value": '"TSK-0O1l8:v2"'}],
                    "content": {"text": json.dumps(r1)},
                },
            },
        ]

    def background_id_at(self, offset: int) -> str:
        start = max(4, self.seed_rows // 2)
        span = max(1, self.seed_rows - start)
        idx = start + (offset % span)
        return f"TSK-{idx:06d}"

    def make_background_update_payload(
        self, worker_id: int, seq: int
    ) -> tuple[str, dict[str, Any]]:
        return (
            "PUT",
            {
                "external_ref": f"BG-UPD-{worker_id}-{seq}",
                "title": f"Background ERP Task {worker_id}",
                "status": "in_progress",
                "description": f"Updated by bg-worker-{worker_id} op {seq}",
                "assignee": f"worker-{worker_id}",
                "due_date": "2026-12-28",
                "labels": "bg,erp",
                "budget_cents": 75000,
                "department": "SUPPLY_CHAIN",
            },
        )

    def make_background_create_payload(
        self, worker_id: int, seq: int
    ) -> dict[str, Any]:
        return {
            "external_ref": f"BG-REF-W{worker_id}-S{seq}",
            "title": f"Background Created Task {worker_id}-{seq}",
            "status": "open",
            "description": "Concurrent ERP background task creation",
            "assignee": f"worker-{worker_id}",
            "due_date": "2026-12-30",
            "labels": "bg,created",
            "budget_cents": 40000,
            "department": "FINANCE_ERP",
        }

    def apply_concurrent_race_mutation(
        self, entity_id: str, actor: str = "concurrent-erp-controller"
    ) -> None:
        current = self.get_record_direct(entity_id)
        if current is None:
            return
        merged = dict(current)
        merged["description"] = "URGENT concurrent controller hold added"
        self._handle_item_write(
            method="PUT",
            entity_id=entity_id,
            body=merged,
            actor=actor,
            if_match=None,
        )


class ClaimsLedgerServer(BaseSorServer):
    """Held-out regulated claims ledger System-of-Record archetype (/api/claims)."""

    app_name = "claims_ledger"
    entity_name = "claim"
    collection_name = "claims"
    table_name = "claims"
    confusable_primary_id = "CLM-0O1l8"
    confusable_sibling_id = "CLM-0O118"

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS claims (
                id TEXT PRIMARY KEY,
                claim_number TEXT NOT NULL,
                policy_id TEXT NOT NULL,
                claimant_name TEXT NOT NULL,
                member_phone TEXT NOT NULL,
                service_date TEXT NOT NULL,
                status TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,
                diagnosis_code TEXT NOT NULL,
                adjudication_note TEXT NOT NULL,
                version INTEGER NOT NULL,
                audit_seq INTEGER NOT NULL,
                etag TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_author TEXT NOT NULL
            );
            """
        )

    def _create_indexes(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_claims_claim_number ON claims(claim_number);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_claims_policy_id ON claims(policy_id);"
        )

    def _seed_insert_sql(self) -> str:
        return (
            "INSERT INTO claims "
            "(id, claim_number, policy_id, claimant_name, member_phone, service_date, "
            "status, amount_cents, diagnosis_code, adjudication_note, version, "
            "audit_seq, etag, updated_at, last_author) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);"
        )

    def _generate_seed_rows(self, count: int) -> Iterator[tuple[Any, ...]]:
        ts = "2026-11-01T00:00:00.000000Z"
        special = (
            (
                "CLM-0O1l8",
                "CN-2026-000000",
                "POL-100000",
                "Bo Li",
                "+14165550100",
                "2026-10-15T00:00:00Z",
                "submitted",
                45000,
                "J06.9",
                "Intake validated against policy schedule",
                1,
                1,
                '"CLM-0O1l8:v1"',
                ts,
                "seed",
            ),
            (
                "CLM-0O118",
                "CN-2026-000001",
                "POL-100001",
                "Bo Lin",
                "+14165550101",
                "2026-10-16T00:00:00Z",
                "submitted",
                82000,
                "I10",
                "Confusable sibling ledger entry",
                1,
                2,
                '"CLM-0O118:v1"',
                ts,
                "seed",
            ),
            (
                "CLM-000002",
                "CN-2026-000002",
                "POL-100002",
                "Amy Wu",
                "+16175550102",
                "2026-10-20T00:00:00Z",
                "pending_review",
                135000,
                "E11.9",
                "Awaiting specialist attestation",
                1,
                3,
                '"CLM-000002:v1"',
                ts,
                "seed",
            ),
            (
                "CLM-000003",
                "CN-2026-000003",
                "POL-100003",
                "Al Ng",
                "+14155550103",
                "2026-10-22T00:00:00Z",
                "submitted",
                64000,
                "M54.5",
                "Physical therapy claim line",
                1,
                4,
                '"CLM-000003:v1"',
                ts,
                "seed",
            ),
        )
        for row in special:
            yield row

        claimants = (
            "Bo Li",
            "Amy Wu",
            "Al Ng",
            "Elena Chen",
            "Marcus Patel",
            "Jo Kim",
            "Priya Shah",
            "Ty Yu",
        )
        dx_codes = ("J06.9", "I10", "E11.9", "M54.5")
        statuses = ("submitted", "pending_review", "adjudicated", "submitted")

        for i in range(4, count):
            cid = f"CLM-{i:06d}"
            yield (
                cid,
                f"CN-2026-{i:06d}",
                f"POL-{100000 + i}",
                claimants[i & 7],
                f"+1416555{i % 10000:04d}",
                f"2026-10-{(i % 28) + 1:02d}T00:00:00Z",
                statuses[i & 3],
                25000 + ((i % 40) * 2500),
                dx_codes[i & 3],
                "Standard regulated claim ledger entry",
                1,
                i + 1,
                f'"{cid}:v1"',
                ts,
                "seed",
            )

    def _allowed_filter_columns(self) -> tuple[str, ...]:
        return ("claim_number", "policy_id", "member_phone")

    def _normalize_filter_param(self, column: str, value: str) -> str:
        if column == "member_phone":
            return normalize_phone_e164(value)
        return value

    def _upsert_raw_record(
        self, conn: sqlite3.Connection, record: Mapping[str, Any]
    ) -> None:
        conn.execute(
            """
            INSERT OR REPLACE INTO claims
            (id, claim_number, policy_id, claimant_name, member_phone, service_date,
             status, amount_cents, diagnosis_code, adjudication_note, version,
             audit_seq, etag, updated_at, last_author)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                record["id"],
                record["claim_number"],
                record["policy_id"],
                record["claimant_name"],
                record["member_phone"],
                record["service_date"],
                record["status"],
                int(record["amount_cents"]),
                record["diagnosis_code"],
                record["adjudication_note"],
                int(record["version"]),
                int(record["audit_seq"]),
                record["etag"],
                record["updated_at"],
                record["last_author"],
            ),
        )

    def _normalize_claim_fields(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        out = dict(fields)
        if "member_phone" in out and out["member_phone"] is not None:
            out["member_phone"] = normalize_phone_e164(str(out["member_phone"]))
        if "service_date" in out and out["service_date"] is not None:
            out["service_date"] = normalize_iso_date_utc(str(out["service_date"]))
        if "claimant_name" in out and out["claimant_name"] is not None:
            out["claimant_name"] = normalize_title_words(str(out["claimant_name"]))
        return out

    def _handle_create(
        self,
        *,
        body: Mapping[str, Any],
        actor: str,
    ) -> tuple[int, dict[str, str], dict[str, Any], int]:
        norm = self._normalize_claim_fields(body)
        cid = str(norm.get("id") or self.next_entity_id("CLM"))
        version = 1
        etag = f'"{cid}:v{version}"'
        audit_seq = self.next_audit_seq()
        record: dict[str, Any] = {
            "id": cid,
            "claim_number": str(norm.get("claim_number", f"CN-2026-{cid}")),
            "policy_id": str(norm.get("policy_id", "POL-999999")),
            "claimant_name": str(norm.get("claimant_name", "")),
            "member_phone": str(norm.get("member_phone", "+14165550199")),
            "service_date": str(norm.get("service_date", "2026-11-19T00:00:00Z")),
            "status": str(norm.get("status", "submitted")),
            "amount_cents": int(norm.get("amount_cents", 0) or 0),
            "diagnosis_code": str(norm.get("diagnosis_code", "Z00.00")),
            "adjudication_note": str(norm.get("adjudication_note", "")),
            "version": version,
            "audit_seq": audit_seq,
            "etag": etag,
            "updated_at": utc_iso_now(),
            "last_author": actor,
        }
        with self._write_lock:
            conn = self._conn()
            conn.execute("BEGIN IMMEDIATE;")
            try:
                self._upsert_raw_record(conn, record)
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
        return 201, {"ETag": etag}, record, 1

    def _handle_item_write(
        self,
        *,
        method: str,
        entity_id: str,
        body: Mapping[str, Any],
        actor: str,
        if_match: Optional[str],
    ) -> tuple[int, dict[str, str], dict[str, Any], int]:
        if method != "PATCH":
            return 405, {}, {"error": "use_patch_for_claims"}, 0
        norm = self._normalize_claim_fields(body)
        mutable_str = (
            "claim_number",
            "policy_id",
            "claimant_name",
            "member_phone",
            "service_date",
            "status",
            "diagnosis_code",
            "adjudication_note",
        )
        with self._write_lock:
            conn = self._conn()
            conn.execute("BEGIN IMMEDIATE;")
            try:
                row = conn.execute(
                    "SELECT * FROM claims WHERE id = ?;",
                    (entity_id,),
                ).fetchone()
                if row is None:
                    conn.execute("ROLLBACK;")
                    return 404, {}, {"error": "not_found", "id": entity_id}, 0
                current = dict(row)
                if if_match is not None and not _etag_matches(
                    if_match,
                    str(current["etag"]),
                    int(current["version"]),
                ):
                    conn.execute("ROLLBACK;")
                    return (
                        412,
                        {"ETag": str(current["etag"])},
                        {"error": "precondition_failed", "current": current},
                        0,
                    )
                updated = dict(current)
                for key in mutable_str:
                    if key in norm:
                        updated[key] = str(norm[key])
                if "amount_cents" in norm:
                    updated["amount_cents"] = int(norm["amount_cents"])
                new_ver = int(current["version"]) + 1
                new_etag = f'"{entity_id}:v{new_ver}"'
                updated["version"] = new_ver
                updated["audit_seq"] = self.next_audit_seq()
                updated["etag"] = new_etag
                updated["updated_at"] = utc_iso_now()
                updated["last_author"] = actor
                self._upsert_raw_record(conn, updated)
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
        return 200, {"ETag": new_etag}, updated, 1

    def lens_spec(self, base_url: Optional[str] = None) -> EntityLensSpec:
        resolved_base = base_url or self.base_url
        return EntityLensSpec(
            entity_name="claim",
            base_url=resolved_base,
            id_field="id",
            write_mode="patch",
            get_one=EndpointTemplate(
                method="GET",
                path_template="/api/claims/{id}",
                id_field="id",
                etag_header="ETag",
                version_field="version",
                author_field="last_author",
            ),
            write_endpoint=EndpointTemplate(
                method="PATCH",
                path_template="/api/claims/{id}",
                id_field="id",
                etag_header="ETag",
                version_field="version",
                author_field="last_author",
            ),
            create_endpoint=EndpointTemplate(
                method="POST",
                path_template="/api/claims",
                id_field="id",
                etag_header="ETag",
                version_field="version",
                author_field="last_author",
            ),
            duplicate_search=EndpointTemplate(
                method="GET",
                path_template="/api/claims",
                query_params={"claim_number": "{claim_number}"},
                records_key="items",
                id_field="id",
            ),
            delete_endpoint=EndpointTemplate(
                method="DELETE",
                path_template="/api/claims/{id}",
                id_field="id",
            ),
            identity_fields=("claim_number",),
            volatile_fields=(
                "updated_at",
                "etag",
                "version",
                "audit_seq",
                "last_author",
            ),
            field_codecs={
                "member_phone": FieldCodec(kind="phone_digits"),
                "service_date": FieldCodec(kind="iso_date"),
                "claimant_name": FieldCodec(kind="capitalize_words"),
            },
            duplicate_keys=("claim_number",),
        )

    def sample_har_entries(
        self, base_url: Optional[str] = None
    ) -> list[dict[str, Any]]:
        b = base_url or self.base_url
        r0 = self.get_record_direct("CLM-0O1l8") or {}
        return [
            {
                "request": {"method": "GET", "url": f"{b}/api/claims/CLM-0O1l8"},
                "response": {
                    "status": 200,
                    "headers": [{"name": "ETag", "value": '"CLM-0O1l8:v1"'}],
                    "content": {"text": json.dumps(r0)},
                },
            },
            {
                "request": {
                    "method": "PATCH",
                    "url": f"{b}/api/claims/CLM-0O1l8",
                    "postData": {
                        "text": json.dumps(
                            {
                                "member_phone": "(416) 555-0199",
                                "service_date": "2026-11-19",
                            }
                        )
                    },
                },
                "response": {
                    "status": 200,
                    "headers": [{"name": "ETag", "value": '"CLM-0O1l8:v2"'}],
                    "content": {
                        "text": json.dumps(
                            {
                                **r0,
                                "member_phone": "+14165550199",
                                "service_date": "2026-11-19T00:00:00Z",
                                "version": 2,
                            }
                        )
                    },
                },
            },
        ]

    def background_id_at(self, offset: int) -> str:
        start = max(4, self.seed_rows // 2)
        span = max(1, self.seed_rows - start)
        idx = start + (offset % span)
        return f"CLM-{idx:06d}"

    def make_background_update_payload(
        self, worker_id: int, seq: int
    ) -> tuple[str, dict[str, Any]]:
        return (
            "PATCH",
            {
                "adjudication_note": f"bg-ledger-worker-{worker_id}-op-{seq}",
                "member_phone": f"(416) 555-{seq % 10000:04d}",
            },
        )

    def make_background_create_payload(
        self, worker_id: int, seq: int
    ) -> dict[str, Any]:
        return {
            "claim_number": f"BG-CN-W{worker_id}-S{seq}",
            "policy_id": f"POL-BG-{worker_id}",
            "claimant_name": "bg member",
            "member_phone": "(416) 555-0199",
            "service_date": "2026-11-19",
            "status": "submitted",
            "amount_cents": 31000,
            "diagnosis_code": "J06.9",
            "adjudication_note": f"bg-created-{worker_id}-{seq}",
        }

    def apply_concurrent_race_mutation(
        self, entity_id: str, actor: str = "concurrent-auditor"
    ) -> None:
        self._handle_item_write(
            method="PATCH",
            entity_id=entity_id,
            body={"adjudication_note": "CONCURRENT regulatory auditor hold"},
            actor=actor,
            if_match=None,
        )


class BackgroundTrafficGenerator:
    """Runs N concurrent background HTTP writers performing creates & updates on non-target rows."""

    def __init__(
        self,
        server: BaseSorServer,
        num_workers: int = 20,
        *,
        interval_s: float = 0.002,
    ) -> None:
        if num_workers < 0:
            raise ValueError("num_workers must be >= 0")
        self.server = server
        self.num_workers = num_workers
        self.interval_s = interval_s

        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self.ops_completed: int = 0
        self.creates_completed: int = 0
        self.updates_completed: int = 0
        self.errors: list[str] = []

    def _worker_loop(self, worker_id: int) -> None:
        conn: Optional[http.client.HTTPConnection] = None
        seq = 0
        col_path = self.server.collection_path
        while not self._stop_event.is_set():
            try:
                if conn is None:
                    conn = http.client.HTTPConnection(
                        self.server.host,
                        self.server.port,
                        timeout=10.0,
                    )
                is_create = (seq % 5) == 4
                headers = {
                    "Content-Type": "application/json",
                    "X-Actor-Id": f"bg-worker-{worker_id}",
                }
                if is_create:
                    payload = self.server.make_background_create_payload(worker_id, seq)
                    body_bytes = json.dumps(payload, separators=(",", ":")).encode(
                        "utf-8"
                    )
                    conn.request("POST", col_path, body=body_bytes, headers=headers)
                else:
                    target_id = self.server.background_id_at(worker_id * 1024 + seq)
                    method, payload = self.server.make_background_update_payload(
                        worker_id, seq
                    )
                    body_bytes = json.dumps(payload, separators=(",", ":")).encode(
                        "utf-8"
                    )
                    conn.request(
                        method,
                        f"{col_path}/{target_id}",
                        body=body_bytes,
                        headers=headers,
                    )

                resp = conn.getresponse()
                _ = resp.read()
                if 200 <= resp.status < 300:
                    with self._lock:
                        self.ops_completed += 1
                        if is_create:
                            self.creates_completed += 1
                        else:
                            self.updates_completed += 1
                else:
                    with self._lock:
                        if len(self.errors) < 50:
                            self.errors.append(
                                f"HTTP {resp.status} in worker {worker_id}"
                            )
                seq += 1
            except Exception as exc:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = None
                if not self._stop_event.is_set():
                    with self._lock:
                        if len(self.errors) < 50:
                            self.errors.append(f"{type(exc).__name__}: {exc}")
            if self.interval_s > 0 and not self._stop_event.is_set():
                self._stop_event.wait(self.interval_s)

        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def start(self) -> "BackgroundTrafficGenerator":
        self._stop_event.clear()
        self._threads = [
            threading.Thread(
                target=self._worker_loop,
                args=(w,),
                name=f"{self.server.app_name}-bg-{w}",
                daemon=True,
            )
            for w in range(self.num_workers)
        ]
        for t in self._threads:
            t.start()
        return self

    def stop(self) -> None:
        self._stop_event.set()
        for t in self._threads:
            t.join(timeout=3.0)
        self._threads.clear()

    def __enter__(self) -> "BackgroundTrafficGenerator":
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.stop()


@dataclass(frozen=True)
class FaultConfig:
    """Declarative write-path fault injection configuration."""

    fault_kind: Optional[FaultKind] = None
    collateral_id: Optional[str] = None
    wrong_target_id: Optional[str] = None


def _corrupt_field_value(key: str, value: Any) -> Any:
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 77777
    if key == "state":
        return "ZZ"
    if "phone" in key:
        return "+19990009999"
    if "date" in key:
        return "1999-01-01"
    if key == "email":
        return f"corrupted.{value}"
    return f"{value}_CORRUPTED"


class FaultInjectingProxy:
    """HTTP reverse proxy capable of injecting all 6 write-path faults + OCC race."""

    def __init__(
        self,
        upstream_url: str,
        *,
        server: Optional[BaseSorServer] = None,
        fault: Optional[FaultConfig] = None,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self._lock = threading.Lock()
        self._upstream_url = upstream_url.rstrip("/")
        self.server = server
        self._fault = fault or FaultConfig()
        self.host = host
        self._requested_port = port

        self.faults_injected: int = 0
        self.requests_proxied: int = 0
        self._httpd: Optional[_FastThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.port: int = 0

    @property
    def base_url(self) -> str:
        if self._httpd is None or self.port == 0:
            raise RuntimeError("FaultInjectingProxy has not been started")
        return f"http://{self.host}:{self.port}"

    def set_fault(self, fault: Optional[FaultConfig]) -> None:
        with self._lock:
            self._fault = fault or FaultConfig()

    def set_upstream(self, upstream_url: str) -> None:
        with self._lock:
            self._upstream_url = upstream_url.rstrip("/")

    def _get_state(self) -> tuple[str, FaultConfig]:
        with self._lock:
            return self._upstream_url, self._fault

    def _forward_once(
        self,
        *,
        upstream_url: str,
        method: str,
        path_and_query: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> tuple[int, dict[str, str], bytes]:
        parts = urlsplit(upstream_url)
        host = parts.hostname or "127.0.0.1"
        port = parts.port or 80
        conn = http.client.HTTPConnection(host, port, timeout=15.0)
        try:
            fwd_headers = {
                k: v
                for k, v in headers.items()
                if k.lower()
                not in ("host", "content-length", "connection", "transfer-encoding")
            }
            fwd_headers["Content-Length"] = str(len(body))
            conn.request(method, path_and_query, body=body, headers=fwd_headers)
            resp = conn.getresponse()
            resp_body = resp.read()
            resp_headers = {
                k: v
                for k, v in resp.getheaders()
                if k.lower()
                not in (
                    "content-length",
                    "transfer-encoding",
                    "connection",
                    "date",
                    "server",
                )
            }
            return resp.status, resp_headers, resp_body
        finally:
            conn.close()

    def _handle_proxy_request(
        self,
        *,
        method: str,
        raw_path: str,
        headers: Mapping[str, str],
        raw_body: bytes,
    ) -> tuple[int, dict[str, str], bytes]:
        upstream_url, fault_cfg = self._get_state()
        with self._lock:
            self.requests_proxied += 1

        fk = fault_cfg.fault_kind
        is_mutation = method in ("POST", "PUT", "PATCH")
        if fk is None or not is_mutation:
            return self._forward_once(
                upstream_url=upstream_url,
                method=method,
                path_and_query=raw_path,
                headers=headers,
                body=raw_body,
            )

        with self._lock:
            self.faults_injected += 1

        parsed = urlparse(raw_path)
        segments = [s for s in parsed.path.split("/") if s]
        collection = (
            segments[1] if len(segments) >= 2 and segments[0] == "api" else "items"
        )
        entity_id = segments[2] if len(segments) >= 3 and segments[0] == "api" else None

        body_obj: dict[str, Any] = {}
        if raw_body:
            try:
                loaded = json.loads(raw_body.decode("utf-8"))
                if isinstance(loaded, dict):
                    body_obj = loaded
            except json.JSONDecodeError:
                body_obj = {}

        if fk == "silent_drop_200_ok":
            drop_headers = dict(headers)
            drop_headers["X-Fault-Silent-Drop"] = "1"
            return self._forward_once(
                upstream_url=upstream_url,
                method=method,
                path_and_query=raw_path,
                headers=drop_headers,
                body=raw_body,
            )

        if fk == "partial_put_clobber":
            mut_method = method
            clobber_body = dict(body_obj)
            if collection == "tasks":
                mut_method = "PUT"
                # Strip payload down to a partial single-field delta so PUT clobbers frame fields!
                if "status" in clobber_body:
                    clobber_body = {"status": clobber_body["status"]}
                elif clobber_body:
                    first_k = next(iter(clobber_body))
                    clobber_body = {first_k: clobber_body[first_k]}
                else:
                    clobber_body = {"status": "completed"}
            elif collection == "patients":
                clobber_body["allergies"] = ""
                clobber_body["notes"] = ""
            elif collection == "claims":
                clobber_body["diagnosis_code"] = ""
                clobber_body["adjudication_note"] = ""
            else:
                clobber_body = {}
            encoded = json.dumps(clobber_body, separators=(",", ":")).encode("utf-8")
            return self._forward_once(
                upstream_url=upstream_url,
                method=mut_method,
                path_and_query=raw_path,
                headers=headers,
                body=encoded,
            )

        if fk == "wrong_value_mutation":
            mutated = dict(body_obj)
            skip_keys = {
                "id",
                "version",
                "etag",
                "updated_at",
                "last_author",
                "audit_seq",
            }
            candidate_keys = [k for k in mutated if k not in skip_keys]
            if candidate_keys:
                # Mutate the last delta key (or status/city/amount_cents if present)
                target_k = candidate_keys[-1]
                for preferred in (
                    "status",
                    "amount_cents",
                    "budget_cents",
                    "street",
                    "state",
                    "member_phone",
                    "title",
                    "fname",
                ):
                    if preferred in mutated:
                        target_k = preferred
                        break
                mutated[target_k] = _corrupt_field_value(target_k, mutated[target_k])
            encoded = json.dumps(mutated, separators=(",", ":")).encode("utf-8")
            return self._forward_once(
                upstream_url=upstream_url,
                method=method,
                path_and_query=raw_path,
                headers=headers,
                body=encoded,
            )

        if fk == "duplicate_post_on_retry":
            # Forward POST twice to simulate unguarded network retry creating duplicate rows
            _ = self._forward_once(
                upstream_url=upstream_url,
                method=method,
                path_and_query=raw_path,
                headers=headers,
                body=raw_body,
            )
            return self._forward_once(
                upstream_url=upstream_url,
                method=method,
                path_and_query=raw_path,
                headers=headers,
                body=raw_body,
            )

        if fk == "collateral_extra_write":
            primary_res = self._forward_once(
                upstream_url=upstream_url,
                method=method,
                path_and_query=raw_path,
                headers=headers,
                body=raw_body,
            )
            default_collateral = {
                "patients": "PT-000050",
                "tasks": "TSK-000050",
                "claims": "CLM-000050",
            }.get(collection, "ENT-000050")
            col_id = fault_cfg.collateral_id or default_collateral
            col_method = "PUT" if collection == "tasks" else "PATCH"
            col_payload = {
                "patients": {"notes": "UNAUTHORIZED_COLLATERAL_EDIT"},
                "tasks": {
                    "title": "UNAUTHORIZED_COLLATERAL_EDIT",
                    "status": "clobbered",
                },
                "claims": {"adjudication_note": "UNAUTHORIZED_COLLATERAL_EDIT"},
            }.get(collection, {"notes": "UNAUTHORIZED_COLLATERAL_EDIT"})
            col_headers = {k: v for k, v in headers.items() if k.lower() != "if-match"}
            _ = self._forward_once(
                upstream_url=upstream_url,
                method=col_method,
                path_and_query=f"/api/{collection}/{col_id}",
                headers=col_headers,
                body=json.dumps(col_payload, separators=(",", ":")).encode("utf-8"),
            )
            return primary_res

        if fk == "wrong_target_record":
            default_wrong = {
                "patients": "PT-0O118",
                "tasks": "TSK-0O118",
                "claims": "CLM-0O118",
            }.get(collection, "ENT-0O118")
            wrong_id = fault_cfg.wrong_target_id or default_wrong
            if wrong_id == entity_id:
                wrong_id = {
                    "patients": "PT-000002",
                    "tasks": "TSK-000002",
                    "claims": "CLM-000002",
                }.get(collection, "ENT-000002")
            wrong_path = f"/api/{collection}/{wrong_id}"
            if parsed.query:
                wrong_path = f"{wrong_path}?{parsed.query}"
            stripped_headers = {
                k: v for k, v in headers.items() if k.lower() != "if-match"
            }
            return self._forward_once(
                upstream_url=upstream_url,
                method=method,
                path_and_query=wrong_path,
                headers=stripped_headers,
                body=raw_body,
            )

        if fk == "same_record_concurrent_race":
            if self.server is not None and entity_id is not None:
                self.server.apply_concurrent_race_mutation(entity_id)
            return self._forward_once(
                upstream_url=upstream_url,
                method=method,
                path_and_query=raw_path,
                headers=headers,
                body=raw_body,
            )

        return self._forward_once(
            upstream_url=upstream_url,
            method=method,
            path_and_query=raw_path,
            headers=headers,
            body=raw_body,
        )

    def start(self) -> "FaultInjectingProxy":
        if self._httpd is not None:
            return self
        proxy_ref = self

        class _ProxyHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def _dispatch(self, method: str) -> None:
                content_len = int(self.headers.get("Content-Length", "0") or "0")
                raw_body = self.rfile.read(content_len) if content_len > 0 else b""
                req_headers = {k: v for k, v in self.headers.items()}
                status, resp_headers, resp_body = proxy_ref._handle_proxy_request(
                    method=method,
                    raw_path=self.path,
                    headers=req_headers,
                    raw_body=raw_body,
                )
                self.send_response(status)
                self.send_header("Content-Length", str(len(resp_body)))
                for hk, hv in resp_headers.items():
                    if any(ch in str(hk) + str(hv) for ch in "\r\n"):
                        raise ValueError("invalid HTTP response header")
                    self.send_header(
                        str(hk).replace("\r", "").replace("\n", ""),
                        str(hv).replace("\r", "").replace("\n", ""),
                    )
                self.end_headers()
                self.wfile.write(resp_body)

            def do_GET(self) -> None:
                self._dispatch("GET")

            def do_POST(self) -> None:
                self._dispatch("POST")

            def do_PUT(self) -> None:
                self._dispatch("PUT")

            def do_PATCH(self) -> None:
                self._dispatch("PATCH")

            def do_DELETE(self) -> None:
                self._dispatch("DELETE")

        self._httpd = _FastThreadingHTTPServer(
            (self.host, self._requested_port), _ProxyHandler
        )
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.02},
            name="tx-acid-fault-proxy",
            daemon=True,
        )
        self._thread.start()
        return self

    def close(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    def __enter__(self) -> "FaultInjectingProxy":
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

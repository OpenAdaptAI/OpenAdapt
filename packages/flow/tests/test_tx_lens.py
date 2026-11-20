"""Real multi-threaded HTTP + SQLite tests for ``tx.lens`` and ``tx.receipt``."""

from __future__ import annotations

import json
import sqlite3
import stat
import subprocess
import sys
import textwrap
import threading
import types
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openadapt_flow.tx.lens import (
    EntityLensClient,
    TransactionalCommitEngine,
    deep_merge,
)
from openadapt_flow.tx.receipt import (
    issue_tx_receipt,
    task_key_for,
    verify_tx_receipt,
)
from openadapt_flow.tx.types import (
    TX_RECEIPT_SCHEMA,
    CertifiedLensPack,
    EndpointTemplate,
    EntityLensSpec,
    FieldCodec,
    JudgeVerdict,
    TxTask,
    WireMutationRecord,
    WireSessionSummary,
)

BUSINESS_FIELDS = (
    "first_name",
    "last_name",
    "email",
    "phone",
    "department",
    "role",
    "notes",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class RealSQLiteAppStore:
    """Thread-safe 5,000-row SQLite backing store with ETag/versioning and hooks."""

    def __init__(self, db_path: Path, *, seed_rows: int = 5000) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self.sql_rows_read = 0
        self.sql_writes_executed = 0
        self._next_seq = seed_rows + 1
        self.after_get_hook: Optional[Callable[[str], None]] = None
        self.after_write_hook: Optional[Callable[[str], None]] = None
        self._init_and_seed(seed_rows)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_and_seed(self, seed_rows: int) -> None:
        now = "2026-11-19T00:00:00Z"
        rows = []
        for idx in range(1, seed_rows + 1):
            rec_id = f"rec-{idx:04d}"
            first_name = "Li" if idx == 10 else f"First{idx}"
            last_name = "Chen" if idx == 10 else f"Last{idx}"
            rows.append(
                (
                    rec_id,
                    first_name,
                    last_name,
                    f"person{idx}@example.test",
                    f"555-{idx:04d}",
                    f"Dept-{idx % 12}",
                    f"Role-{idx % 5}",
                    f"Seeded note for {rec_id}",
                    1,
                    "seed",
                    now,
                )
            )
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE records (
                    id TEXT PRIMARY KEY,
                    first_name TEXT,
                    last_name TEXT,
                    email TEXT,
                    phone TEXT,
                    department TEXT,
                    role TEXT,
                    notes TEXT,
                    version INTEGER NOT NULL,
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX idx_records_email ON records(email)")
            conn.executemany(
                """
                INSERT INTO records (
                    id, first_name, last_name, email, phone,
                    department, role, notes, version, updated_by, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()

    def reset_counters(self) -> None:
        with self._lock:
            self.sql_rows_read = 0
            self.sql_writes_executed = 0

    def direct_inspect(self, entity_id: str) -> Optional[dict[str, Any]]:
        """Test-only direct read that does not increment HTTP SQL counters."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM records WHERE id = ?", (entity_id,)
            ).fetchone()
            return dict(row) if row is not None else None

    def direct_concurrent_touch(
        self,
        entity_id: str,
        *,
        updated_by: str = "concurrent-worker",
        notes_override: Optional[str] = None,
    ) -> None:
        """Simulate an external writer mutating a record concurrently in SQLite."""
        with self._lock, self._connect() as conn:
            if notes_override is not None:
                conn.execute(
                    """
                    UPDATE records
                    SET notes = ?, version = version + 1, updated_by = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (notes_override, updated_by, _utc_now(), entity_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE records
                    SET version = version + 1, updated_by = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (updated_by, _utc_now(), entity_id),
                )
            conn.commit()

    def get_one(self, entity_id: str) -> Optional[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM records WHERE id = ?", (entity_id,)
            ).fetchone()
            if row is None:
                return None
            self.sql_rows_read += 1
            record = dict(row)
        hook = self.after_get_hook
        if hook is not None:
            hook(entity_id)
        return record

    def search(self, filters: dict[str, str]) -> list[dict[str, Any]]:
        allowed_cols = set(BUSINESS_FIELDS) | {"id"}
        clauses = []
        params: list[Any] = []
        for col, val in filters.items():
            if col in allowed_cols:
                clauses.append(f"{col} = ?")
                params.append(val)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM records {where_sql} LIMIT 50", params
            ).fetchall()
            self.sql_rows_read += len(rows)
            return [dict(r) for r in rows]

    def create(self, payload: dict[str, Any], author: str) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            rec_id = str(payload.get("id") or f"rec-{self._next_seq:04d}")
            self._next_seq += 1
            now = _utc_now()
            conn.execute(
                """
                INSERT INTO records (
                    id, first_name, last_name, email, phone,
                    department, role, notes, version, updated_by, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rec_id,
                    payload.get("first_name"),
                    payload.get("last_name"),
                    payload.get("email"),
                    payload.get("phone"),
                    payload.get("department"),
                    payload.get("role"),
                    payload.get("notes"),
                    1,
                    str(payload.get("updated_by") or author),
                    now,
                ),
            )
            conn.commit()
            self.sql_writes_executed += 1
            created = dict(
                conn.execute("SELECT * FROM records WHERE id = ?", (rec_id,)).fetchone()
            )
        hook = self.after_write_hook
        if hook is not None:
            hook(rec_id)
        return created

    def replace_put(
        self,
        entity_id: str,
        payload: dict[str, Any],
        *,
        if_match: Optional[str],
        author: str,
    ) -> tuple[int, Optional[dict[str, Any]]]:
        """Full PUT replacement: omitted business fields are wiped to NULL!"""
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM records WHERE id = ?", (entity_id,)
            ).fetchone()
            if existing is None:
                return 404, None
            current_etag = f'"v{existing["version"]}"'
            if if_match is not None and if_match != current_etag:
                return 412, dict(existing)

            new_version = int(existing["version"]) + 1
            now = _utc_now()
            effective_author = str(payload.get("updated_by") or author)
            conn.execute(
                """
                UPDATE records
                SET first_name = ?,
                    last_name = ?,
                    email = ?,
                    phone = ?,
                    department = ?,
                    role = ?,
                    notes = ?,
                    version = ?,
                    updated_by = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    payload.get("first_name"),
                    payload.get("last_name"),
                    payload.get("email"),
                    payload.get("phone"),
                    payload.get("department"),
                    payload.get("role"),
                    payload.get("notes"),
                    new_version,
                    effective_author,
                    now,
                    entity_id,
                ),
            )
            conn.commit()
            self.sql_writes_executed += 1
            updated = dict(
                conn.execute(
                    "SELECT * FROM records WHERE id = ?", (entity_id,)
                ).fetchone()
            )
        hook = self.after_write_hook
        if hook is not None:
            hook(entity_id)
        return 200, updated

    def delete(self, entity_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM records WHERE id = ?", (entity_id,))
            conn.commit()
            if cur.rowcount > 0:
                self.sql_writes_executed += 1
                return True
            return False


class _StoreHTTPServer(ThreadingHTTPServer):
    store: RealSQLiteAppStore


class _StoreRequestHandler(BaseHTTPRequestHandler):
    server: _StoreHTTPServer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return  # Keep test output clean

    def _send_json(
        self,
        status: int,
        payload: Optional[dict[str, Any]],
        *,
        etag: Optional[str] = None,
        location: Optional[str] = None,
    ) -> None:
        raw = json.dumps(payload).encode("utf-8") if payload is not None else b""
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        if etag is not None:
            self.send_header("ETag", etag)
        if location is not None:
            self.send_header("Location", location)
        self.end_headers()
        if raw:
            self.wfile.write(raw)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        return dict(json.loads(self.rfile.read(length).decode("utf-8")))

    def _author(self) -> str:
        return self.headers.get("X-Actor-Id") or "api-client"

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        if parts == ["records"]:
            filters = dict(urllib.parse.parse_qsl(parsed.query))
            items = self.server.store.search(filters)
            self._send_json(200, {"items": items})
            return
        if len(parts) == 2 and parts[0] == "records":
            entity_id = urllib.parse.unquote(parts[1])
            rec = self.server.store.get_one(entity_id)
            if rec is None:
                self._send_json(404, {"error": "not_found"})
                return
            self._send_json(200, rec, etag=f'"v{rec["version"]}"')
            return
        self._send_json(404, {"error": "unknown_route"})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        if parts == ["records"]:
            body = self._read_json_body()
            created = self.server.store.create(body, self._author())
            self._send_json(
                201,
                created,
                etag=f'"v{created["version"]}"',
                location=f"/records/{created['id']}",
            )
            return
        self._send_json(404, {"error": "unknown_route"})

    def do_PUT(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) == 2 and parts[0] == "records":
            entity_id = urllib.parse.unquote(parts[1])
            body = self._read_json_body()
            if_match = self.headers.get("If-Match")
            status, rec = self.server.store.replace_put(
                entity_id,
                body,
                if_match=if_match,
                author=self._author(),
            )
            if status == 404 or rec is None:
                self._send_json(404, {"error": "not_found"})
                return
            self._send_json(status, rec, etag=f'"v{rec["version"]}"')
            return
        self._send_json(404, {"error": "unknown_route"})

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) == 2 and parts[0] == "records":
            entity_id = urllib.parse.unquote(parts[1])
            deleted = self.server.store.delete(entity_id)
            self._send_json(204 if deleted else 404, None)
            return
        self._send_json(404, {"error": "unknown_route"})


class _StubWireTap:
    """Minimal ``WireTapProtocol`` test double for ``wrap_action`` integration checks."""

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url
        self._session_id = "s0"
        self.next_summary: Optional[WireSessionSummary] = None
        self.reset_calls: list[Optional[str]] = []

    @property
    def base_url(self) -> str:
        return self._base_url

    def reset_session(self, session_id: Optional[str] = None) -> str:
        self.reset_calls.append(session_id)
        self._session_id = session_id or "default"
        return self._session_id

    def summary(self) -> WireSessionSummary:
        if self.next_summary is not None:
            return self.next_summary
        return WireSessionSummary(session_id=self._session_id)


@pytest.fixture()
def live_app(tmp_path: Path) -> Iterator[tuple[str, RealSQLiteAppStore]]:
    store = RealSQLiteAppStore(tmp_path / "app.sqlite", seed_rows=5000)
    httpd = _StoreHTTPServer(("127.0.0.1", 0), _StoreRequestHandler)
    httpd.store = store
    host, port = httpd.server_address[:2]
    base_url = f"http://{host}:{port}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield base_url, store
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5.0)


def _make_replace_lens(
    base_url: str,
    *,
    expected_author: Optional[str] = None,
) -> EntityLensSpec:
    return EntityLensSpec(
        entity_name="record",
        base_url=base_url,
        id_field="id",
        write_mode="replace",
        get_one=EndpointTemplate(
            method="GET",
            path_template="/records/{id}",
            etag_header="ETag",
            version_field="version",
            author_field="updated_by",
        ),
        write_endpoint=EndpointTemplate(
            method="PUT",
            path_template="/records/{id}",
            etag_header="ETag",
            author_field="updated_by" if expected_author else None,
        ),
        create_endpoint=EndpointTemplate(
            method="POST",
            path_template="/records",
            etag_header="ETag",
        ),
        duplicate_search=EndpointTemplate(
            method="GET",
            path_template="/records",
            query_params={"email": "{email}"},
            records_key="items",
        ),
        delete_endpoint=EndpointTemplate(
            method="DELETE",
            path_template="/records/{id}",
        ),
        identity_fields=("first_name", "last_name"),
        volatile_fields=("updated_at", "updated_by", "version", "etag"),
        field_codecs={"phone": FieldCodec(kind="phone_digits")},
        duplicate_keys=("email",),
        expected_author=expected_author,
    )


def _http_json(
    method: str,
    url: str,
    payload: Optional[dict[str, Any]] = None,
) -> tuple[int, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url=url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            raw = resp.read()
            return int(resp.status), json.loads(raw.decode("utf-8")) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return int(exc.code), json.loads(raw.decode("utf-8")) if raw else None


# ---------------------------------------------------------------------------
# 1. Safe replace-mode PUT merge + O(1) 2-row verification on 5,000-row store
# ---------------------------------------------------------------------------


def test_replace_mode_update_preserves_untouched_fields_in_o1_reads(
    live_app: tuple[str, RealSQLiteAppStore],
    tmp_path: Path,
) -> None:
    base_url, store = live_app
    workdir = tmp_path / "tx-work"
    lens = _make_replace_lens(base_url)
    engine = TransactionalCommitEngine(
        lens,
        workdir=workdir,
        allow_unqualified_prototype=True,
    )

    before = store.direct_inspect("rec-2500")
    assert before is not None
    store.reset_counters()

    task = TxTask(
        task_id="update-rec-2500",
        operation="update",
        entity_id="rec-2500",
        expected_identity={"first_name": "First2500", "last_name": "Last2500"},
        intended_delta={"phone": "555-9999"},
    )
    result = engine.execute(task)

    assert result.outcome == "UNCERTAIN"
    assert result.occ_status == "UNKNOWN"
    assert result.rows_read == 2
    assert store.sql_rows_read == 2  # Exactly 2 rows read out of 5,000!
    assert store.sql_writes_executed == 1
    assert result.judge_verdict is not None
    assert result.judge_verdict.passed is True
    assert result.judge_verdict.clobbered_fields == ()

    after = store.direct_inspect("rec-2500")
    assert after is not None
    assert after["phone"] == "555-9999"
    assert after["version"] == 2
    # All 6 untouched business fields are preserved intact:
    untouched_six = ("first_name", "last_name", "email", "department", "role", "notes")
    for col in untouched_six:
        assert after[col] == before[col], f"field {col} was clobbered"

    # Private salt.bin + undo_r0.json have 0o600 mode, and public receipt has zero PII
    task_dir = workdir / task_key_for(task.task_id)
    salt_file = task_dir / "salt.bin"
    undo_file = task_dir / "undo_r0.json"
    assert salt_file.is_file() and len(salt_file.read_bytes()) == 32
    assert undo_file.is_file()
    assert stat.S_IMODE(salt_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(undo_file.stat().st_mode) == 0o600

    assert result.receipt_path is not None
    verified = verify_tx_receipt(result.receipt_path, engine.public_key)
    assert verified["schema_version"] == TX_RECEIPT_SCHEMA
    assert verified["outcome"] == "UNCERTAIN"
    assert verified["occ_status"] == "UNKNOWN"
    assert verified["rows_read"] == 2
    assert "r0_salted_sha256" in verified
    assert "r1_salted_sha256" in verified
    raw_receipt_text = Path(result.receipt_path).read_bytes().decode("latin-1")
    assert "First2500" not in raw_receipt_text
    assert "person2500@example.test" not in raw_receipt_text
    assert "555-9999" not in raw_receipt_text


# ---------------------------------------------------------------------------
# 2. Wrapping naive external partial-PUT agent -> REFUTED + 1-click compensate
# ---------------------------------------------------------------------------


def test_wrap_action_catches_partial_put_clobber_and_compensates(
    live_app: tuple[str, RealSQLiteAppStore],
    tmp_path: Path,
) -> None:
    base_url, store = live_app
    workdir = tmp_path / "tx-wrap"
    lens = _make_replace_lens(base_url)
    engine = TransactionalCommitEngine(
        lens,
        workdir=workdir,
        allow_unqualified_prototype=True,
    )

    baseline = store.direct_inspect("rec-1234")
    assert baseline is not None
    store.reset_counters()

    task = TxTask(
        task_id="naive-agent-put-1234",
        operation="update",
        entity_id="rec-1234",
        expected_identity={"first_name": "First1234", "last_name": "Last1234"},
        intended_delta={"phone": "555-0000"},
    )

    def naive_partial_put_agent() -> None:
        # Broken partial PUT sends ONLY phone, wiping all 6 other columns to NULL!
        status, _ = _http_json(
            "PUT",
            f"{base_url}/records/rec-1234",
            {"phone": "555-0000"},
        )
        assert status == 200

    result = engine.wrap_action(task, naive_partial_put_agent)

    assert result.outcome == "REFUTED"
    assert result.can_compensate is False
    assert result.rows_read == 2
    assert result.judge_verdict is not None
    expected_clobbered = {
        "first_name",
        "last_name",
        "email",
        "department",
        "role",
        "notes",
    }
    assert set(result.judge_verdict.clobbered_fields) == expected_clobbered
    for field_name in expected_clobbered:
        assert any(field_name in reason for reason in result.reasons)

    # Confirm the naive agent genuinely wiped the DB columns before compensation:
    damaged = store.direct_inspect("rec-1234")
    assert damaged is not None
    for field_name in expected_clobbered:
        assert damaged[field_name] is None

    # HTTP cannot promise atomic undo; retain a review proposal across fresh engines:
    recovery_engine = TransactionalCommitEngine(
        lens,
        workdir=workdir,
        allow_unqualified_prototype=True,
    )
    assert recovery_engine.compensate(task.task_id) is False
    assert recovery_engine.compensation_proposal(task.task_id) is not None

    restored = store.direct_inspect("rec-1234")
    assert restored is not None
    for col in BUSINESS_FIELDS:
        assert restored[col] == damaged[col]


def test_wrap_action_wire_tap_collateral_and_create_delete_compensation(
    live_app: tuple[str, RealSQLiteAppStore],
    tmp_path: Path,
) -> None:
    base_url, store = live_app
    wire_tap = _StubWireTap(base_url)
    lens = _make_replace_lens(base_url)
    engine = TransactionalCommitEngine(
        lens,
        workdir=tmp_path / "tx-wire",
        wire_tap=wire_tap,
        allow_unqualified_prototype=True,
    )

    create_task = TxTask(
        task_id="create-via-wiretap",
        operation="create",
        intended_delta={
            "first_name": "Grace",
            "last_name": "Hopper",
            "email": "grace@example.test",
            "phone": "555-7777",
        },
    )
    created_holder: dict[str, str] = {}

    def agent_create_with_collateral() -> None:
        status, body = _http_json(
            "POST",
            f"{base_url}/records",
            dict(create_task.intended_delta),
        )
        assert status == 201
        created_id = str(body["id"])
        created_holder["id"] = created_id
        wire_tap.next_summary = WireSessionSummary(
            session_id=create_task.task_id,
            total_requests=2,
            mutations_allowed=1,
            collateral_mutations=1,
            mutations=(
                WireMutationRecord(
                    seq=1,
                    method="POST",
                    url=f"{base_url}/records",
                    path="/records",
                    request_body_sha256="a" * 64,
                    response_status=201,
                    extracted_entity_id=created_id,
                ),
            ),
            ledger_sha256="b" * 64,
        )

    res = engine.wrap_action(create_task, agent_create_with_collateral)
    assert res.outcome == "REFUTED"
    assert "wire_collateral_mutation" in res.reasons
    assert res.entity_id == created_holder["id"]
    assert res.can_compensate is False

    verified = verify_tx_receipt(res.receipt_path or "", engine.public_key)
    assert verified["wire_collateral_mutations"] == 1
    assert verified["wire_mutations_allowed"] == 1

    # Unqualified HTTP create cannot authorize automatic deletion.
    assert engine.compensate(create_task.task_id) is False
    assert engine.compensation_proposal(create_task.task_id) is not None
    assert store.direct_inspect(created_holder["id"]) is not None


# ---------------------------------------------------------------------------
# 3. Pre-commit wrong-record refusal (including 2-char "Bo" vs "Li" names!)
# ---------------------------------------------------------------------------


def test_precommit_short_name_identity_mismatch_blocks_zero_writes(
    live_app: tuple[str, RealSQLiteAppStore],
    tmp_path: Path,
) -> None:
    base_url, store = live_app
    lens = _make_replace_lens(base_url)
    engine = TransactionalCommitEngine(
        lens,
        workdir=tmp_path / "tx-id-guard",
        allow_unqualified_prototype=True,
    )

    # rec-0010 is seeded with first_name="Li", last_name="Chen"
    rec10 = store.direct_inspect("rec-0010")
    assert rec10 is not None
    assert rec10["first_name"] == "Li"
    store.reset_counters()

    wrong_person_task = TxTask(
        task_id="wrong-short-name-bo-vs-li",
        operation="update",
        entity_id="rec-0010",
        expected_identity={"first_name": "Bo", "last_name": "Chen"},
        intended_delta={"phone": "555-8888"},
    )
    agent_called = False

    def should_never_run() -> None:
        nonlocal agent_called
        agent_called = True

    wrap_res = engine.wrap_action(wrong_person_task, should_never_run)
    assert wrap_res.outcome == "BLOCKED_PRECOMMIT"
    assert "precommit_identity_mismatch" in wrap_res.reasons
    assert wrap_res.rows_read == 1
    assert store.sql_rows_read == 1
    assert store.sql_writes_executed == 0
    assert agent_called is False

    store.reset_counters()
    exec_res = engine.execute(
        TxTask(
            task_id="wrong-short-name-exec",
            operation="update",
            entity_id="rec-0010",
            expected_identity={"first_name": "Bo", "last_name": "Chen"},
            intended_delta={"phone": "555-8888"},
        )
    )
    assert exec_res.outcome == "BLOCKED_PRECOMMIT"
    assert "precommit_identity_mismatch" in exec_res.reasons
    assert exec_res.rows_read == 1
    assert store.sql_writes_executed == 0
    assert store.direct_inspect("rec-0010") == rec10

    # 404 target record -> BLOCKED_PRECOMMIT ("precommit_target_unreadable")
    missing_res = engine.execute(
        TxTask(
            task_id="missing-rec-99999",
            operation="update",
            entity_id="rec-99999",
            expected_identity={"first_name": "Bo"},
            intended_delta={"phone": "555-8888"},
        )
    )
    assert missing_res.outcome == "BLOCKED_PRECOMMIT"
    assert "precommit_target_unreadable" in missing_res.reasons
    assert store.sql_writes_executed == 0


# ---------------------------------------------------------------------------
# 4. Duplicate create prevention + crash-safe write_ledger across instances
# ---------------------------------------------------------------------------


def test_duplicate_create_and_crash_safe_ledger_across_engine_instances(
    live_app: tuple[str, RealSQLiteAppStore],
    tmp_path: Path,
) -> None:
    base_url, store = live_app
    shared_workdir = tmp_path / "shared-tx-workdir"
    lens = _make_replace_lens(base_url)

    engine_1 = TransactionalCommitEngine(
        lens,
        workdir=shared_workdir,
        allow_unqualified_prototype=True,
    )
    store.reset_counters()

    # 4a. Duplicate email already exists in the 5,000-row table -> zero writes!
    dup_task = TxTask(
        task_id="dup-create-person42",
        operation="create",
        intended_delta={
            "first_name": "First42",
            "last_name": "Last42",
            "email": "person42@example.test",
            "phone": "555-0042",
        },
    )
    dup_res = engine_1.execute(dup_task)
    assert dup_res.outcome == "BLOCKED_PRECOMMIT"
    assert "duplicate_record_exists" in dup_res.reasons
    assert dup_res.rows_read == 1
    assert store.sql_writes_executed == 0

    # 4b. Unique create succeeds on engine_1, then retry on fresh engine_2 blocks
    #     at the ledger gate with rows_read == 0 and zero network writes!
    store.reset_counters()
    unique_create = TxTask(
        task_id="idempotent-create-ada",
        operation="create",
        intended_delta={
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": "ada.lovelace@example.test",
            "phone": "555-1843",
            "department": "Analytics",
            "role": "Lead",
            "notes": "First run",
        },
    )
    first_res = engine_1.execute(unique_create)
    assert first_res.outcome == "UNCERTAIN"
    assert first_res.rows_read == 1  # 0 duplicate matches + 1 post-commit read
    assert store.sql_writes_executed == 1

    store.reset_counters()
    engine_2 = TransactionalCommitEngine(
        lens,
        workdir=shared_workdir,
        allow_unqualified_prototype=True,
    )
    retry_res = engine_2.execute(unique_create)
    assert retry_res.outcome == "BLOCKED_PRECOMMIT"
    assert "ledger_hold_active" in retry_res.reasons
    assert retry_res.rows_read == 0
    assert store.sql_rows_read == 0
    assert store.sql_writes_executed == 0

    # 4c. Simulated crash mid-flight ("begun" recorded in write_ledger, process died
    #     before "resolved"): engine_3 sharing workdir refuses to re-send!
    crash_task = TxTask(
        task_id="crashed-in-flight-update",
        operation="update",
        entity_id="rec-0777",
        expected_identity={"first_name": "First777"},
        intended_delta={"phone": "555-0777"},
    )
    crash_key = engine_1._ledger_key(crash_task)
    engine_1.ledger.begun(crash_key, "crashed-attempt-1", write_key_bound=True)

    store.reset_counters()
    engine_3 = TransactionalCommitEngine(
        lens,
        workdir=shared_workdir,
        allow_unqualified_prototype=True,
    )
    after_crash_res = engine_3.execute(crash_task)
    assert after_crash_res.outcome == "BLOCKED_PRECOMMIT"
    assert "ledger_hold_active" in after_crash_res.reasons
    assert after_crash_res.rows_read == 0
    assert store.sql_rows_read == 0
    assert store.sql_writes_executed == 0


# ---------------------------------------------------------------------------
# 5. Optimistic Concurrency Control (OCC) + zero false stops on other rows
# ---------------------------------------------------------------------------


def test_occ_detects_same_record_races_while_ignoring_other_row_traffic(
    live_app: tuple[str, RealSQLiteAppStore],
    tmp_path: Path,
) -> None:
    base_url, store = live_app
    lens = _make_replace_lens(base_url)

    # 5a. Concurrent edit between r0 read and PUT -> If-Match fails with HTTP 412
    engine_if_match = TransactionalCommitEngine(
        lens,
        workdir=tmp_path / "occ-412",
        allow_unqualified_prototype=True,
    )

    def race_after_r0_get(entity_id: str) -> None:
        if entity_id == "rec-0500":
            store.after_get_hook = None
            store.direct_concurrent_touch(
                "rec-0500",
                notes_override="Concurrent edit before PUT",
            )

    store.after_get_hook = race_after_r0_get
    res_412 = engine_if_match.execute(
        TxTask(
            task_id="occ-if-match-500",
            operation="update",
            entity_id="rec-0500",
            expected_identity={"first_name": "First500"},
            intended_delta={"phone": "555-0500"},
        )
    )
    store.after_get_hook = None
    assert res_412.outcome == "BLOCKED_PRECOMMIT"
    assert res_412.occ_status == "CONCURRENT_MODIFICATION"
    assert store.direct_inspect("rec-0500")["notes"] == "Concurrent edit before PUT"  # type: ignore[index]

    # 5b. Concurrent edit between write and r1 read (bumping version by +2 overall)
    #     -> surfaces CONCURRENT_MODIFICATION + UNCERTAIN instead of CONFIRMED!
    engine_ver_jump = TransactionalCommitEngine(
        lens,
        workdir=tmp_path / "occ-vjump",
        allow_unqualified_prototype=True,
    )

    def race_after_write(entity_id: str) -> None:
        if entity_id == "rec-0501":
            store.after_write_hook = None
            store.direct_concurrent_touch("rec-0501")

    store.after_write_hook = race_after_write
    res_jump = engine_ver_jump.execute(
        TxTask(
            task_id="occ-version-jump-501",
            operation="update",
            entity_id="rec-0501",
            expected_identity={"first_name": "First501"},
            intended_delta={"phone": "555-0501"},
        )
    )
    store.after_write_hook = None
    assert res_jump.occ_status == "CONCURRENT_MODIFICATION"
    assert res_jump.outcome == "UNCERTAIN"
    assert "concurrent_version_jump" in res_jump.reasons
    assert res_jump.can_compensate is False

    # 5c. Heavy concurrent writes to OTHER records in the 5,000-row table cause
    #     ZERO false stops on our target record (rec-4000)!
    stop_bg = threading.Event()
    bg_errors: list[Exception] = []

    def background_writer(worker_idx: int) -> None:
        try:
            step = 0
            while not stop_bg.is_set():
                other_id = f"rec-{(worker_idx * 20 + (step % 15) + 1):04d}"
                store.direct_concurrent_touch(
                    other_id,
                    updated_by=f"bg-{worker_idx}",
                )
                step += 1
        except Exception as exc:  # noqa: BLE001
            bg_errors.append(exc)

    workers = [
        threading.Thread(target=background_writer, args=(i,), daemon=True)
        for i in range(4)
    ]
    for w in workers:
        w.start()
    try:
        engine_isolated = TransactionalCommitEngine(
            lens,
            workdir=tmp_path / "occ-isolated",
            allow_unqualified_prototype=True,
        )
        for idx in range(5):
            target_num = 4000 + idx
            target_id = f"rec-{target_num:04d}"
            clean_res = engine_isolated.execute(
                TxTask(
                    task_id=f"isolated-update-{target_num}",
                    operation="update",
                    entity_id=target_id,
                    expected_identity={"first_name": f"First{target_num}"},
                    intended_delta={"phone": f"(555) 900-{idx:04d}"},
                )
            )
            assert clean_res.outcome == "UNCERTAIN"
            assert clean_res.occ_status == "UNKNOWN"
            assert clean_res.rows_read == 2
    finally:
        stop_bg.set()
        for w in workers:
            w.join(timeout=5.0)
    assert bg_errors == []


# ---------------------------------------------------------------------------
# 6. Receipt tamper resistance + standalone verify_tx_receipt checks
# ---------------------------------------------------------------------------


def test_tx_receipt_single_byte_tamper_fails_verification(tmp_path: Path) -> None:
    workdir = tmp_path / "receipt-tamper"
    signer = Ed25519PrivateKey.generate()
    rec_id, rec_path = issue_tx_receipt(
        workdir,
        task_id="tamper-check-1",
        entity_name="record",
        operation="update",
        outcome="CONFIRMED",
        occ_status="UNCONTESTED",
        intended_delta={"phone": "555-1212"},
        r0={"id": "rec-1", "phone": "555-0000"},
        r1={"id": "rec-1", "phone": "555-1212"},
        rows_read=2,
        private_key=signer,
    )
    assert len(rec_id) == 64

    decoded = verify_tx_receipt(rec_path, signer.public_key())
    assert decoded["outcome"] == "CONFIRMED"
    assert decoded["rows_read"] == 2

    original = rec_path.read_bytes()
    # Flip 1 byte across beginning, header, JSON payload, signature, and SCITT proof
    sample_offsets = sorted(
        {
            0,
            1,
            len(original) // 4,
            len(original) // 2,
            (3 * len(original)) // 4,
            len(original) - 1,
        }
    )
    for offset in sample_offsets:
        tampered = bytearray(original)
        tampered[offset] ^= 0x01
        tampered_path = tmp_path / f"tampered-{offset}.cose"
        tampered_path.write_bytes(bytes(tampered))
        with pytest.raises(ValueError):
            verify_tx_receipt(
                tampered_path,
                signer.public_key(),
                log_dir=workdir / "log",
            )

    # Trailing byte appended -> fails canonical CBOR check
    trailing_path = tmp_path / "trailing.cose"
    trailing_path.write_bytes(original + b"\x00")
    with pytest.raises(ValueError):
        verify_tx_receipt(
            trailing_path,
            signer.public_key(),
            log_dir=workdir / "log",
        )

    # Wrong public key -> fails KID / signature check
    wrong_signer = Ed25519PrivateKey.generate()
    with pytest.raises(ValueError):
        verify_tx_receipt(
            rec_path,
            wrong_signer.public_key(),
            log_dir=workdir / "log",
        )


# ---------------------------------------------------------------------------
# 7. Generality & zero-vision import boundary gates
# ---------------------------------------------------------------------------


def test_generality_and_import_boundary_gates(tmp_path: Path) -> None:
    tx_dir = Path(__file__).resolve().parents[1] / "openadapt_flow" / "tx"
    banned_app_names = (
        "openemr",
        "medplum",
        "vikunja",
        "dolibarr",
        "openimis",
        "suitecrm",
        "erpnext",
    )
    for filename in ("receipt.py", "lens.py"):
        source = (tx_dir / filename).read_text(encoding="utf-8").lower()
        for app_name in banned_app_names:
            assert app_name not in source, f"banned app name {app_name!r} in {filename}"

    script = textwrap.dedent(
        """
        import json, sys
        REFUSED = {"numpy", "cv2", "PIL", "rapidocr_onnxruntime", "playwright", "torch"}
        attempts = []

        class RefuseVision:
            def find_spec(self, name, path=None, target=None):
                if name.partition(".")[0] in REFUSED:
                    attempts.append(name)
                    raise ImportError("refused: " + name)
                return None

        sys.meta_path.insert(0, RefuseVision())
        import openadapt_flow.tx.receipt
        import openadapt_flow.tx.lens
        loaded = sorted(m for m in sys.modules if m.partition(".")[0] in REFUSED)
        print(json.dumps({"attempts": attempts, "loaded": loaded}))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    assert report == {"attempts": [], "loaded": []}
    # Verify EntityLensClient is cleanly exported for callers
    assert EntityLensClient is not None


def test_occ_author_attribution_and_pre_send_reread(
    live_app: tuple[str, RealSQLiteAppStore],
    tmp_path: Path,
) -> None:
    base_url, store = live_app

    # Author mismatch in wrap_action -> CONCURRENT_MODIFICATION + UNCERTAIN
    author_lens = _make_replace_lens(base_url, expected_author="trusted-bot")
    author_engine = TransactionalCommitEngine(
        author_lens,
        workdir=tmp_path / "occ-author",
        allow_unqualified_prototype=True,
    )
    baseline = store.direct_inspect("rec-0600")
    assert baseline is not None

    def rogue_author_write() -> None:
        merged = {**baseline, "phone": "555-0600", "updated_by": "other-human"}
        status, _ = _http_json("PUT", f"{base_url}/records/rec-0600", merged)
        assert status == 200

    author_res = author_engine.wrap_action(
        TxTask(
            task_id="author-mismatch-600",
            operation="update",
            entity_id="rec-0600",
            expected_identity={"first_name": "First600"},
            intended_delta={"phone": "555-0600"},
        ),
        rogue_author_write,
    )
    assert author_res.occ_status == "CONCURRENT_MODIFICATION"
    assert author_res.outcome == "UNCERTAIN"
    assert "concurrent_author_mismatch" in author_res.reasons

    # pre_send_reread=True detects concurrent edit BEFORE write is ever sent
    reread_lens = _make_replace_lens(base_url)
    reread_engine = TransactionalCommitEngine(
        reread_lens,
        workdir=tmp_path / "occ-reread",
        pre_send_reread=True,
        allow_unqualified_prototype=True,
    )

    def touch_once_after_first_get(entity_id: str) -> None:
        if entity_id == "rec-0601":
            store.after_get_hook = None
            store.direct_concurrent_touch("rec-0601")

    store.reset_counters()
    store.after_get_hook = touch_once_after_first_get
    reread_res = reread_engine.execute(
        TxTask(
            task_id="reread-occ-601",
            operation="update",
            entity_id="rec-0601",
            expected_identity={"first_name": "First601"},
            intended_delta={"phone": "555-0601"},
        )
    )
    store.after_get_hook = None
    assert reread_res.outcome == "BLOCKED_PRECOMMIT"
    assert reread_res.occ_status == "CONCURRENT_MODIFICATION"
    assert (
        store.sql_writes_executed == 0
    )  # Only direct_concurrent_touch wrote directly!


def test_identity_codec_opt_in_and_certified_pack_and_lazy_judge(
    live_app: tuple[str, RealSQLiteAppStore],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_url, store = live_app
    base_lens = _make_replace_lens(base_url)

    # Without casefold codec on first_name, "li" != "Li" -> BLOCKED_PRECOMMIT
    strict_engine = TransactionalCommitEngine(
        base_lens,
        workdir=tmp_path / "strict-case",
        allow_unqualified_prototype=True,
    )
    strict_res = strict_engine.execute(
        TxTask(
            task_id="case-mismatch-li",
            operation="update",
            entity_id="rec-0010",
            expected_identity={"first_name": "li", "last_name": "Chen"},
            intended_delta={"phone": "555-1010"},
        )
    )
    assert strict_res.outcome == "BLOCKED_PRECOMMIT"
    assert "precommit_identity_mismatch" in strict_res.reasons

    # With explicit FieldCodec(kind="casefold") on first_name, "li" matches "Li"
    casefold_lens = base_lens.model_copy(
        update={
            "field_codecs": {
                **base_lens.field_codecs,
                "first_name": FieldCodec(kind="casefold"),
            }
        }
    )
    casefold_engine = TransactionalCommitEngine(
        casefold_lens,
        workdir=tmp_path / "optin-case",
        allow_unqualified_prototype=True,
    )
    ok_res = casefold_engine.execute(
        TxTask(
            task_id="casefold-ok-li",
            operation="update",
            entity_id="rec-0010",
            expected_identity={"first_name": "li", "last_name": "Chen"},
            intended_delta={"phone": "(555) 1010"},
        )
    )
    assert ok_res.outcome == "UNCERTAIN"

    # Legacy pack names retain probe observations, but never grant authority
    uncertified_pack = CertifiedLensPack(
        lens=base_lens,
        certified=False,
        sensitivity=0.8,
        specificity=1.0,
    ).with_computed_digest()
    with pytest.raises(ValueError, match="uncertified"):
        TransactionalCommitEngine(
            uncertified_pack,
            workdir=tmp_path / "uncert",
            allow_unqualified_prototype=True,
        )

    certified_pack = CertifiedLensPack(
        lens=base_lens,
        certified=False,
        probes_passed=True,
        sensitivity=1.0,
        specificity=1.0,
    ).with_computed_digest()
    with pytest.raises(ValueError, match="incomplete_probe_set"):
        TransactionalCommitEngine(
            certified_pack,
            workdir=tmp_path / "cert",
            allow_unqualified_prototype=True,
        )
    # Real loopback probes must supply six complete observations; they remain unqualified.
    from openadapt_flow.tx.induce import certify_lens_on_sandbox

    qualified_pack = certify_lens_on_sandbox(
        base_lens,
        sample_record={
            "first_name": "Dana",
            "last_name": "Lee",
            "email": "qualified@example.test",
            "phone": "555-9999",
            "department": "Care",
            "role": "Staff",
            "notes": "keep",
        },
        sample_update={"phone": "555-4444"},
    )
    assert qualified_pack.probes_passed and not qualified_pack.certified
    assert not qualified_pack.probe_integrity_errors()
    assert qualified_pack.admission_errors() == ("unqualified_probe_report",)
    cert_engine = TransactionalCommitEngine(
        qualified_pack,
        workdir=tmp_path / "qualified",
        allow_unqualified_prototype=True,
    )
    assert cert_engine.lens == qualified_pack.lens

    # Lazy discovery of openadapt_flow.tx.judge.FrameAndIntentJudge when merged
    fake_judge_mod = types.ModuleType("openadapt_flow.tx.judge")

    class FrameAndIntentJudge:
        def evaluate(
            self,
            r0: Any,
            r1: Any,
            intended_delta: Any,
            *,
            volatile_fields: tuple[str, ...] = (),
            field_codecs: Any = None,
        ) -> JudgeVerdict:
            return JudgeVerdict(
                passed=True,
                frame_preserved=True,
                intent_satisfied=True,
            )

    fake_judge_mod.FrameAndIntentJudge = FrameAndIntentJudge  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openadapt_flow.tx.judge", fake_judge_mod)
    lazy_engine = TransactionalCommitEngine(
        base_lens,
        workdir=tmp_path / "lazy-judge",
        allow_unqualified_prototype=True,
    )
    assert isinstance(lazy_engine.judge, FrameAndIntentJudge)

    # Nested + dotted deep_merge preserves untouched sibling keys
    merged = deep_merge(
        {"a": 1, "nested": {"keep": "yes", "change": "old"}},
        {"nested.change": "new"},
    )
    assert merged == {"a": 1, "nested": {"keep": "yes", "change": "new"}}

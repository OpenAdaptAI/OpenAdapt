"""End-to-end tests for openadapt_flow.tx.induce (Zero-YAML Self-Certifying Lens Compiler).

Exercises real multi-threaded HTTP + SQLite sandbox servers with zero mocks:
1. Silent-clobber PUT server (overwrites omitted fields with "" / 0 and returns 200 OK).
2. Paginated + field-normalizing server pre-seeded with 2,000 rows (200-row GET cap,
   uppercase street, Title-Case last_name, "Massachusetts" -> "MA" bijection, and
   concurrent background writers).
3. Weak/broken custom lenses (wrong get_one, collection get_one, clobber-ignoring
   volatile_fields, weak intent-only judge, unrefined patch on clobbering PUT).
4. Remote non-loopback sandbox refusal gate + LLM proposer veto gate + import/generality
   boundaries.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

import pytest

from openadapt_flow.tx.induce import (
    DefaultFrameAndIntentJudge,
    SandboxSafetyError,
    certify_lens_on_sandbox,
    induce_lens_from_har,
    infer_field_codec,
)
from openadapt_flow.tx.judge import FrameAndIntentJudge
from openadapt_flow.tx.types import (
    CERTIFIED_LENS_PACK_SCHEMA,
    ENTITY_LENS_SCHEMA,
    EndpointTemplate,
    EntityLensSpec,
    FieldCodec,
    JudgeVerdict,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _http_call_and_record_har_entry(
    method: str,
    url: str,
    *,
    payload: Optional[Mapping[str, Any]] = None,
    headers: Optional[Mapping[str, str]] = None,
) -> tuple[int, Any, dict[str, Any]]:
    """Execute a real HTTP call against a live server and return a HAR 1.2 entry."""
    req_headers: dict[str, str] = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    body_text = ""
    body_bytes: Optional[bytes] = None
    if payload is not None:
        body_text = json.dumps(payload)
        body_bytes = body_text.encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")

    req = urllib.request.Request(
        url=url, data=body_bytes, headers=req_headers, method=method.upper()
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            status = int(resp.status)
            resp_headers = list(resp.headers.items())
            raw_resp = resp.read()
            mime_type = resp.headers.get_content_type()
    except urllib.error.HTTPError as err:
        status = int(err.code)
        resp_headers = list(err.headers.items()) if err.headers else []
        raw_resp = err.read() if err.fp else b""
        mime_type = err.headers.get_content_type() if err.headers else "text/plain"

    resp_text = raw_resp.decode("utf-8", errors="replace")
    parsed_json: Any = None
    if resp_text.strip():
        try:
            parsed_json = json.loads(resp_text)
        except json.JSONDecodeError:
            parsed_json = resp_text

    parsed_url = urllib.parse.urlparse(url)
    query_list = [
        {"name": k, "value": v}
        for k, v in urllib.parse.parse_qsl(parsed_url.query, keep_blank_values=True)
    ]

    har_req: dict[str, Any] = {
        "method": method.upper(),
        "url": url,
        "httpVersion": "HTTP/1.1",
        "headers": [{"name": k, "value": v} for k, v in req_headers.items()],
        "queryString": query_list,
        "cookies": [],
        "headersSize": -1,
        "bodySize": len(body_bytes) if body_bytes else 0,
    }
    if payload is not None:
        har_req["postData"] = {
            "mimeType": req_headers["Content-Type"],
            "text": body_text,
        }

    har_entry: dict[str, Any] = {
        "startedDateTime": _utc_now_iso(),
        "time": 2.5,
        "request": har_req,
        "response": {
            "status": status,
            "statusText": "OK" if 200 <= status < 300 else "ERR",
            "httpVersion": "HTTP/1.1",
            "headers": [{"name": k, "value": v} for k, v in resp_headers],
            "cookies": [],
            "content": {
                "size": len(raw_resp),
                "mimeType": mime_type,
                "text": resp_text,
            },
            "redirectURL": "",
            "headersSize": -1,
            "bodySize": len(raw_resp),
        },
        "cache": {},
        "timings": {"send": 0.5, "wait": 1.5, "receive": 0.5},
    }
    return status, parsed_json, har_entry


# ==============================================================================
# Archetype 1: Real HTTP + SQLite Silent-Clobber PUT Server
# ==============================================================================


class _SilentClobberTaskServer:
    """Real SQLite-backed HTTP server whose PUT overwrites omitted fields with '' / 0."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    priority INTEGER NOT NULL DEFAULT 0,
                    assignee TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def _send(
                self,
                status: int,
                body: Any = None,
                content_type: str = "application/json",
            ) -> None:
                raw = b""
                if body is not None:
                    raw = (
                        json.dumps(body).encode("utf-8")
                        if content_type == "application/json"
                        else str(body).encode("utf-8")
                    )
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                if raw:
                    self.wfile.write(raw)

            def _read_json(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length) if length > 0 else b"{}"
                return json.loads(raw.decode("utf-8"))

            def do_GET(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path.rstrip("/") or "/"
                if path == "/health":
                    self._send(200, {"ok": True})
                    return
                if path == "/static/bundle.js":
                    self._send(200, "console.log('ready');", "application/javascript")
                    return
                if path == "/api/tasks":
                    with parent._lock:
                        rows = [
                            dict(r)
                            for r in parent._conn.execute(
                                "SELECT * FROM tasks ORDER BY id ASC"
                            ).fetchall()
                        ]
                    self._send(200, rows)
                    return
                if path.startswith("/api/tasks/"):
                    tid = path.split("/")[-1]
                    if not tid.isdigit():
                        self._send(404, {"error": "not found"})
                        return
                    with parent._lock:
                        row = parent._conn.execute(
                            "SELECT * FROM tasks WHERE id = ?", (int(tid),)
                        ).fetchone()
                    if row is None:
                        self._send(404, {"error": "not found"})
                    else:
                        self._send(200, dict(row))
                    return
                self._send(404, {"error": "not found"})

            def do_POST(self) -> None:
                path = urllib.parse.urlparse(self.path).path.rstrip("/")
                if path != "/api/tasks":
                    self._send(404, {"error": "not found"})
                    return
                data = self._read_json()
                now = _utc_now_iso()
                with parent._lock, parent._conn:
                    cur = parent._conn.execute(
                        """
                        INSERT INTO tasks (title, description, priority, assignee, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(data.get("title", "")),
                            str(data.get("description", "")),
                            int(data.get("priority", 0)),
                            str(data.get("assignee", "")),
                            now,
                            now,
                        ),
                    )
                    new_id = cur.lastrowid
                    row = parent._conn.execute(
                        "SELECT * FROM tasks WHERE id = ?", (new_id,)
                    ).fetchone()
                self._send(201, dict(row))

            def do_PUT(self) -> None:
                path = urllib.parse.urlparse(self.path).path.rstrip("/")
                if not path.startswith("/api/tasks/"):
                    self._send(404, {"error": "not found"})
                    return
                tid = path.split("/")[-1]
                if not tid.isdigit():
                    self._send(404, {"error": "not found"})
                    return
                data = self._read_json()
                now = _utc_now_iso()
                # SILENT CLOBBER: omitted fields are overwritten with "" / 0!
                with parent._lock, parent._conn:
                    existing = parent._conn.execute(
                        "SELECT id FROM tasks WHERE id = ?", (int(tid),)
                    ).fetchone()
                    if existing is None:
                        self._send(404, {"error": "not found"})
                        return
                    parent._conn.execute(
                        """
                        UPDATE tasks
                        SET title = ?, description = ?, priority = ?, assignee = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            str(data.get("title", "")),
                            str(data.get("description", "")),
                            int(data.get("priority", 0)),
                            str(data.get("assignee", "")),
                            now,
                            int(tid),
                        ),
                    )
                    row = parent._conn.execute(
                        "SELECT * FROM tasks WHERE id = ?", (int(tid),)
                    ).fetchone()
                self._send(200, dict(row))

            def do_DELETE(self) -> None:
                path = urllib.parse.urlparse(self.path).path.rstrip("/")
                if not path.startswith("/api/tasks/"):
                    self._send(404, {"error": "not found"})
                    return
                tid = path.split("/")[-1]
                if not tid.isdigit():
                    self._send(404, {"error": "not found"})
                    return
                with parent._lock, parent._conn:
                    parent._conn.execute("DELETE FROM tasks WHERE id = ?", (int(tid),))
                self._send(204, None)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=3.0)
        self._conn.close()


# ==============================================================================
# Archetype 2: Paginated (2,000 Rows, 200 Cap) + Field-Normalizing Server
# ==============================================================================

_US_STATE_MAP: dict[str, str] = {
    "massachusetts": "MA",
    "california": "CA",
    "new york": "NY",
    "texas": "TX",
    "illinois": "IL",
    "washington": "WA",
}


def _normalize_state(raw: str) -> str:
    cleaned = raw.strip()
    mapped = _US_STATE_MAP.get(cleaned.lower())
    if mapped is not None:
        return mapped
    return cleaned.upper()


class _PaginatedNormalizingRecordServer:
    """Real SQLite server pre-seeded with 2,000 rows, 200-row list cap, and codecs."""

    def __init__(self, db_path: Path, *, seed_rows: int = 2000) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    first_name TEXT NOT NULL,
                    last_name TEXT NOT NULL,
                    street TEXT NOT NULL,
                    state TEXT NOT NULL,
                    email TEXT NOT NULL UNIQUE,
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                )
                """
            )
            now = _utc_now_iso()
            batch = [
                (
                    f"SeedFirst{i}",
                    f"SeedLast{i}",
                    f"{100 + i} SEED AVE",
                    "CA" if i % 2 == 0 else "NY",
                    f"seed.user.{i}@example.org",
                    1,
                    now,
                )
                for i in range(1, seed_rows + 1)
            ]
            self._conn.executemany(
                """
                INSERT INTO records (first_name, last_name, street, state, email, version, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                batch,
            )

        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def _send(
                self,
                status: int,
                body: Any = None,
                etag: Optional[str] = None,
            ) -> None:
                raw = json.dumps(body).encode("utf-8") if body is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                if etag is not None:
                    self.send_header("ETag", etag)
                self.end_headers()
                if raw:
                    self.wfile.write(raw)

            def _read_json(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length) if length > 0 else b"{}"
                return json.loads(raw.decode("utf-8"))

            def do_GET(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path.rstrip("/") or "/"
                qs = dict(urllib.parse.parse_qsl(parsed.query))
                if path == "/api/records":
                    with parent._lock:
                        if "email" in qs:
                            rows = [
                                dict(r)
                                for r in parent._conn.execute(
                                    "SELECT * FROM records WHERE email = ? LIMIT 200",
                                    (qs["email"].strip().lower(),),
                                ).fetchall()
                            ]
                        else:
                            # Hard 200-row cap on collection endpoint!
                            rows = [
                                dict(r)
                                for r in parent._conn.execute(
                                    "SELECT * FROM records ORDER BY id ASC LIMIT 200"
                                ).fetchall()
                            ]
                    self._send(200, {"records": rows, "capped_at": 200})
                    return
                if path.startswith("/api/records/"):
                    rid = path.split("/")[-1]
                    if not rid.isdigit():
                        self._send(404, {"error": "not found"})
                        return
                    with parent._lock:
                        row = parent._conn.execute(
                            "SELECT * FROM records WHERE id = ?", (int(rid),)
                        ).fetchone()
                    if row is None:
                        self._send(404, {"error": "not found"})
                    else:
                        d = dict(row)
                        self._send(200, d, etag=f'W/"v{d["version"]}"')
                    return
                self._send(404, {"error": "not found"})

            def do_POST(self) -> None:
                path = urllib.parse.urlparse(self.path).path.rstrip("/")
                if path != "/api/records":
                    self._send(404, {"error": "not found"})
                    return
                data = self._read_json()
                first_name = str(data.get("first_name", "")).strip()
                last_name = str(data.get("last_name", "")).strip().title()
                street = str(data.get("street", "")).strip().upper()
                state = _normalize_state(str(data.get("state", "")))
                email = str(data.get("email", "")).strip().lower()
                now = _utc_now_iso()
                try:
                    with parent._lock, parent._conn:
                        cur = parent._conn.execute(
                            """
                            INSERT INTO records (first_name, last_name, street, state, email, version, updated_at)
                            VALUES (?, ?, ?, ?, ?, 1, ?)
                            """,
                            (first_name, last_name, street, state, email, now),
                        )
                        new_id = cur.lastrowid
                        row = parent._conn.execute(
                            "SELECT * FROM records WHERE id = ?", (new_id,)
                        ).fetchone()
                except sqlite3.IntegrityError as exc:
                    self._send(409, {"error": str(exc)})
                    return
                d = dict(row)
                self._send(201, d, etag=f'W/"v{d["version"]}"')

            def do_PATCH(self) -> None:
                path = urllib.parse.urlparse(self.path).path.rstrip("/")
                if not path.startswith("/api/records/"):
                    self._send(404, {"error": "not found"})
                    return
                rid = path.split("/")[-1]
                if not rid.isdigit():
                    self._send(404, {"error": "not found"})
                    return
                data = self._read_json()
                with parent._lock, parent._conn:
                    existing = parent._conn.execute(
                        "SELECT * FROM records WHERE id = ?", (int(rid),)
                    ).fetchone()
                    if existing is None:
                        self._send(404, {"error": "not found"})
                        return
                    curr = dict(existing)
                    first_name = (
                        str(data["first_name"]).strip()
                        if "first_name" in data
                        else curr["first_name"]
                    )
                    last_name = (
                        str(data["last_name"]).strip().title()
                        if "last_name" in data
                        else curr["last_name"]
                    )
                    street = (
                        str(data["street"]).strip().upper()
                        if "street" in data
                        else curr["street"]
                    )
                    state = (
                        _normalize_state(str(data["state"]))
                        if "state" in data
                        else curr["state"]
                    )
                    email = (
                        str(data["email"]).strip().lower()
                        if "email" in data
                        else curr["email"]
                    )
                    new_ver = int(curr["version"]) + 1
                    now = _utc_now_iso()
                    parent._conn.execute(
                        """
                        UPDATE records
                        SET first_name = ?, last_name = ?, street = ?, state = ?, email = ?, version = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            first_name,
                            last_name,
                            street,
                            state,
                            email,
                            new_ver,
                            now,
                            int(rid),
                        ),
                    )
                    updated = parent._conn.execute(
                        "SELECT * FROM records WHERE id = ?", (int(rid),)
                    ).fetchone()
                d = dict(updated)
                self._send(200, d, etag=f'W/"v{d["version"]}"')

            def do_DELETE(self) -> None:
                path = urllib.parse.urlparse(self.path).path.rstrip("/")
                if not path.startswith("/api/records/"):
                    self._send(404, {"error": "not found"})
                    return
                rid = path.split("/")[-1]
                if not rid.isdigit():
                    self._send(404, {"error": "not found"})
                    return
                with parent._lock, parent._conn:
                    parent._conn.execute(
                        "DELETE FROM records WHERE id = ?", (int(rid),)
                    )
                self._send(204, None)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def count_rows(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS c FROM records").fetchone()
            return int(row["c"])

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=3.0)
        self._conn.close()


@pytest.fixture()
def clobber_server(tmp_path: Path) -> Iterator[_SilentClobberTaskServer]:
    srv = _SilentClobberTaskServer(tmp_path / "tasks.sqlite")
    srv.start()
    try:
        yield srv
    finally:
        srv.close()


@pytest.fixture()
def normalizing_server(tmp_path: Path) -> Iterator[_PaginatedNormalizingRecordServer]:
    srv = _PaginatedNormalizingRecordServer(tmp_path / "records.sqlite", seed_rows=2000)
    srv.start()
    try:
        yield srv
    finally:
        srv.close()


# ==============================================================================
# Test 1: Silent-Clobber PUT Archetype
# ==============================================================================


def test_silent_clobber_put_archetype_induces_replace_and_certifies(
    clobber_server: _SilentClobberTaskServer,
) -> None:
    """1 create + 1 full PUT update HAR -> auto-discovers write_mode='replace' & certifies 1.0/1.0."""
    entries: list[dict[str, Any]] = []

    # Include static asset and health-check noise in the raw HAR trace.
    _, _, h_health = _http_call_and_record_har_entry(
        "GET", f"{clobber_server.base_url}/health"
    )
    _, _, h_js = _http_call_and_record_har_entry(
        "GET", f"{clobber_server.base_url}/static/bundle.js"
    )
    entries.extend([h_health, h_js])

    # 1. Demonstrated Create (POST /api/tasks).
    st_create, created, h_create = _http_call_and_record_har_entry(
        "POST",
        f"{clobber_server.base_url}/api/tasks",
        payload={
            "title": "Prepare quarterly review",
            "description": "Collect metrics from all engineering lanes",
            "priority": 3,
            "assignee": "alex",
        },
    )
    assert st_create == 201
    task_id = created["id"]
    entries.append(h_create)

    # 2. Demonstrated Full Update (PUT /api/tasks/{id} with all fields supplied, so HAR
    # alone does NOT reveal that omitting fields silently wipes them to "" / 0!).
    st_put, _updated, h_put = _http_call_and_record_har_entry(
        "PUT",
        f"{clobber_server.base_url}/api/tasks/{task_id}",
        payload={
            "title": "Finalize quarterly review",
            "description": "Collect metrics from all engineering lanes",
            "priority": 3,
            "assignee": "alex",
        },
    )
    assert st_put == 200
    entries.append(h_put)

    har = {
        "log": {
            "version": "1.2",
            "creator": {"name": "recorder", "version": "1.0"},
            "entries": entries,
        }
    }

    candidate = induce_lens_from_har(har)
    assert candidate.schema_version == ENTITY_LENS_SCHEMA
    assert candidate.entity_name == "task"
    assert candidate.base_url == clobber_server.base_url
    assert candidate.id_field == "id"
    assert candidate.get_one.method == "GET"
    assert candidate.get_one.path_template == "/api/tasks/{id}"
    assert candidate.write_endpoint.method == "PUT"
    assert candidate.write_endpoint.path_template == "/api/tasks/{id}"
    assert candidate.create_endpoint is not None
    assert candidate.create_endpoint.method == "POST"
    assert candidate.create_endpoint.path_template == "/api/tasks"
    assert "created_at" in candidate.volatile_fields
    assert "updated_at" in candidate.volatile_fields

    # Confirm the server really DOES silently clobber omitted fields on raw PUT!
    st_raw, clobbered_body, _ = _http_call_and_record_har_entry(
        "PUT",
        f"{clobber_server.base_url}/api/tasks/{task_id}",
        payload={"title": "Partial PUT only"},
    )
    assert st_raw == 200
    assert clobbered_body["description"] == ""
    assert clobbered_body["priority"] == 0

    # Run empirical quirk discovery + 6 adversarial/concurrency probes.
    pack = certify_lens_on_sandbox(
        candidate,
        sample_record={
            "title": "Audit database backups",
            "description": "Verify point-in-time recovery",
            "priority": 2,
            "assignee": "jordan",
        },
        sample_update={"title": "Audit and sign off database backups"},
    )

    assert pack.schema_version == CERTIFIED_LENS_PACK_SCHEMA
    assert pack.lens.write_mode == "replace"
    assert pack.sensitivity == 1.0
    assert pack.specificity == 1.0
    assert pack.certified is True
    assert len(pack.probes) == 6
    assert {p.probe_name for p in pack.probes} == {
        "clean_roundtrip",
        "benign_concurrent_other_record",
        "fault_partial_clobber",
        "fault_wrong_value",
        "fault_wrong_record",
        "fault_no_persist",
    }
    assert all(p.passed for p in pack.probes)
    assert len(pack.pack_sha256) == 64
    assert pack.pack_sha256 == pack.with_computed_digest().pack_sha256


# ==============================================================================
# Test 2: Paginated (2,000 Rows) + Field-Normalizing Server + Safety Gate
# ==============================================================================


def test_paginated_normalizing_2000_row_archetype_and_remote_refusal(
    normalizing_server: _PaginatedNormalizingRecordServer,
) -> None:
    """2,000 pre-seeded rows, 200-row cap, codecs + 'Massachusetts'->'MA' bijection, live bg inserts."""
    assert normalizing_server.count_rows() == 2000

    entries: list[dict[str, Any]] = []

    # 1. Capped collection GET returning only 200 of 2,000 rows.
    st_list, list_body, h_list = _http_call_and_record_har_entry(
        "GET", f"{normalizing_server.base_url}/api/records?limit=200"
    )
    assert st_list == 200
    assert len(list_body["records"]) == 200
    entries.append(h_list)

    # 2. Pre-write duplicate check GET /api/records?email=...
    st_dup, dup_body, h_dup = _http_call_and_record_har_entry(
        "GET",
        f"{normalizing_server.base_url}/api/records?email=claire.bennett%40example.org",
    )
    assert st_dup == 200
    assert dup_body["records"] == []
    entries.append(h_dup)

    # 3. Create record (> row #2000, invisible to GET /api/records!) with unnormalized fields.
    st_post, created, h_post = _http_call_and_record_har_entry(
        "POST",
        f"{normalizing_server.base_url}/api/records",
        payload={
            "first_name": "  Claire  ",
            "last_name": "bennett",
            "street": "742 evergreen terrace",
            "state": "Massachusetts",
            "email": "Claire.Bennett@Example.org",
        },
    )
    assert st_post == 201
    rec_id = created["id"]
    assert rec_id == 2001
    assert created["street"] == "742 EVERGREEN TERRACE"
    assert created["last_name"] == "Bennett"
    assert created["state"] == "MA"
    entries.append(h_post)

    # 4. Partial update PATCH /api/records/{id} + DELETE cleanup demonstrated in HAR.
    st_patch, patched, h_patch = _http_call_and_record_har_entry(
        "PATCH",
        f"{normalizing_server.base_url}/api/records/{rec_id}",
        payload={"street": "100 memorial drive", "last_name": "de la cruz"},
    )
    assert st_patch == 200
    assert patched["street"] == "100 MEMORIAL DRIVE"
    assert patched["last_name"] == "De La Cruz"
    assert patched["state"] == "MA"
    entries.append(h_patch)

    st_del, _, h_del = _http_call_and_record_har_entry(
        "DELETE", f"{normalizing_server.base_url}/api/records/{rec_id}"
    )
    assert st_del == 204
    entries.append(h_del)

    har = {"log": {"version": "1.2", "entries": entries}}
    candidate = induce_lens_from_har(har)

    assert candidate.entity_name == "record"
    assert candidate.id_field == "id"
    assert candidate.get_one.path_template == "/api/records/{id}"
    assert candidate.write_endpoint.method == "PATCH"
    assert candidate.delete_endpoint is not None
    assert candidate.delete_endpoint.method == "DELETE"
    assert candidate.duplicate_search is not None
    assert candidate.duplicate_search.path_template == "/api/records"
    assert candidate.duplicate_search.query_params == {"email": "{email}"}
    assert candidate.duplicate_search.records_key == "records"

    # Verify induced codecs & discrete bijection!
    assert candidate.field_codecs["street"].kind == "upper"
    assert candidate.field_codecs["last_name"].kind == "capitalize_words"
    assert "state" not in candidate.field_codecs
    # Tested data override supplied by this synthetic server's declared contract.
    candidate = candidate.model_copy(
        update={
            "field_codecs": {
                **candidate.field_codecs,
                "state": FieldCodec(kind="bijection", mapping={"Massachusetts": "MA"}),
            }
        }
    )
    assert candidate.field_codecs["first_name"].kind == "strip_whitespace"
    assert candidate.field_codecs["email"].kind == "lower"

    # Safety gate: MUST refuse non-loopback URLs unless allow_remote_sandbox=True!
    remote_candidate = candidate.model_copy(
        update={"base_url": "https://staging.internal.example.com"}
    )
    with pytest.raises(SandboxSafetyError):
        certify_lens_on_sandbox(
            remote_candidate,
            sample_record={
                "first_name": "Dana",
                "last_name": "scully",
                "street": "15 beacon st",
                "state": "Massachusetts",
                "email": "dana@example.org",
            },
            sample_update={"street": "88 boylston st"},
        )

    remote_ip_candidate = candidate.model_copy(
        update={"base_url": "http://198.51.100.42:8080"}
    )
    with pytest.raises(SandboxSafetyError):
        certify_lens_on_sandbox(
            remote_ip_candidate,
            sample_record={
                "first_name": "Dana",
                "last_name": "scully",
                "street": "15 beacon st",
                "state": "Massachusetts",
                "email": "dana@example.org",
            },
            sample_update={"street": "88 boylston st"},
        )

    # Start a live background thread continuously inserting rows into the 2,000-row table
    # throughout sandbox certification!
    stop_bg = threading.Event()
    bg_counter = {"inserted": 0}

    def _background_inserter() -> None:
        idx = 0
        while not stop_bg.is_set():
            idx += 1
            _http_call_and_record_har_entry(
                "POST",
                f"{normalizing_server.base_url}/api/records",
                payload={
                    "first_name": "BgWorker",
                    "last_name": f"runner{idx}",
                    "street": f"{idx} noisy lane",
                    "state": "Massachusetts",
                    "email": f"bg.noise.{idx}.{time.time_ns()}@example.org",
                },
            )
            bg_counter["inserted"] += 1
            time.sleep(0.005)

    bg_thread = threading.Thread(target=_background_inserter, daemon=True)
    bg_thread.start()
    try:
        rows_before = normalizing_server.count_rows()
        pack = certify_lens_on_sandbox(
            candidate,
            sample_record={
                "first_name": "  Evelyn ",
                "last_name": "mcbride",
                "street": "42 commonwealth ave",
                "state": "Massachusetts",
                "email": "evelyn.mcbride@example.org",
            },
            sample_update={
                "street": "99 newbury street",
                "last_name": "o'connor",
                "state": "Massachusetts",
            },
        )
    finally:
        stop_bg.set()
        bg_thread.join(timeout=3.0)

    assert bg_counter["inserted"] >= 1
    assert pack.lens.write_mode == "patch"
    assert pack.sensitivity == 1.0
    assert pack.specificity == 1.0
    assert pack.certified is True
    assert all(p.passed for p in pack.probes)
    # Disposable probe records were cleaned up via delete_endpoint (only bg thread rows remain).
    assert normalizing_server.count_rows() == rows_before + bg_counter["inserted"]


def test_allow_remote_sandbox_opt_in_bypasses_loopback_guard(
    clobber_server: _SilentClobberTaskServer,
) -> None:
    """Passing allow_remote_sandbox=True explicitly permits non-IP/custom host validation."""
    # Verify loopback check passes with allow_remote_sandbox=True even on unreachable remote
    # (fails certification probes cleanly with certified=False instead of SandboxSafetyError).
    remote_lens = EntityLensSpec(
        entity_name="task",
        base_url="http://sandbox.example.invalid:8080",
        get_one=EndpointTemplate(method="GET", path_template="/api/tasks/{id}"),
        write_endpoint=EndpointTemplate(method="PUT", path_template="/api/tasks/{id}"),
        create_endpoint=EndpointTemplate(method="POST", path_template="/api/tasks"),
    )
    with pytest.raises(SandboxSafetyError):
        certify_lens_on_sandbox(
            remote_lens,
            sample_record={"title": "a", "description": "b"},
            sample_update={"title": "c"},
            allow_remote_sandbox=False,
        )

    pack = certify_lens_on_sandbox(
        remote_lens,
        sample_record={"title": "a", "description": "b"},
        sample_update={"title": "c"},
        allow_remote_sandbox=True,
    )
    assert pack.certified is False


# ==============================================================================
# Test 3: Weak / Broken Custom Lens Rejection Tests
# ==============================================================================


def test_broken_custom_lenses_fail_certification(
    clobber_server: _SilentClobberTaskServer,
    normalizing_server: _PaginatedNormalizingRecordServer,
) -> None:
    """Broken get_one or clobber-ignoring lenses MUST yield certified == False."""
    sample_rec = {
        "title": "Keep frame intact",
        "description": "Critical compliance notes",
        "priority": 5,
        "assignee": "morgan",
    }
    sample_upd = {"title": "Updated title"}

    # 3a. Wrong get_one endpoint (404).
    wrong_get_lens = EntityLensSpec(
        entity_name="task",
        base_url=clobber_server.base_url,
        write_mode="replace",
        get_one=EndpointTemplate(method="GET", path_template="/api/wrong_tasks/{id}"),
        write_endpoint=EndpointTemplate(method="PUT", path_template="/api/tasks/{id}"),
        create_endpoint=EndpointTemplate(method="POST", path_template="/api/tasks"),
        identity_fields=("assignee",),
    )
    pack_wrong_get = certify_lens_on_sandbox(
        wrong_get_lens, sample_record=sample_rec, sample_update=sample_upd
    )
    assert pack_wrong_get.certified is False
    assert pack_wrong_get.specificity < 1.0

    # 3b. Broken get_one pointing at the 200-capped collection endpoint instead of /{id}.
    capped_collection_lens = EntityLensSpec(
        entity_name="record",
        base_url=normalizing_server.base_url,
        write_mode="patch",
        get_one=EndpointTemplate(
            method="GET", path_template="/api/records", records_key="records"
        ),
        write_endpoint=EndpointTemplate(
            method="PATCH", path_template="/api/records/{id}"
        ),
        create_endpoint=EndpointTemplate(method="POST", path_template="/api/records"),
        identity_fields=("email",),
    )
    pack_collection_get = certify_lens_on_sandbox(
        capped_collection_lens,
        sample_record={
            "first_name": "Sam",
            "last_name": "Carter",
            "street": "10 MAIN ST",
            "state": "MA",
            "email": "sam.carter@example.org",
        },
        sample_update={"street": "20 ELM ST"},
    )
    assert pack_collection_get.certified is False

    # 3c. Broken lens that ignores clobbered fields by marking user business fields volatile!
    clobber_blind_lens = EntityLensSpec(
        entity_name="task",
        base_url=clobber_server.base_url,
        write_mode="replace",
        get_one=EndpointTemplate(method="GET", path_template="/api/tasks/{id}"),
        write_endpoint=EndpointTemplate(method="PUT", path_template="/api/tasks/{id}"),
        create_endpoint=EndpointTemplate(method="POST", path_template="/api/tasks"),
        identity_fields=("assignee",),
        volatile_fields=(
            "description",
            "priority",
            "assignee",
            "created_at",
            "updated_at",
        ),
    )
    pack_clobber_blind = certify_lens_on_sandbox(
        clobber_blind_lens, sample_record=sample_rec, sample_update=sample_upd
    )
    assert pack_clobber_blind.certified is False
    assert pack_clobber_blind.sensitivity < 1.0
    clobber_probe = next(
        p for p in pack_clobber_blind.probes if p.probe_name == "fault_partial_clobber"
    )
    assert clobber_probe.passed is False
    assert clobber_probe.observed_reject is False

    # 3d. Weak custom judge that checks only intended_delta and ignores frame preservation.
    class _WeakIntentOnlyJudge:
        def evaluate(
            self,
            r0: Optional[Mapping[str, Any]],
            r1: Mapping[str, Any],
            intended_delta: Mapping[str, Any],
            *,
            volatile_fields: tuple[str, ...] = (),
            field_codecs: Optional[Mapping[str, FieldCodec]] = None,
        ) -> JudgeVerdict:
            ok = all(r1.get(k) == v for k, v in intended_delta.items())
            return JudgeVerdict(passed=ok, frame_preserved=True, intent_satisfied=ok)

    healthy_candidate = EntityLensSpec(
        entity_name="task",
        base_url=clobber_server.base_url,
        write_mode="replace",
        get_one=EndpointTemplate(method="GET", path_template="/api/tasks/{id}"),
        write_endpoint=EndpointTemplate(method="PUT", path_template="/api/tasks/{id}"),
        create_endpoint=EndpointTemplate(method="POST", path_template="/api/tasks"),
        identity_fields=("assignee",),
        volatile_fields=("created_at", "updated_at"),
    )
    pack_weak_judge = certify_lens_on_sandbox(
        healthy_candidate,
        sample_record=sample_rec,
        sample_update=sample_upd,
        judge=_WeakIntentOnlyJudge(),
    )
    assert pack_weak_judge.certified is False
    assert pack_weak_judge.sensitivity < 1.0

    # 3e. Unrefined naive patch lens on silent-clobber PUT server fails clean_roundtrip.
    naive_patch_lens = healthy_candidate.model_copy(update={"write_mode": "patch"})
    pack_unrefined = certify_lens_on_sandbox(
        naive_patch_lens,
        sample_record=sample_rec,
        sample_update=sample_upd,
        refine_quirks=False,
    )
    assert pack_unrefined.certified is False
    assert pack_unrefined.specificity < 1.0


# ==============================================================================
# Test 4: Additional Codecs, Create-Only Synthesis, LLM Proposer Gate & Boundaries
# ==============================================================================


def test_intended_inputs_codecs_phone_isodate_and_llm_proposer_veto(
    clobber_server: _SilentClobberTaskServer,
) -> None:
    """Verify phone_digits, iso_date, intended_inputs, create-only synthesis, and llm_proposer."""
    assert infer_field_codec("(617) 555-0199", "6175550199") == FieldCodec(
        kind="phone_digits"
    )
    assert infer_field_codec("March 15, 2026", "2026-03-15") is None

    judge = DefaultFrameAndIntentJudge()
    verdict = judge.evaluate(
        {"id": 1, "phone": "6175550100", "dob": "2000-01-01", "note": "keep"},
        {"id": 1, "phone": "6175550199", "dob": "2026-03-15", "note": "keep"},
        {"phone": "+1 (617) 555-0199", "dob": "03/15/2026"},
        field_codecs={
            "phone": FieldCodec(kind="phone_digits"),
            "dob": FieldCodec(kind="bijection", mapping={"03/15/2026": "2026-03-15"}),
        },
    )
    assert verdict.passed is True

    # Create-only HAR (only POST /api/tasks observed -> synthesizes GET /api/tasks/{id}
    # and sets write_mode="create") + intended_inputs codec discovery.
    _, _created, h_post = _http_call_and_record_har_entry(
        "POST",
        f"{clobber_server.base_url}/api/tasks",
        payload={
            "title": "MA",
            "description": "Synthesize get_one from create-only HAR",
            "priority": 1,
            "assignee": "riley",
        },
    )
    create_only_har = {"log": {"version": "1.2", "entries": [h_post]}}
    create_lens = induce_lens_from_har(
        create_only_har,
        intended_inputs={"title": "Massachusetts"},
    )
    assert create_lens.write_mode == "create"
    assert create_lens.get_one.path_template == "/api/tasks/{id}"
    assert create_lens.write_endpoint.method == "POST"
    assert create_lens.field_codecs["title"].kind == "exact"
    assert (
        not FrameAndIntentJudge()
        .evaluate(
            None,
            {"title": "MA"},
            {"title": "Massachusetts"},
            field_codecs=create_lens.field_codecs,
        )
        .passed
    )

    pack_create = certify_lens_on_sandbox(
        create_lens,
        sample_record={
            "title": "MA",
            "description": "Create workflow check",
            "priority": 2,
            "assignee": "taylor",
        },
        sample_update={"priority": 4},
    )
    assert pack_create.certified is True
    assert pack_create.sensitivity == 1.0
    assert pack_create.specificity == 1.0

    # LLM proposer hook: valid proposal passes; hallucinated proposal gets vetoed!
    called = {"count": 0}

    def _bad_llm_proposer(_har: Mapping[str, Any]) -> EntityLensSpec:
        called["count"] += 1
        return EntityLensSpec(
            entity_name="task",
            base_url=clobber_server.base_url,
            get_one=EndpointTemplate(
                method="GET", path_template="/api/hallucinated/{id}"
            ),
            write_endpoint=EndpointTemplate(
                method="PUT", path_template="/api/tasks/{id}"
            ),
            create_endpoint=EndpointTemplate(method="POST", path_template="/api/tasks"),
        )

    bad_proposed = induce_lens_from_har(create_only_har, llm_proposer=_bad_llm_proposer)
    assert called["count"] == 1
    vetoed_pack = certify_lens_on_sandbox(
        bad_proposed,
        sample_record={
            "title": "Test",
            "description": "Desc",
            "priority": 1,
            "assignee": "casey",
        },
        sample_update={"title": "Test 2"},
    )
    assert vetoed_pack.certified is False

    def _good_llm_proposer(_har: Mapping[str, Any]) -> EntityLensSpec:
        return EntityLensSpec(
            entity_name="task",
            base_url=clobber_server.base_url,
            write_mode="patch",
            get_one=EndpointTemplate(method="GET", path_template="/api/tasks/{id}"),
            write_endpoint=EndpointTemplate(
                method="PUT", path_template="/api/tasks/{id}"
            ),
            create_endpoint=EndpointTemplate(method="POST", path_template="/api/tasks"),
            delete_endpoint=EndpointTemplate(
                method="DELETE", path_template="/api/tasks/{id}"
            ),
        )

    good_proposed = induce_lens_from_har(
        create_only_har, llm_proposer=_good_llm_proposer
    )
    admitted_pack = certify_lens_on_sandbox(
        good_proposed,
        sample_record={
            "title": "LLM valid candidate",
            "description": "Upgraded to replace by sandbox quirk probe",
            "priority": 3,
            "assignee": "drew",
        },
        sample_update={"title": "LLM certified candidate"},
        auth_headers={"X-Sandbox-Token": "verified-token"},
    )
    assert admitted_pack.certified is True
    assert admitted_pack.lens.write_mode == "replace"
    assert admitted_pack.sensitivity == 1.0
    assert admitted_pack.specificity == 1.0


def test_har_non_id_pk_enveloped_base64_form_and_error_cases() -> None:
    """Verify pid/uuid matching, PUT-only synthesis, base64 HAR content, form body, and errors."""
    import base64

    resp_obj = {
        "data": {
            "pid": "ent-42",
            "full_name": "Jordan Lee",
            "contact_email": "jordan@example.org",
            "meta": {
                "versionId": "2",
                "lastUpdated": "2026-05-01T12:00:00Z",
            },
        }
    }
    encoded = base64.b64encode(json.dumps(resp_obj).encode("utf-8")).decode("ascii")

    put_only_har = {
        "log": {
            "version": "1.2",
            "entries": [
                {
                    "request": {
                        "method": "PUT",
                        "url": "http://127.0.0.1:9999/v2/profiles/ent-42",
                        "postData": {
                            "mimeType": "application/x-www-form-urlencoded",
                            "text": "full_name=jordan+lee&contact_email=JORDAN%40EXAMPLE.ORG",
                        },
                    },
                    "response": {
                        "status": 200,
                        "headers": [{"name": "ETag", "value": '"v2"'}],
                        "content": {
                            "mimeType": "application/json",
                            "encoding": "base64",
                            "text": encoded,
                        },
                    },
                }
            ],
        }
    }
    spec = induce_lens_from_har(put_only_har)
    assert spec.entity_name == "profile"
    assert spec.id_field == "pid"
    assert spec.get_one.path_template == "/v2/profiles/{id}"
    assert spec.get_one.records_key == "data"
    assert spec.get_one.id_field == "pid"
    assert spec.write_endpoint.method == "PUT"
    assert spec.write_endpoint.path_template == "/v2/profiles/{id}"
    assert spec.field_codecs["full_name"].kind == "capitalize_words"
    assert spec.field_codecs["contact_email"].kind == "lower"
    assert "meta.lastUpdated" in spec.volatile_fields
    assert "meta.versionId" in spec.volatile_fields

    with pytest.raises(ValueError):
        induce_lens_from_har({})

    with pytest.raises(ValueError):
        induce_lens_from_har(
            {
                "log": {
                    "entries": [
                        {
                            "request": {
                                "method": "GET",
                                "url": "http://127.0.0.1:8000/static/app.css",
                            },
                            "response": {
                                "status": 200,
                                "content": {"mimeType": "text/css", "text": "body{}"},
                            },
                        }
                    ]
                }
            }
        )


def test_induce_generality_and_import_boundaries() -> None:
    """Enforce zero banned app names and zero vision/ML/HTTP-client imports in induce.py."""
    induce_path = (
        Path(__file__).resolve().parents[1] / "openadapt_flow" / "tx" / "induce.py"
    )
    source_lower = induce_path.read_text(encoding="utf-8").lower()

    banned_tokens = (
        "open" + "emr",
        "med" + "plum",
        "vik" + "unja",
        "doli" + "barr",
        "open" + "imis",
        "app" + "world",
        "work" + "arena",
    )
    for token in banned_tokens:
        assert (
            token not in source_lower
        ), f"Banned app name {token!r} found in induce.py"

    refused_modules = [
        "numpy",
        "cv2",
        "PIL",
        "rapidocr_onnxruntime",
        "onnxruntime",
        "playwright",
        "httpx",
        "requests",
    ]
    check_script = (
        "import json, sys\n"
        "REFUSED = set(json.loads(sys.argv[1]))\n"
        "class Blocker:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.partition('.')[0] in REFUSED:\n"
        "            raise ImportError(f'Refused import: {name}')\n"
        "        return None\n"
        "sys.meta_path.insert(0, Blocker())\n"
        "import openadapt_flow.tx.induce as m\n"
        "loaded = sorted(k for k in sys.modules if k.partition('.')[0] in REFUSED)\n"
        "print(json.dumps({'exports': sorted(m.__all__), 'loaded': loaded}))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", check_script, json.dumps(refused_modules)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["loaded"] == []
    assert "induce_lens_from_har" in out["exports"]
    assert "certify_lens_on_sandbox" in out["exports"]

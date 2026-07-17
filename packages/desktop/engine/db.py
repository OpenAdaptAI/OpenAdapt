"""SQLite index database for capture metadata and upload queue.

Provides a thin wrapper around sqlite3 for all persistent state:
captures table (metadata, review status, tier) and upload_jobs table
(persistent queue that survives restarts).

Uses WAL mode for concurrent read access from monitoring threads.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_CAPTURES_COLUMNS = frozenset({
    "capture_path", "scrubbed_path", "started_at", "stopped_at",
    "duration_secs", "event_count", "size_bytes", "review_status",
    "tier", "archive_path", "task_description", "created_at",
})

_UPLOAD_JOB_COLUMNS = frozenset({
    "status", "archive_path", "remote_url", "bytes_sent",
    "error", "completed_at", "attempts", "next_retry_at",
})

# Compiled workflow bundles (local mirror of a compile step).
_BUNDLE_COLUMNS = frozenset({
    "capture_id", "bundle_path", "workflow_name", "workflow_id",
    "version", "steps", "schema_version", "status", "compiled_at",
})

# Local replay/run executions of a bundle.
_RUN_COLUMNS = frozenset({
    "bundle_id", "run_path", "report_path", "status",
    "duration_secs", "steps", "finished_at",
})

# Open halts (the local mirror of cloud needs-attention).
_HALT_COLUMNS = frozenset({
    "run_id", "workflow_id", "step_intent", "reason", "resolver_rung",
    "drift_signature", "status", "teach_url", "resolved_at",
})

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS captures (
    capture_id TEXT PRIMARY KEY,
    capture_path TEXT NOT NULL,
    scrubbed_path TEXT,
    started_at TEXT NOT NULL,
    stopped_at TEXT,
    duration_secs REAL,
    event_count INTEGER DEFAULT 0,
    size_bytes INTEGER DEFAULT 0,
    review_status TEXT DEFAULT 'captured',
    tier TEXT DEFAULT 'hot',
    archive_path TEXT,
    task_description TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS upload_jobs (
    job_id TEXT PRIMARY KEY,
    capture_id TEXT NOT NULL REFERENCES captures(capture_id),
    backend_name TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    archive_path TEXT,
    remote_url TEXT,
    bytes_sent INTEGER DEFAULT 0,
    error TEXT,
    attempts INTEGER DEFAULT 0,
    next_retry_at TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS bundles (
    bundle_id TEXT PRIMARY KEY,
    capture_id TEXT REFERENCES captures(capture_id),
    bundle_path TEXT NOT NULL,
    workflow_name TEXT DEFAULT '',
    workflow_id TEXT,
    version INTEGER DEFAULT 1,
    steps INTEGER DEFAULT 0,
    schema_version INTEGER DEFAULT 2,
    status TEXT DEFAULT 'compiled',
    created_at TEXT NOT NULL,
    compiled_at TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    bundle_id TEXT REFERENCES bundles(bundle_id),
    run_path TEXT NOT NULL,
    report_path TEXT,
    status TEXT DEFAULT 'pending',
    duration_secs REAL,
    steps INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS halts (
    halt_id TEXT PRIMARY KEY,
    run_id TEXT REFERENCES runs(run_id),
    workflow_id TEXT,
    step_intent TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    resolver_rung TEXT,
    drift_signature TEXT,
    status TEXT DEFAULT 'open',
    teach_url TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_captures_review ON captures(review_status);
CREATE INDEX IF NOT EXISTS idx_captures_tier ON captures(tier);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON upload_jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_capture ON upload_jobs(capture_id);
CREATE INDEX IF NOT EXISTS idx_bundles_capture ON bundles(capture_id);
CREATE INDEX IF NOT EXISTS idx_runs_bundle ON runs(bundle_id);
CREATE INDEX IF NOT EXISTS idx_halts_status ON halts(status);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class IndexDB:
    """SQLite index database for capture metadata and upload queue."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def initialize(self) -> None:
        """Open or create the database, enable WAL mode, create tables."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns/tables missing from databases created by older versions."""
        existing = {
            r["name"]
            for r in self._conn.execute("PRAGMA table_info(upload_jobs)").fetchall()
        }
        for col, ddl in (
            ("attempts", "ALTER TABLE upload_jobs ADD COLUMN attempts INTEGER DEFAULT 0"),
            ("next_retry_at", "ALTER TABLE upload_jobs ADD COLUMN next_retry_at TEXT"),
        ):
            if col not in existing:
                self._conn.execute(ddl)
        self._conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database not initialized -- call initialize() first")
        return self._conn

    # --- Capture operations ---

    def insert_capture(
        self,
        capture_id: str,
        capture_path: str,
        started_at: str,
        *,
        task_description: str = "",
    ) -> None:
        """Insert a new capture record."""
        self.conn.execute(
            "INSERT INTO captures"
            " (capture_id, capture_path, started_at, task_description, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (capture_id, capture_path, started_at, task_description, _now()),
        )
        self.conn.commit()

    def get_capture(self, capture_id: str) -> dict | None:
        """Get a single capture by ID."""
        row = self.conn.execute(
            "SELECT * FROM captures WHERE capture_id = ?", (capture_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_captures(
        self,
        limit: int = 10,
        review_status: str | None = None,
        tier: str | None = None,
    ) -> list[dict]:
        """List captures, newest first. Optional filter by status/tier."""
        query = "SELECT * FROM captures WHERE 1=1"
        params: list = []
        if review_status:
            query += " AND review_status = ?"
            params.append(review_status)
        if tier:
            query += " AND tier = ?"
            params.append(tier)
        query += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_pending_reviews(self) -> list[dict]:
        """Get captures in 'captured' or 'scrubbed' status."""
        rows = self.conn.execute(
            "SELECT * FROM captures WHERE review_status IN ('captured', 'scrubbed')"
            " ORDER BY started_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def update_capture(self, capture_id: str, **fields: object) -> None:
        """Update specific fields on a capture record."""
        if not fields:
            return
        bad = set(fields) - _CAPTURES_COLUMNS
        if bad:
            raise ValueError(f"Unknown capture fields: {bad}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [capture_id]
        self.conn.execute(f"UPDATE captures SET {sets} WHERE capture_id = ?", vals)
        self.conn.commit()

    def delete_capture(self, capture_id: str) -> None:
        """Delete a capture record from the database."""
        self.conn.execute("DELETE FROM captures WHERE capture_id = ?", (capture_id,))
        self.conn.commit()

    # --- Upload job operations ---

    def insert_upload_job(
        self, job_id: str, capture_id: str, backend_name: str
    ) -> None:
        """Create a new upload job in 'pending' status."""
        now = _now()
        self.conn.execute(
            "INSERT INTO upload_jobs (job_id, capture_id, backend_name, created_at, completed_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (job_id, capture_id, backend_name, now, now),
        )
        self.conn.commit()

    def get_pending_jobs(self) -> list[dict]:
        """Get all jobs in 'pending' status, ordered by created_at."""
        rows = self.conn.execute(
            "SELECT * FROM upload_jobs WHERE status = 'pending' ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_due_jobs(self, now: str | None = None) -> list[dict]:
        """Get pending jobs whose retry backoff (if any) has elapsed.

        A job is due when it is pending and either has no ``next_retry_at`` or
        its ``next_retry_at`` is at/before ``now`` (defaults to the current UTC
        timestamp). This is the durable/offline queue's drain query.
        """
        now = now or _now()
        rows = self.conn.execute(
            "SELECT * FROM upload_jobs WHERE status = 'pending'"
            " AND (next_retry_at IS NULL OR next_retry_at <= ?)"
            " ORDER BY created_at",
            (now,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_jobs_for_capture(self, capture_id: str) -> list[dict]:
        """Get all upload jobs for a specific capture."""
        rows = self.conn.execute(
            "SELECT * FROM upload_jobs WHERE capture_id = ? ORDER BY created_at",
            (capture_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def update_upload_job(self, job_id: str, **fields: object) -> None:
        """Update an upload job."""
        if not fields:
            return
        bad = set(fields) - _UPLOAD_JOB_COLUMNS
        if bad:
            raise ValueError(f"Unknown upload_job fields: {bad}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [job_id]
        self.conn.execute(f"UPDATE upload_jobs SET {sets} WHERE job_id = ?", vals)
        self.conn.commit()

    # --- Bundle operations (compiled workflows) ---

    def insert_bundle(
        self, bundle_id: str, bundle_path: str, *, capture_id: str | None = None
    ) -> None:
        """Record a compiled bundle directory."""
        now = _now()
        self.conn.execute(
            "INSERT INTO bundles (bundle_id, capture_id, bundle_path, created_at, compiled_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (bundle_id, capture_id, bundle_path, now, now),
        )
        self.conn.commit()

    def get_bundle(self, bundle_id: str) -> dict | None:
        """Get a single bundle by ID."""
        row = self.conn.execute(
            "SELECT * FROM bundles WHERE bundle_id = ?", (bundle_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_bundles(self, limit: int = 50) -> list[dict]:
        """List compiled bundles, newest first."""
        rows = self.conn.execute(
            "SELECT * FROM bundles ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def update_bundle(self, bundle_id: str, **fields: object) -> None:
        """Update fields on a bundle record."""
        _update(self.conn, "bundles", "bundle_id", bundle_id, fields, _BUNDLE_COLUMNS)

    # --- Run operations (local replay/run executions) ---

    def insert_run(self, run_id: str, run_path: str, *, bundle_id: str | None = None) -> None:
        """Record a local replay/run execution."""
        self.conn.execute(
            "INSERT INTO runs (run_id, bundle_id, run_path, created_at) VALUES (?, ?, ?, ?)",
            (run_id, bundle_id, run_path, _now()),
        )
        self.conn.commit()

    def get_run(self, run_id: str) -> dict | None:
        """Get a single run by ID."""
        row = self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self, limit: int = 50) -> list[dict]:
        """List runs, newest first."""
        rows = self.conn.execute(
            "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def update_run(self, run_id: str, **fields: object) -> None:
        """Update fields on a run record."""
        _update(self.conn, "runs", "run_id", run_id, fields, _RUN_COLUMNS)

    # --- Halt operations (local mirror of cloud needs-attention) ---

    def insert_halt(self, halt_id: str, run_id: str, **fields: object) -> None:
        """Record an open halt for a run."""
        bad = set(fields) - _HALT_COLUMNS
        if bad:
            raise ValueError(f"Unknown halt fields: {bad}")
        cols = ["halt_id", "run_id", "created_at", *fields.keys()]
        placeholders = ", ".join("?" for _ in cols)
        vals = [halt_id, run_id, _now(), *fields.values()]
        self.conn.execute(
            f"INSERT INTO halts ({', '.join(cols)}) VALUES ({placeholders})", vals
        )
        self.conn.commit()

    def get_halt(self, halt_id: str) -> dict | None:
        """Get a single halt by ID."""
        row = self.conn.execute("SELECT * FROM halts WHERE halt_id = ?", (halt_id,)).fetchone()
        return dict(row) if row else None

    def list_open_halts(self) -> list[dict]:
        """List halts still in 'open' status (the local needs-attention list)."""
        rows = self.conn.execute(
            "SELECT * FROM halts WHERE status = 'open' ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def count_open_halts(self) -> int:
        """Count open halts (feeds the local break badge)."""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM halts WHERE status = 'open'"
        ).fetchone()
        return int(row[0])

    def update_halt(self, halt_id: str, **fields: object) -> None:
        """Update fields on a halt record."""
        _update(self.conn, "halts", "halt_id", halt_id, fields, _HALT_COLUMNS)


def _update(
    conn: sqlite3.Connection,
    table: str,
    key_col: str,
    key: str,
    fields: dict,
    allowed: frozenset,
) -> None:
    """Shared UPDATE helper with column whitelisting."""
    if not fields:
        return
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"Unknown {table} fields: {bad}")
    sets = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [key]
    conn.execute(f"UPDATE {table} SET {sets} WHERE {key_col} = ?", vals)
    conn.commit()

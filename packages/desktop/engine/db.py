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
    "error", "completed_at",
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
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_captures_review ON captures(review_status);
CREATE INDEX IF NOT EXISTS idx_captures_tier ON captures(tier);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON upload_jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_capture ON upload_jobs(capture_id);
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

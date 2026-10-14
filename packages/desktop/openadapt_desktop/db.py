"""SQLite index database for capture metadata and upload queue.

Provides a thin wrapper around sqlite3 for all persistent state:
captures table (metadata, review status, tier) and upload_jobs table
(persistent queue that survives restarts).

Uses WAL mode for concurrent read access from monitoring threads.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from openadapt_desktop.item_vault import (
    DIGEST_PREFIX,
    SEALED_COLUMNS,
    ItemVault,
    ItemVaultError,
    is_sealed,
    open_vault,
)
from openadapt_desktop.private_fs import ensure_private_dir, secure_sqlite_files

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

# One input row a task ran on, and the person decision that followed a stop.
# Input values stay in this local database inside the customer boundary.
_WORK_ITEM_COLUMNS = frozenset({
    "run_id", "execution_outcome", "transaction_outcome", "halt_kind",
    "halt_step", "halt_reason", "expected", "observed", "status",
    "duration_s", "finished_at", "resolved_at", "approval_json",
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

CREATE TABLE IF NOT EXISTS first_workflow_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    stage TEXT NOT NULL,
    capture_id TEXT,
    workflow_id TEXT,
    target_json TEXT,
    task TEXT DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS work_items (
    item_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    label TEXT DEFAULT '',
    inputs_json TEXT NOT NULL DEFAULT '{}',
    inputs_digest TEXT NOT NULL,
    run_id TEXT,
    execution_outcome TEXT,
    transaction_outcome TEXT,
    halt_kind TEXT,
    halt_step TEXT DEFAULT '',
    halt_reason TEXT DEFAULT '',
    expected TEXT DEFAULT '',
    observed TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    duration_s REAL,
    retry_of TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    resolved_at TEXT,
    -- Flow's PHI-free approval-mode block for the item (closed codes only).
    approval_json TEXT
);

-- The keychain key that seals work-item values (openadapt_desktop/item_vault.py). Only
-- the key's id and a check value are here; the key is in the OS keychain.
CREATE TABLE IF NOT EXISTS item_vault (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    key_id TEXT NOT NULL,
    key_check TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Where a task was recorded, so Check it and Run it start from the same app.
-- Local only: a target can name an internal host or window.
CREATE TABLE IF NOT EXISTS task_targets (
    capture_id TEXT PRIMARY KEY,
    target_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_captures_review ON captures(review_status);
CREATE INDEX IF NOT EXISTS idx_work_items_workflow ON work_items(workflow_id, status);
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
        self._vault: ItemVault | None = None

    def initialize(self) -> None:
        """Open or create the database, enable WAL mode, create tables."""
        # The index can name patients (capture titles, work-item labels), so
        # the database and its WAL files are owner-only. SQLite creates the
        # WAL and shared-memory files with the database file's own mode.
        ensure_private_dir(self._db_path.parent)
        if not self._db_path.exists():
            self._db_path.touch(mode=0o600)
        secure_sqlite_files(self._db_path)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        # Overwrite deleted and replaced content, so a value replaced by its
        # ciphertext doesn't linger in a free page of the file.
        self._conn.execute("PRAGMA secure_delete=ON")
        secure_sqlite_files(self._db_path)
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._seal_legacy_work_items()

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
        work_item_columns = {
            r["name"]
            for r in self._conn.execute("PRAGMA table_info(work_items)").fetchall()
        }
        if "approval_json" not in work_item_columns:
            self._conn.execute("ALTER TABLE work_items ADD COLUMN approval_json TEXT")
        self._conn.commit()

    # --- Work-item values at rest (openadapt_desktop/item_vault.py) ---

    def _seal_legacy_work_items(self) -> None:
        """Seal work-item values that an earlier version stored in plain text.

        A closed item's input values are dropped, because nothing reads them
        again. The rest are sealed with the keychain key, and each digest is
        recomputed under that key. Without a working keychain the values of
        open items stay as they are until the next start, because a retry
        still needs them; their digests keep the old form, which the
        duplicate check still recognizes.
        """

        from openadapt_desktop.work_items import CLOSED_STATUSES

        plain = " OR ".join(
            f"({column} IS NOT NULL AND {column} != '' AND {column} NOT LIKE 'enc:v1:%'"
            + (" AND inputs_json != '{}')" if column == "inputs_json" else ")")
            for column in SEALED_COLUMNS
        )
        rows = [
            dict(row)
            for row in self.conn.execute(f"SELECT * FROM work_items WHERE {plain}").fetchall()
        ]
        if not rows:
            return
        vault = self._vault_or_none()
        for row in rows:
            fields: dict[str, object] = {}
            closed = row.get("status") in CLOSED_STATUSES
            digest = str(row.get("inputs_digest") or "")
            if vault is not None and not digest.startswith(DIGEST_PREFIX):
                try:
                    values = json.loads(row.get("inputs_json") or "")
                except ValueError:
                    values = None
                if isinstance(values, dict) and not is_sealed(row.get("inputs_json")):
                    fields["inputs_digest"] = vault.digest(str(row["workflow_id"]), values)
            if closed:
                fields["inputs_json"] = "{}"
            if vault is not None:
                for column in SEALED_COLUMNS:
                    value = fields.get(column, row.get(column))
                    if column == "inputs_json" and value == "{}":
                        continue
                    if isinstance(value, str) and value and not is_sealed(value):
                        fields[column] = vault.seal(
                            value, item_id=str(row["item_id"]), column=column
                        )
            if fields:
                sets = ", ".join(f"{column} = ?" for column in fields)
                self.conn.execute(
                    f"UPDATE work_items SET {sets} WHERE item_id = ?",
                    [*fields.values(), row["item_id"]],
                )
        self.conn.commit()
        # Rewrite the file so no page still holds the old plain text. Best
        # effort: another process holding the database open makes it wait
        # until a later start; the values themselves are already sealed.
        try:
            self.conn.execute("VACUUM")
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass
        secure_sqlite_files(self._db_path)

    def _item_vault(self) -> ItemVault:
        """This database's vault. Creates the key only for a database without one."""

        if self._vault is not None:
            return self._vault
        conn = self.conn
        # Two processes on one database (the sidecar and the CLI) must not
        # each create a key: take the write lock before looking.
        own_transaction = not conn.in_transaction
        if own_transaction:
            conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT key_id, key_check FROM item_vault WHERE singleton = 1"
            ).fetchone()
            vault, key_id, key_check = open_vault(
                row["key_id"] if row else None,
                row["key_check"] if row else None,
            )
            if row is None:
                conn.execute(
                    "INSERT INTO item_vault (singleton, key_id, key_check, created_at)"
                    " VALUES (1, ?, ?, ?)",
                    (key_id, key_check, _now()),
                )
            if own_transaction:
                conn.commit()
        except BaseException:
            if own_transaction:
                conn.rollback()
            raise
        self._vault = vault
        return vault

    def work_item_digest(self, workflow_id: str, values: dict) -> str:
        """Keyed digest of one item's values, for the duplicate check.

        Raises:
            ItemVaultError: The keychain key isn't available. Nothing may run.
        """

        return self._item_vault().digest(workflow_id, values)

    def _seal_fields(self, item_id: str, fields: dict[str, object]) -> dict[str, object]:
        sealed = dict(fields)
        for column in SEALED_COLUMNS:
            value = sealed.get(column)
            if isinstance(value, str) and value and not is_sealed(value):
                sealed[column] = self._item_vault().seal(value, item_id=item_id, column=column)
        return sealed

    def _vault_or_none(self) -> ItemVault | None:
        try:
            return self._item_vault()
        except ItemVaultError:
            return None

    def _open_work_item(self, row: sqlite3.Row, vault: object = ...) -> dict:
        item = dict(row)
        columns = [column for column in SEALED_COLUMNS if is_sealed(item.get(column))]
        if not columns:
            return item
        if vault is ...:
            vault = self._vault_or_none()
        for column in columns:
            try:
                if not isinstance(vault, ItemVault):
                    raise ItemVaultError("The key for this item isn't available.")
                item[column] = vault.open(
                    str(item[column]), item_id=str(item["item_id"]), column=column
                )
            except ItemVaultError:
                # Fail closed: an unreadable value is never shown or reused.
                item[column] = None if column == "inputs_json" else ""
                item["sealed_unreadable"] = True
        return item

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
        self,
        job_id: str,
        capture_id: str,
        backend_name: str,
        *,
        archive_path: str | None = None,
    ) -> None:
        """Create a new upload job in 'pending' status."""
        now = _now()
        self.conn.execute(
            "INSERT INTO upload_jobs "
            "(job_id, capture_id, backend_name, archive_path, created_at, completed_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, capture_id, backend_name, archive_path, now, None),
        )
        self.conn.commit()

    def recover_interrupted_upload_jobs(self) -> int:
        """Return crash-interrupted jobs to the pending queue."""

        cursor = self.conn.execute(
            "UPDATE upload_jobs SET status = 'pending', "
            "error = 'Desktop stopped during the prior upload attempt' "
            "WHERE status = 'in_progress'"
        )
        self.conn.commit()
        return int(cursor.rowcount)

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

    # --- Durable first-workflow journey ---

    def get_first_workflow_state(self) -> dict | None:
        """Return the one local first-workflow journey, if it is active."""

        row = self.conn.execute(
            "SELECT * FROM first_workflow_state WHERE singleton = 1"
        ).fetchone()
        if row is None:
            return None
        state = dict(row)
        raw_target = state.pop("target_json", None)
        try:
            target = json.loads(raw_target) if raw_target else None
        except (TypeError, json.JSONDecodeError):
            target = None
        state["target"] = target if isinstance(target, dict) else None
        state.pop("singleton", None)
        return state

    def set_first_workflow_state(
        self,
        *,
        stage: str,
        capture_id: str | None = None,
        workflow_id: str | None = None,
        target: dict | None = None,
        task: str = "",
    ) -> None:
        """Store one exact first-workflow stage atomically."""

        target_json = (
            json.dumps(target, sort_keys=True, separators=(",", ":"))
            if target is not None
            else None
        )
        self.conn.execute(
            "INSERT INTO first_workflow_state "
            "(singleton, stage, capture_id, workflow_id, target_json, task, updated_at) "
            "VALUES (1, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(singleton) DO UPDATE SET "
            "stage = excluded.stage, capture_id = excluded.capture_id, "
            "workflow_id = excluded.workflow_id, target_json = excluded.target_json, "
            "task = excluded.task, updated_at = excluded.updated_at",
            (stage, capture_id, workflow_id, target_json, task, _now()),
        )
        self.conn.commit()

    # --- Where each task was recorded ---

    def set_task_target(self, capture_id: str, target: dict) -> None:
        """Remember the application target one recording used."""

        self.conn.execute(
            "INSERT INTO task_targets (capture_id, target_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(capture_id) DO UPDATE SET "
            "target_json = excluded.target_json, updated_at = excluded.updated_at",
            (
                capture_id,
                json.dumps(target, sort_keys=True, separators=(",", ":")),
                _now(),
            ),
        )
        self.conn.commit()

    def get_task_target(self, capture_id: str) -> dict | None:
        """The application target a recording used, if Desktop knows it."""

        row = self.conn.execute(
            "SELECT target_json FROM task_targets WHERE capture_id = ?", (capture_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            target = json.loads(row["target_json"])
        except (TypeError, json.JSONDecodeError):
            return None
        return target if isinstance(target, dict) else None

    # --- Run operations (local replay/run executions) ---

    def insert_run(
        self,
        run_id: str,
        run_path: str,
        *,
        bundle_id: str | None = None,
        status: str = "pending",
    ) -> None:
        """Record a local execution and its known outcome atomically."""
        try:
            self.conn.execute(
                "INSERT INTO runs "
                "(run_id, bundle_id, run_path, status, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (run_id, bundle_id, run_path, status, _now()),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

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

    # --- Work items (one input row of a task run) ---

    def insert_work_item(
        self,
        item_id: str,
        *,
        batch_id: str,
        workflow_id: str,
        position: int,
        label: str,
        inputs_json: str,
        inputs_digest: str,
        retry_of: str | None = None,
    ) -> None:
        """Record one queued item before its run starts.

        The label and input values are sealed with the keychain key.

        Raises:
            ItemVaultError: The keychain key isn't available; nothing is stored.
        """
        sealed = self._seal_fields(item_id, {"label": label, "inputs_json": inputs_json})
        label = str(sealed["label"])
        inputs_json = str(sealed["inputs_json"])
        self.conn.execute(
            "INSERT INTO work_items (item_id, batch_id, workflow_id, position, label,"
            " inputs_json, inputs_digest, status, retry_of, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
            (
                item_id,
                batch_id,
                workflow_id,
                position,
                label,
                inputs_json,
                inputs_digest,
                retry_of,
                _now(),
            ),
        )
        self.conn.commit()

    def get_work_item(self, item_id: str) -> dict | None:
        """Get one work item by ID."""
        row = self.conn.execute(
            "SELECT * FROM work_items WHERE item_id = ?", (item_id,)
        ).fetchone()
        return self._open_work_item(row) if row else None

    def list_work_items(
        self,
        *,
        workflow_id: str | None = None,
        statuses: tuple[str, ...] | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        """List work items, newest first, optionally by task and status."""
        query = "SELECT * FROM work_items WHERE 1=1"
        params: list[object] = []
        if workflow_id is not None:
            query += " AND workflow_id = ?"
            params.append(workflow_id)
        if statuses:
            query += f" AND status IN ({', '.join('?' for _ in statuses)})"
            params.extend(statuses)
        query += " ORDER BY created_at DESC, position DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        sealed = any(is_sealed(row[column]) for row in rows for column in SEALED_COLUMNS)
        # Open the keychain once per listing, not once per row.
        vault = self._vault_or_none() if sealed else None
        return [self._open_work_item(row, vault) for row in rows]

    def update_work_item(self, item_id: str, **fields: object) -> None:
        """Update fields on one work item. Record text is sealed first."""
        _update(
            self.conn,
            "work_items",
            "item_id",
            item_id,
            self._seal_fields(item_id, fields),
            _WORK_ITEM_COLUMNS,
        )

    def clear_work_item_inputs(self, item_id: str) -> None:
        """Drop a closed item's input values; its digest still marks it done."""
        self.conn.execute(
            "UPDATE work_items SET inputs_json = '{}' WHERE item_id = ?", (item_id,)
        )
        self.conn.commit()


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

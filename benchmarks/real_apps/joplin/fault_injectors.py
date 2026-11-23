"""Planted-fault injectors for Joplin local SQLite silent-fail verification."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SUPPORTED_FAULTS = (
    "dropped_write",
    "truncated_value",
    "wrong_target",
    "duplicate_record",
    "stale_sync_conflict",
)


def apply_fault(
    db_path: Path,
    fault_class: str,
    *,
    note_title: str = "Q4 Vendor Checklist",
) -> Path:
    """Mutate Joplin's local ``database.sqlite`` to inject a deterministic silent failure."""
    with sqlite3.connect(db_path) as conn:
        if fault_class == "dropped_write":
            conn.execute("DELETE FROM notes WHERE title = ?", (note_title,))
        elif fault_class == "truncated_value":
            row = conn.execute(
                "SELECT body FROM notes WHERE title = ?", (note_title,)
            ).fetchone()
            if row:
                body = str(row[0])
                conn.execute(
                    "UPDATE notes SET body = ? WHERE title = ?",
                    (body[: max(1, len(body) // 2)], note_title),
                )
        elif fault_class == "wrong_target":
            conn.execute(
                "UPDATE notes SET parent_id = 'folder-ops-002' WHERE title = ?",
                (note_title,),
            )
        elif fault_class == "duplicate_record":
            row = conn.execute(
                "SELECT parent_id, title, body, updated_time FROM notes WHERE title = ?",
                (note_title,),
            ).fetchone()
            if row:
                conn.execute(
                    "INSERT INTO notes (id, parent_id, title, body, is_conflict, updated_time) "
                    "VALUES (?, ?, ?, ?, 0, ?)",
                    ("note-synth-dup", row[0], row[1], row[2], row[3]),
                )
        elif fault_class == "stale_sync_conflict":
            conn.execute(
                "UPDATE notes SET is_conflict = 1 WHERE title = ?",
                (note_title,),
            )
        else:
            raise ValueError(f"Unsupported Joplin fault class: {fault_class!r}")
        conn.commit()
    return db_path

"""Independent read-only SQLite read-back oracle for Joplin Electron desktop."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Mapping


def verify_write(
    db_path: str | Path,
    note_title: str,
    expected_fields: Mapping[str, Any],
) -> dict[str, Any]:
    """Query Joplin's local ``database.sqlite`` in read-only mode and fail closed."""
    p = Path(db_path)
    if not p.is_file():
        return {
            "verdict": "HALT",
            "channel": "local_sqlite_ro",
            "reason": "missing_database",
            "mismatches": [f"database not found: {p}"],
            "observed": None,
        }

    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT id, parent_id, title, body, is_conflict, updated_time "
                    "FROM notes WHERE title = ?",
                    (note_title,),
                ).fetchall()
            ]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return {
            "verdict": "HALT",
            "channel": "local_sqlite_ro",
            "reason": f"sqlite_error: {exc}",
            "mismatches": ["sqlite_read_error"],
            "observed": None,
        }

    if len(rows) == 0:
        return {
            "verdict": "HALT",
            "channel": "local_sqlite_ro",
            "reason": "missing_record",
            "mismatches": [f"no note found with title={note_title!r}"],
            "observed": [],
        }
    if len(rows) > 1:
        return {
            "verdict": "HALT",
            "channel": "local_sqlite_ro",
            "reason": "duplicate_record",
            "mismatches": [f"expected 1 note, observed {len(rows)}"],
            "observed": rows,
        }

    rec = rows[0]
    mismatches: list[str] = []
    if rec.get("is_conflict", 0) != 0:
        mismatches.append(
            f"is_conflict: expected 0, observed {rec.get('is_conflict')!r}"
        )
    for k, exp in expected_fields.items():
        actual = rec.get(k)
        if actual != exp:
            mismatches.append(f"{k}: expected {exp!r}, observed {actual!r}")

    return {
        "verdict": "VERIFIED" if not mismatches else "HALT",
        "channel": "local_sqlite_ro",
        "reason": "ok" if not mismatches else "field_or_conflict_mismatch",
        "mismatches": mismatches,
        "observed": rec,
    }

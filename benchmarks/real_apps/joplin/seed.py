"""Local SQLite seeder matching Joplin's desktop database schema (0 real user data)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS folders (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    updated_time INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY,
    parent_id TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    is_conflict INTEGER NOT NULL DEFAULT 0,
    updated_time INTEGER NOT NULL
);
"""


def seed_app(profile_dir: Path) -> Path:
    """Create a seeded Joplin ``database.sqlite`` under ``profile_dir``."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    db_path = profile_dir / "database.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT OR REPLACE INTO folders (id, title, updated_time) VALUES (?, ?, ?)",
            ("folder-ops-001", "Operations Runbooks", 1732233600000),
        )
        conn.execute(
            "INSERT OR REPLACE INTO folders (id, title, updated_time) VALUES (?, ?, ?)",
            ("folder-ops-002", "Archive", 1732233600000),
        )
        conn.execute(
            "INSERT OR REPLACE INTO notes (id, parent_id, title, body, is_conflict, updated_time) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "note-synth-001",
                "folder-ops-001",
                "Q4 Vendor Checklist",
                "Verify invoice totals and read back SQLite state.",
                0,
                1732233600000,
            ),
        )
        conn.commit()
    return db_path

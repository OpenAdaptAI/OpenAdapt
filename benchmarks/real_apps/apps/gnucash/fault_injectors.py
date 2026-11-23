"""Read-only lock and unsaved-buffer fault injectors for GnuCash."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from benchmarks.real_apps.apps.gnucash.seed import LedgerEntryFixture


def inject_readonly_lock(db_path: Path, *, hostname: str = "other-workstation", pid: int = 99999) -> None:
    """Insert a stale ``gnclock`` row simulating another GnuCash session holding the lock."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO gnclock (Hostname, PID) VALUES (?, ?)",
            (hostname, pid),
        )
        conn.commit()
    finally:
        conn.close()


def has_active_lock(db_path: Path) -> bool:
    """Return True if ``gnclock`` contains an active lock entry."""
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        row = conn.execute("SELECT COUNT(*) FROM gnclock").fetchone()
        return bool(row and int(row[0]) > 0)
    finally:
        conn.close()


def simulate_unsaved_buffer_write(
    db_path: Path, entry: LedgerEntryFixture
) -> dict[str, object]:
    """Simulate a UI entry left in an unsaved buffer (or blocked by read-only lock).

    Returns a deceptive UI-level status while intentionally NOT committing to SQLite.
    """
    locked = has_active_lock(db_path)
    return {
        "ui_row_visible": True,
        "tx_guid": entry.tx_guid,
        "persisted": False,
        "reason": "readonly_gnclock" if locked else "unsaved_buffer",
    }

"""Tests for the index database."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from engine.db import IndexDB


@pytest.fixture
def db(tmp_path: Path) -> IndexDB:
    """Create a temporary index database."""
    d = IndexDB(tmp_path / "index.db")
    d.initialize()
    yield d
    d.close()


class TestIndexDB:
    """Tests for IndexDB operations."""

    def test_initialize_creates_tables(self, db: IndexDB) -> None:
        """Both tables should exist after initialization."""
        tables = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = {r["name"] for r in tables}
        assert "captures" in names
        assert "upload_jobs" in names

    def test_wal_mode_enabled(self, db: IndexDB) -> None:
        """Database should use WAL journal mode."""
        mode = db.conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"

    def test_insert_and_get_capture(self, db: IndexDB) -> None:
        """Round-trip insert and get."""
        db.insert_capture("abc123", "/tmp/cap", "2026-03-02T10:00:00Z")
        cap = db.get_capture("abc123")
        assert cap is not None
        assert cap["capture_id"] == "abc123"
        assert cap["capture_path"] == "/tmp/cap"
        assert cap["review_status"] == "captured"

    def test_get_nonexistent_capture(self, db: IndexDB) -> None:
        """Getting a nonexistent capture returns None."""
        assert db.get_capture("nope") is None

    def test_update_capture(self, db: IndexDB) -> None:
        """Update specific fields."""
        db.insert_capture("abc123", "/tmp/cap", "2026-03-02T10:00:00Z")
        db.update_capture("abc123", stopped_at="2026-03-02T10:05:00Z", duration_secs=300.0)
        cap = db.get_capture("abc123")
        assert cap["stopped_at"] == "2026-03-02T10:05:00Z"
        assert cap["duration_secs"] == 300.0

    def test_update_capture_rejects_unknown_field(self, db: IndexDB) -> None:
        """Unknown fields should raise ValueError."""
        db.insert_capture("abc123", "/tmp/cap", "2026-03-02T10:00:00Z")
        with pytest.raises(ValueError, match="Unknown"):
            db.update_capture("abc123", nonexistent_field="value")

    def test_list_captures_ordered(self, db: IndexDB) -> None:
        """Captures should be returned newest first."""
        db.insert_capture("a", "/tmp/a", "2026-03-01T10:00:00Z")
        db.insert_capture("b", "/tmp/b", "2026-03-02T10:00:00Z")
        db.insert_capture("c", "/tmp/c", "2026-03-03T10:00:00Z")
        caps = db.list_captures(limit=10)
        assert [c["capture_id"] for c in caps] == ["c", "b", "a"]

    def test_list_captures_filter_by_status(self, db: IndexDB) -> None:
        """Filter by review_status."""
        db.insert_capture("a", "/tmp/a", "2026-03-01T10:00:00Z")
        db.insert_capture("b", "/tmp/b", "2026-03-02T10:00:00Z")
        db.update_capture("b", review_status="scrubbed")
        caps = db.list_captures(review_status="scrubbed")
        assert len(caps) == 1
        assert caps[0]["capture_id"] == "b"

    def test_get_pending_reviews(self, db: IndexDB) -> None:
        """Only captured and scrubbed captures returned."""
        db.insert_capture("a", "/tmp/a", "2026-03-01T10:00:00Z")
        db.insert_capture("b", "/tmp/b", "2026-03-02T10:00:00Z")
        db.insert_capture("c", "/tmp/c", "2026-03-03T10:00:00Z")
        db.update_capture("b", review_status="scrubbed")
        db.update_capture("c", review_status="reviewed")
        pending = db.get_pending_reviews()
        ids = {c["capture_id"] for c in pending}
        assert ids == {"a", "b"}

    def test_insert_and_get_upload_job(self, db: IndexDB) -> None:
        """Round-trip for upload jobs."""
        db.insert_capture("cap1", "/tmp/cap1", "2026-03-01T10:00:00Z")
        db.insert_upload_job("job1", "cap1", "s3")
        jobs = db.get_pending_jobs()
        assert len(jobs) == 1
        assert jobs[0]["job_id"] == "job1"
        assert jobs[0]["backend_name"] == "s3"

    def test_concurrent_reads(self, db: IndexDB) -> None:
        """WAL mode allows concurrent reads."""
        db.insert_capture("a", "/tmp/a", "2026-03-01T10:00:00Z")
        results = []

        def read_cap():
            cap = db.get_capture("a")
            results.append(cap is not None)

        threads = [threading.Thread(target=read_cap) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert all(results)

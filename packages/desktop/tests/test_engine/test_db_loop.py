"""Tests for the bundles/runs/halts tables and durable-queue columns."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from engine.db import IndexDB


@pytest.fixture
def db(tmp_path: Path) -> IndexDB:
    d = IndexDB(tmp_path / "index.db")
    d.initialize()
    yield d
    d.close()


class TestBundles:
    def test_insert_and_get(self, db: IndexDB) -> None:
        db.insert_bundle("b1", "/tmp/b1", capture_id="cap1")
        bundle = db.get_bundle("b1")
        assert bundle["bundle_path"] == "/tmp/b1"
        assert bundle["status"] == "compiled"
        assert bundle["schema_version"] == 2

    def test_update_workflow_id(self, db: IndexDB) -> None:
        db.insert_bundle("b1", "/tmp/b1")
        db.update_bundle("b1", workflow_id="wf_9", steps=7)
        bundle = db.get_bundle("b1")
        assert bundle["workflow_id"] == "wf_9"
        assert bundle["steps"] == 7

    def test_update_rejects_unknown(self, db: IndexDB) -> None:
        db.insert_bundle("b1", "/tmp/b1")
        with pytest.raises(ValueError, match="Unknown"):
            db.update_bundle("b1", nope="x")


class TestRuns:
    def test_insert_and_list(self, db: IndexDB) -> None:
        db.insert_bundle("b1", "/tmp/b1")
        db.insert_run("r1", "/tmp/runs/r1", bundle_id="b1")
        db.update_run("r1", status="halt", steps=3)
        run = db.get_run("r1")
        assert run["status"] == "halt"
        assert run["bundle_id"] == "b1"
        assert len(db.list_runs()) == 1


class TestHalts:
    def test_insert_and_count_open(self, db: IndexDB) -> None:
        db.insert_run("r1", "/tmp/runs/r1")
        db.insert_halt("h1", "r1", reason="drift", step_intent="click", workflow_id="wf_1")
        assert db.count_open_halts() == 1
        assert db.list_open_halts()[0]["reason"] == "drift"

    def test_resolve_halt(self, db: IndexDB) -> None:
        db.insert_run("r1", "/tmp/runs/r1")
        db.insert_halt("h1", "r1", reason="drift")
        db.update_halt("h1", status="resolved")
        assert db.count_open_halts() == 0

    def test_insert_rejects_unknown_field(self, db: IndexDB) -> None:
        db.insert_run("r1", "/tmp/runs/r1")
        with pytest.raises(ValueError, match="Unknown"):
            db.insert_halt("h1", "r1", bogus="x")


class TestDurableQueue:
    def test_due_jobs_excludes_future_retry(self, db: IndexDB) -> None:
        db.insert_capture("cap1", "/tmp/cap1", "2026-03-01T10:00:00Z")
        db.insert_upload_job("j1", "cap1", "hosted_ingest")
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        db.update_upload_job("j1", next_retry_at=future)
        assert db.get_due_jobs() == []

    def test_due_jobs_includes_elapsed_retry(self, db: IndexDB) -> None:
        db.insert_capture("cap1", "/tmp/cap1", "2026-03-01T10:00:00Z")
        db.insert_upload_job("j1", "cap1", "hosted_ingest")
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        db.update_upload_job("j1", next_retry_at=past)
        due = db.get_due_jobs()
        assert len(due) == 1

    def test_attempts_column_persists(self, db: IndexDB) -> None:
        db.insert_capture("cap1", "/tmp/cap1", "2026-03-01T10:00:00Z")
        db.insert_upload_job("j1", "cap1", "hosted_ingest")
        db.update_upload_job("j1", attempts=3)
        job = db.get_jobs_for_capture("cap1")[0]
        assert job["attempts"] == 3

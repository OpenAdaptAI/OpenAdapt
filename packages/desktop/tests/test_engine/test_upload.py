"""Tests for the upload manager."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from engine.audit import AuditLogger
from engine.backends.protocol import UploadResult
from engine.config import EngineConfig
from engine.db import IndexDB
from engine.review import EgressBlockedError
from engine.upload_manager import UploadManager


@pytest.fixture
def db(tmp_path: Path) -> IndexDB:
    d = IndexDB(tmp_path / "index.db")
    d.initialize()
    yield d
    d.close()


@pytest.fixture
def audit(tmp_path: Path) -> AuditLogger:
    return AuditLogger(tmp_path / "audit.jsonl", enabled=True)


@pytest.fixture
def mock_backend() -> MagicMock:
    backend = MagicMock()
    backend.name = "test_backend"
    backend.upload.return_value = UploadResult(
        success=True, remote_url="test://uploaded", bytes_sent=100
    )
    return backend


class TestUploadManager:
    """Tests for UploadManager operations."""

    def test_enqueue_checks_egress(
        self, db: IndexDB, audit: AuditLogger, mock_backend: MagicMock,
    ) -> None:
        """Enqueue should block unreviewed captures."""
        db.insert_capture("cap1", "/tmp/cap1", "2026-03-01T10:00:00Z")
        # Status is 'captured' -- not cleared for egress
        manager = UploadManager(EngineConfig(), [mock_backend], db, audit)
        with pytest.raises(EgressBlockedError):
            manager.enqueue("cap1", "test_backend")

    def test_enqueue_valid_creates_job(
        self, db: IndexDB, audit: AuditLogger, mock_backend: MagicMock,
    ) -> None:
        """Enqueue should create a job for reviewed captures."""
        db.insert_capture("cap1", "/tmp/cap1", "2026-03-01T10:00:00Z")
        db.update_capture("cap1", review_status="reviewed")
        manager = UploadManager(EngineConfig(), [mock_backend], db, audit)
        job_id = manager.enqueue("cap1", "test_backend")
        assert job_id is not None
        jobs = db.get_pending_jobs()
        assert len(jobs) == 1

    def test_enqueue_invalid_backend_raises(
        self, db: IndexDB, audit: AuditLogger, mock_backend: MagicMock,
    ) -> None:
        """Enqueue with unknown backend should raise ValueError."""
        db.insert_capture("cap1", "/tmp/cap1", "2026-03-01T10:00:00Z")
        db.update_capture("cap1", review_status="reviewed")
        manager = UploadManager(EngineConfig(), [mock_backend], db, audit)
        with pytest.raises(ValueError, match="Backend not available"):
            manager.enqueue("cap1", "nonexistent")

    def test_get_queue_status(
        self, db: IndexDB, audit: AuditLogger, mock_backend: MagicMock,
    ) -> None:
        """Queue status should return pending jobs."""
        db.insert_capture("cap1", "/tmp/cap1", "2026-03-01T10:00:00Z")
        db.update_capture("cap1", review_status="reviewed")
        manager = UploadManager(EngineConfig(), [mock_backend], db, audit)
        manager.enqueue("cap1", "test_backend")
        status = manager.get_queue_status()
        assert len(status) == 1

    def test_process_queue_calls_backend(
        self, db: IndexDB, audit: AuditLogger, mock_backend: MagicMock, tmp_path: Path,
    ) -> None:
        """Processing queue should call the backend upload."""
        cap_dir = tmp_path / "captures" / "test_cap"
        cap_dir.mkdir(parents=True)
        (cap_dir / "data.bin").write_bytes(b"test")

        db.insert_capture("cap1", str(cap_dir), "2026-03-01T10:00:00Z")
        db.update_capture("cap1", review_status="reviewed")
        manager = UploadManager(EngineConfig(), [mock_backend], db, audit)
        manager.enqueue("cap1", "test_backend")
        results = manager.process_queue()
        assert len(results) == 1
        assert results[0]["success"] is True
        mock_backend.upload.assert_called_once()

    def test_upload_logs_audit(
        self, db: IndexDB, audit: AuditLogger, mock_backend: MagicMock, tmp_path: Path,
    ) -> None:
        """Upload should log to audit trail."""
        archive = tmp_path / "test.tar.gz"
        archive.write_bytes(b"fake archive")
        manager = UploadManager(EngineConfig(), [mock_backend], db, audit)
        result = manager.upload(archive, "test_backend", {"capture_id": "cap1"})
        assert result.success
        # Verify audit log was written
        assert audit.log_path.exists()

    def test_get_active_backends(
        self, db: IndexDB, audit: AuditLogger, mock_backend: MagicMock,
    ) -> None:
        """Active backends should return configured backend names."""
        manager = UploadManager(EngineConfig(), [mock_backend], db, audit)
        assert "test_backend" in manager.get_active_backends()

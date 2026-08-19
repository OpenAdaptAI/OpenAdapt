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


def _write_review_files(path: Path) -> None:
    from engine.review import derivative_tree_sha256

    (path / "scrub_manifest.json").write_text('{"scrub_level":"standard"}')
    (path / "review_status.json").write_text(
        '{"status":"reviewed","approved_tree_sha256":"'
        + derivative_tree_sha256(path)
        + '"}'
    )


def _config(tmp_path: Path) -> EngineConfig:
    return EngineConfig(data_dir=tmp_path / "openadapt-data")


class TestUploadManager:
    """Tests for UploadManager operations."""

    def test_enqueue_checks_egress(
        self,
        db: IndexDB,
        audit: AuditLogger,
        mock_backend: MagicMock,
    ) -> None:
        """Enqueue should block unreviewed captures."""
        db.insert_capture("cap1", "/tmp/cap1", "2026-03-01T10:00:00Z")
        # Status is 'captured' -- not cleared for egress
        manager = UploadManager(EngineConfig(), [mock_backend], db, audit)
        with pytest.raises(EgressBlockedError):
            manager.enqueue("cap1", "test_backend")

    def test_enqueue_valid_creates_job(
        self,
        db: IndexDB,
        audit: AuditLogger,
        mock_backend: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Enqueue should create a job for reviewed captures."""
        raw = tmp_path / "cap1"
        scrubbed = tmp_path / "cap1.scrubbed"
        raw.mkdir()
        scrubbed.mkdir()
        _write_review_files(scrubbed)
        db.insert_capture("cap1", str(raw), "2026-03-01T10:00:00Z")
        db.update_capture("cap1", review_status="reviewed", scrubbed_path=str(scrubbed))
        manager = UploadManager(_config(tmp_path), [mock_backend], db, audit)
        job_id = manager.enqueue("cap1", "test_backend")
        assert job_id is not None
        jobs = db.get_pending_jobs()
        assert len(jobs) == 1
        assert jobs[0]["completed_at"] is None

    def test_enqueue_invalid_backend_raises(
        self,
        db: IndexDB,
        audit: AuditLogger,
        mock_backend: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Enqueue with unknown backend should raise ValueError."""
        raw = tmp_path / "cap1"
        scrubbed = tmp_path / "cap1.scrubbed"
        raw.mkdir()
        scrubbed.mkdir()
        _write_review_files(scrubbed)
        db.insert_capture("cap1", str(raw), "2026-03-01T10:00:00Z")
        db.update_capture("cap1", review_status="reviewed", scrubbed_path=str(scrubbed))
        manager = UploadManager(_config(tmp_path), [mock_backend], db, audit)
        with pytest.raises(ValueError, match="Backend not available"):
            manager.enqueue("cap1", "nonexistent")

    def test_get_queue_status(
        self,
        db: IndexDB,
        audit: AuditLogger,
        mock_backend: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Queue status should return pending jobs."""
        raw = tmp_path / "cap1"
        scrubbed = tmp_path / "cap1.scrubbed"
        raw.mkdir()
        scrubbed.mkdir()
        _write_review_files(scrubbed)
        db.insert_capture("cap1", str(raw), "2026-03-01T10:00:00Z")
        db.update_capture("cap1", review_status="reviewed", scrubbed_path=str(scrubbed))
        manager = UploadManager(_config(tmp_path), [mock_backend], db, audit)
        manager.enqueue("cap1", "test_backend")
        status = manager.get_queue_status()
        assert len(status) == 1

    def test_process_queue_calls_backend(
        self,
        db: IndexDB,
        audit: AuditLogger,
        mock_backend: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Processing queue should call the backend upload."""
        cap_dir = tmp_path / "captures" / "test_cap"
        cap_dir.mkdir(parents=True)
        (cap_dir / "data.bin").write_bytes(b"raw-secret")
        scrubbed_dir = tmp_path / "captures" / "test_cap.scrubbed"
        scrubbed_dir.mkdir()
        (scrubbed_dir / "data.bin").write_bytes(b"sanitized")
        _write_review_files(scrubbed_dir)

        db.insert_capture("cap1", str(cap_dir), "2026-03-01T10:00:00Z")
        db.update_capture("cap1", review_status="reviewed", scrubbed_path=str(scrubbed_dir))
        uploaded: dict[str, bytes] = {}

        def inspect_archive(path: Path, _metadata: dict) -> UploadResult:
            import zipfile

            with zipfile.ZipFile(path) as archive:
                uploaded["data"] = archive.read("data.bin")
            return UploadResult(success=True, remote_url="test://uploaded", bytes_sent=100)

        mock_backend.upload.side_effect = inspect_archive
        manager = UploadManager(_config(tmp_path), [mock_backend], db, audit)
        manager.enqueue("cap1", "test_backend")
        results = manager.process_queue()
        assert len(results) == 1
        assert results[0]["success"] is True
        assert uploaded["data"] == b"sanitized"
        mock_backend.upload.assert_called_once()
        job = db.get_jobs_for_capture("cap1")[0]
        assert not Path(job["archive_path"]).exists()

    def test_queue_upload_logs_audit(
        self,
        db: IndexDB,
        audit: AuditLogger,
        mock_backend: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Upload should log to audit trail."""
        raw = tmp_path / "cap1"
        scrubbed = tmp_path / "cap1.scrubbed"
        raw.mkdir()
        scrubbed.mkdir()
        (scrubbed / "data.bin").write_bytes(b"sanitized")
        _write_review_files(scrubbed)
        db.insert_capture("cap1", str(raw), "2026-03-01T10:00:00Z")
        db.update_capture("cap1", review_status="reviewed", scrubbed_path=str(scrubbed))
        manager = UploadManager(_config(tmp_path), [mock_backend], db, audit)
        manager.enqueue("cap1", "test_backend")
        result = manager.process_queue()[0]
        assert result["success"] is True
        # Verify audit log was written
        assert audit.log_path.exists()

    def test_get_active_backends(
        self,
        db: IndexDB,
        audit: AuditLogger,
        mock_backend: MagicMock,
    ) -> None:
        """Active backends should return configured backend names."""
        manager = UploadManager(EngineConfig(), [mock_backend], db, audit)
        assert "test_backend" in manager.get_active_backends()

    def test_direct_hosted_backend_is_refused_even_when_injected(
        self,
        db: IndexDB,
        audit: AuditLogger,
        tmp_path: Path,
    ) -> None:
        raw = tmp_path / "cap1"
        scrubbed = tmp_path / "cap1.scrubbed"
        raw.mkdir()
        scrubbed.mkdir()
        _write_review_files(scrubbed)
        db.insert_capture("cap1", str(raw), "2026-03-01T10:00:00Z")
        db.update_capture("cap1", review_status="reviewed", scrubbed_path=str(scrubbed))
        backend = MagicMock()
        backend.name = "hosted_ingest"
        manager = UploadManager(_config(tmp_path), [backend], db, audit)

        with pytest.raises(ValueError, match="Direct hosted ingest is disabled"):
            manager.enqueue("cap1", "hosted_ingest")

        backend.upload.assert_not_called()

    def test_customer_storage_is_paused_without_network(
        self,
        db: IndexDB,
        audit: AuditLogger,
        tmp_path: Path,
    ) -> None:
        raw = tmp_path / "cap1"
        scrubbed = tmp_path / "cap1.scrubbed"
        raw.mkdir()
        scrubbed.mkdir()
        _write_review_files(scrubbed)
        db.insert_capture("cap1", str(raw), "2026-03-01T10:00:00Z")
        db.update_capture("cap1", review_status="reviewed", scrubbed_path=str(scrubbed))
        backend = MagicMock()
        backend.name = "s3"
        manager = UploadManager(_config(tmp_path), [backend], db, audit)

        with pytest.raises(ValueError, match="paused for this release"):
            manager.enqueue("cap1", "s3")

        backend.upload.assert_not_called()


class TestDurableRetry:
    """Durable/offline queue behavior (spec section 5)."""

    def _prepare(self, db: IndexDB, tmp_path: Path):
        cap_dir = tmp_path / "captures" / "cap"
        cap_dir.mkdir(parents=True)
        (cap_dir / "data.bin").write_bytes(b"x")
        scrubbed_dir = tmp_path / "captures" / "cap.scrubbed"
        scrubbed_dir.mkdir()
        (scrubbed_dir / "data.bin").write_bytes(b"sanitized")
        _write_review_files(scrubbed_dir)
        db.insert_capture("cap1", str(cap_dir), "2026-03-01T10:00:00Z")
        db.update_capture("cap1", review_status="reviewed", scrubbed_path=str(scrubbed_dir))

    def test_transient_failure_requeues_with_backoff(
        self,
        db: IndexDB,
        audit: AuditLogger,
        tmp_path: Path,
    ) -> None:
        self._prepare(db, tmp_path)
        backend = MagicMock()
        backend.name = "test_backend"
        backend.upload.return_value = UploadResult(success=False, error="network down")

        manager = UploadManager(_config(tmp_path), [backend], db, audit)
        manager.enqueue("cap1", "test_backend")
        manager.process_queue()

        # Job is offline-deferred, not permanently failed.
        assert manager.offline is True
        job = db.get_jobs_for_capture("cap1")[0]
        assert job["status"] == "pending"
        assert job["attempts"] == 1
        assert job["next_retry_at"] is not None

    def test_permanent_failure_after_max_attempts(
        self,
        db: IndexDB,
        audit: AuditLogger,
        tmp_path: Path,
    ) -> None:
        self._prepare(db, tmp_path)
        backend = MagicMock()
        backend.name = "test_backend"
        backend.upload.return_value = UploadResult(success=False, error="network down")

        manager = UploadManager(_config(tmp_path), [backend], db, audit, max_attempts=1)
        manager.enqueue("cap1", "test_backend")
        manager.process_queue()

        job = db.get_jobs_for_capture("cap1")[0]
        assert job["status"] == "failed"

    def test_existing_legacy_hosted_job_is_failed_without_network(
        self,
        db: IndexDB,
        audit: AuditLogger,
        tmp_path: Path,
    ) -> None:
        self._prepare(db, tmp_path)
        backend = MagicMock()
        backend.name = "hosted_ingest"
        db.insert_upload_job("legacy-job", "cap1", "hosted_ingest")
        manager = UploadManager(_config(tmp_path), [backend], db, audit)

        result = manager.process_queue()[0]

        assert result["success"] is False
        assert "Direct hosted ingest is disabled" in result["error"]
        assert db.get_jobs_for_capture("cap1")[0]["status"] == "failed"
        backend.upload.assert_not_called()

    def test_missing_path_is_permanent(
        self,
        db: IndexDB,
        audit: AuditLogger,
        mock_backend: MagicMock,
        tmp_path: Path,
    ) -> None:
        raw = tmp_path / "cap"
        scrubbed = tmp_path / "cap.scrubbed"
        raw.mkdir()
        scrubbed.mkdir()
        _write_review_files(scrubbed)
        db.insert_capture("cap1", str(raw), "2026-03-01T10:00:00Z")
        db.update_capture("cap1", review_status="reviewed", scrubbed_path=str(scrubbed))
        manager = UploadManager(_config(tmp_path), [mock_backend], db, audit)
        manager.enqueue("cap1", "test_backend")
        for path in scrubbed.iterdir():
            path.unlink()
        scrubbed.rmdir()
        manager.process_queue()
        job = db.get_jobs_for_capture("cap1")[0]
        assert job["status"] == "failed"
        mock_backend.upload.assert_not_called()

    def test_queue_revalidates_derivative_path_before_network_egress(
        self,
        db: IndexDB,
        audit: AuditLogger,
        mock_backend: MagicMock,
        tmp_path: Path,
    ) -> None:
        raw = tmp_path / "cap"
        scrubbed = tmp_path / "cap.scrubbed"
        raw.mkdir()
        scrubbed.mkdir()
        _write_review_files(scrubbed)
        db.insert_capture("cap1", str(raw), "2026-03-01T10:00:00Z")
        db.update_capture("cap1", review_status="reviewed", scrubbed_path=str(scrubbed))
        manager = UploadManager(_config(tmp_path), [mock_backend], db, audit)
        manager.enqueue("cap1", "test_backend")

        db.update_capture("cap1", scrubbed_path=str(raw))
        result = manager.process_queue()[0]

        assert result["success"] is False
        assert "raw data" in result["error"]
        mock_backend.upload.assert_not_called()

    def test_post_review_mutation_is_refused_and_does_not_strand_job(
        self,
        db: IndexDB,
        audit: AuditLogger,
        mock_backend: MagicMock,
        tmp_path: Path,
    ) -> None:
        raw = tmp_path / "cap"
        scrubbed = tmp_path / "cap.scrubbed"
        raw.mkdir()
        scrubbed.mkdir()
        artifact = scrubbed / "data.bin"
        artifact.write_bytes(b"reviewed")
        _write_review_files(scrubbed)
        db.insert_capture("cap1", str(raw), "2026-03-01T10:00:00Z")
        db.update_capture("cap1", review_status="reviewed", scrubbed_path=str(scrubbed))
        manager = UploadManager(_config(tmp_path), [mock_backend], db, audit)
        manager.enqueue("cap1", "test_backend")

        artifact.write_bytes(b"changed")
        result = manager.process_queue()[0]

        assert result["success"] is False
        assert "changed after" in result["error"]
        assert db.get_jobs_for_capture("cap1")[0]["status"] == "failed"
        mock_backend.upload.assert_not_called()

    def test_mutated_frozen_archive_is_refused(
        self,
        db: IndexDB,
        audit: AuditLogger,
        mock_backend: MagicMock,
        tmp_path: Path,
    ) -> None:
        self._prepare(db, tmp_path)
        manager = UploadManager(_config(tmp_path), [mock_backend], db, audit)
        manager.enqueue("cap1", "test_backend")
        job = db.get_jobs_for_capture("cap1")[0]
        Path(job["archive_path"]).write_bytes(b"tampered")

        result = manager.process_queue()[0]

        assert result["success"] is False
        assert "changed after enqueue" in result["error"]
        assert db.get_jobs_for_capture("cap1")[0]["status"] == "failed"
        mock_backend.upload.assert_not_called()

    def test_interrupted_job_returns_to_queue_after_restart(
        self,
        db: IndexDB,
        audit: AuditLogger,
        mock_backend: MagicMock,
        tmp_path: Path,
    ) -> None:
        self._prepare(db, tmp_path)
        first = UploadManager(_config(tmp_path), [mock_backend], db, audit)
        job_id = first.enqueue("cap1", "test_backend")
        db.update_upload_job(job_id, status="in_progress")

        restarted = UploadManager(_config(tmp_path), [mock_backend], db, audit)
        result = restarted.process_queue()[0]

        assert result["success"] is True
        assert db.get_jobs_for_capture("cap1")[0]["status"] == "completed"

    def test_freeze_failure_does_not_create_pending_job(
        self,
        db: IndexDB,
        audit: AuditLogger,
        mock_backend: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._prepare(db, tmp_path)
        manager = UploadManager(_config(tmp_path), [mock_backend], db, audit)
        monkeypatch.setattr(
            manager,
            "_freeze_artifact",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
        )

        with pytest.raises(OSError, match="disk full"):
            manager.enqueue("cap1", "test_backend")

        assert db.get_jobs_for_capture("cap1") == []

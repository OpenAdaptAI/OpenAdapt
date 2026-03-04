"""End-to-end pipeline test: record -> scrub -> review -> upload.

Tests the complete pipeline with mocked openadapt-capture Recorder
and a mock storage backend.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from engine.audit import AuditLogger
from engine.backends.protocol import UploadResult
from engine.config import EngineConfig
from engine.controller import RecordingController, RecordingState
from engine.db import IndexDB
from engine.review import (
    ReviewStatus,
    check_egress_allowed,
    get_pending_reviews,
    transition_status,
)
from engine.scrubber import Scrubber, ScrubLevel
from engine.storage_manager import StorageManager
from engine.upload_manager import UploadManager


@pytest.fixture
def pipeline(tmp_path: Path):
    """Set up full pipeline components."""
    data_dir = tmp_path / ".openadapt"
    data_dir.mkdir()
    (data_dir / "captures").mkdir()
    (data_dir / "archive").mkdir()
    (data_dir / "tombstones").mkdir()

    config = EngineConfig(
        data_dir=data_dir,
        storage_mode="air-gapped",
        max_storage_gb=1.0,
        log_level="WARNING",
    )

    db = IndexDB(data_dir / "index.db")
    db.initialize()

    audit = AuditLogger(data_dir / "audit.jsonl", enabled=True)

    storage = StorageManager(config)
    storage.initialize()
    storage._db = db

    # Mock backend
    mock_backend = MagicMock()
    mock_backend.name = "mock"
    mock_backend.upload.return_value = UploadResult(
        success=True, remote_url="mock://uploaded/test", bytes_sent=100
    )

    upload_manager = UploadManager(config, [mock_backend], db, audit)

    class NS:
        pass

    ns = NS()
    ns.config = config
    ns.db = db
    ns.audit = audit
    ns.storage = storage
    ns.upload_manager = upload_manager
    ns.mock_backend = mock_backend

    yield ns
    db.close()


class TestE2EPipeline:
    """Full pipeline end-to-end test."""

    def test_record_scrub_approve_upload(self, pipeline) -> None:
        """Full pipeline: record -> scrub -> approve -> upload."""
        # Step 1: Start and stop recording
        controller = RecordingController(
            captures_dir=pipeline.config.data_dir / "captures",
            storage_manager=pipeline.storage,
        )
        capture_id = controller.start(task_description="E2E test task")
        assert controller.state == RecordingState.RECORDING

        metadata = controller.stop()
        assert controller.state == RecordingState.IDLE
        assert metadata["id"] == capture_id

        # Verify capture is registered
        cap = pipeline.db.get_capture(capture_id)
        assert cap is not None
        assert cap["review_status"] == "captured"

        # Step 2: Verify it shows up in pending reviews
        pending = get_pending_reviews(pipeline.db)
        assert any(c["capture_id"] == capture_id for c in pending)

        # Step 3: Scrub the capture
        scrubber = Scrubber(level=ScrubLevel.BASIC)
        capture_path = Path(cap["capture_path"])
        scrubbed_path = scrubber.scrub_capture(capture_path)
        assert scrubbed_path.exists()
        assert (scrubbed_path / "scrub_manifest.json").exists()

        transition_status(
            capture_id, ReviewStatus.CAPTURED, ReviewStatus.SCRUBBED,
            db=pipeline.db, audit=pipeline.audit,
        )
        cap = pipeline.db.get_capture(capture_id)
        assert cap["review_status"] == "scrubbed"

        # Step 4: Approve the scrubbed capture
        transition_status(
            capture_id, ReviewStatus.SCRUBBED, ReviewStatus.REVIEWED,
            db=pipeline.db, audit=pipeline.audit,
        )

        # Step 5: Verify egress is now allowed
        assert check_egress_allowed(capture_id, pipeline.db) is True

        # Step 6: Enqueue and process upload
        job_id = pipeline.upload_manager.enqueue(capture_id, "mock")
        assert job_id is not None

        results = pipeline.upload_manager.process_queue()
        assert len(results) == 1
        assert results[0]["success"] is True
        assert results[0]["remote_url"] == "mock://uploaded/test"

        # Verify backend was called
        pipeline.mock_backend.upload.assert_called_once()

        # Verify audit log has entries
        assert pipeline.audit.log_path.exists()

    def test_dismiss_allows_egress(self, pipeline) -> None:
        """Dismissed captures should be cleared for egress."""
        controller = RecordingController(
            captures_dir=pipeline.config.data_dir / "captures",
            storage_manager=pipeline.storage,
        )
        capture_id = controller.start()
        controller.stop()

        # Dismiss (skip scrubbing)
        transition_status(
            capture_id, ReviewStatus.CAPTURED, ReviewStatus.DISMISSED,
            db=pipeline.db,
        )

        assert check_egress_allowed(capture_id, pipeline.db) is True

    def test_upload_blocked_before_review(self, pipeline) -> None:
        """Upload should be blocked for unreviewed captures."""
        controller = RecordingController(
            captures_dir=pipeline.config.data_dir / "captures",
            storage_manager=pipeline.storage,
        )
        capture_id = controller.start()
        controller.stop()

        from engine.review import EgressBlockedError

        with pytest.raises(EgressBlockedError):
            pipeline.upload_manager.enqueue(capture_id, "mock")

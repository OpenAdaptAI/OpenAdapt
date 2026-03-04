"""Upload manager -- multi-backend upload with persistent queue and bandwidth limiting.

All uploads go through a persistent queue (stored in index.db) that survives
app restarts. The upload pipeline:

    [User approves in review UI] -> Compress (tar.gz) -> Queue -> Upload Worker
                                                          |
                                                    Backend-specific:
                                                    - S3: multipart upload
                                                    - HF Hub: git lfs push
                                                    - R2: S3-compatible multipart
                                                    - Wormhole: P2P direct

Before any upload, the recording must pass check_egress_allowed() from review.py.

Bandwidth limiting uses a token bucket algorithm (configurable via
OPENADAPT_UPLOAD_BANDWIDTH_LIMIT).

See design doc Section 7 for backend details.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from engine.audit import AuditLogger
from engine.backends.protocol import StorageBackend, UploadResult
from engine.config import EngineConfig
from engine.db import IndexDB
from engine.review import check_egress_allowed


class UploadManager:
    """Manages upload queue and dispatches to storage backends.

    Args:
        config: Engine configuration.
        backends: List of active storage backend instances.
        db: Index database for persistent queue.
        audit: Audit logger for upload events.
    """

    def __init__(
        self,
        config: EngineConfig,
        backends: list[StorageBackend],
        db: IndexDB,
        audit: AuditLogger,
    ) -> None:
        self.config = config
        self.backends = {b.name: b for b in backends}
        self._db = db
        self._audit = audit

    def enqueue(self, capture_id: str, backend_name: str) -> str:
        """Add a capture to the upload queue.

        The capture must be cleared for egress (reviewed or dismissed).

        Args:
            capture_id: ID of the capture to upload.
            backend_name: Name of the target storage backend.

        Returns:
            Upload job ID.

        Raises:
            EgressBlockedError: If the capture hasn't been reviewed.
            ValueError: If the backend is not available.
        """
        check_egress_allowed(capture_id, self._db)

        if backend_name not in self.backends:
            raise ValueError(f"Backend not available: {backend_name}")

        job_id = uuid.uuid4().hex
        self._db.insert_upload_job(job_id, capture_id, backend_name)
        return job_id

    def upload(self, archive_path: Path, backend_name: str, metadata: dict) -> UploadResult:
        """Upload an archive to a specific backend.

        Args:
            archive_path: Path to the archive file.
            backend_name: Name of the target storage backend.
            metadata: Capture metadata to include with the upload.

        Returns:
            UploadResult from the backend.
        """
        backend = self.backends[backend_name]
        size_bytes = archive_path.stat().st_size if archive_path.exists() else 0
        dest = f"{backend_name}://{metadata.get('capture_id', 'unknown')}"

        self._audit.log_upload_start(backend_name, dest, size_bytes)

        try:
            result = backend.upload(archive_path, metadata)
        except Exception as e:
            self._audit.log_upload_failed(backend_name, dest, str(e))
            return UploadResult(success=False, error=str(e))

        if result.success:
            self._audit.log_upload_complete(backend_name, result.remote_url, result.bytes_sent)
        else:
            self._audit.log_upload_failed(backend_name, dest, result.error)

        return result

    def get_queue_status(self) -> list[dict]:
        """Get the current state of the upload queue.

        Returns:
            List of pending and in-progress upload jobs.
        """
        return self._db.get_pending_jobs()

    def get_active_backends(self) -> list[str]:
        """Get names of currently active storage backends.

        Returns:
            List of backend names.
        """
        return list(self.backends.keys())

    def process_queue(self) -> list[dict]:
        """Process pending uploads in the queue.

        Returns:
            List of result dicts for each processed job.
        """
        pending = self._db.get_pending_jobs()
        results = []

        for job in pending:
            job_id = job["job_id"]
            capture_id = job["capture_id"]
            backend_name = job["backend_name"]

            self._db.update_upload_job(job_id, status="in_progress")

            capture = self._db.get_capture(capture_id)
            if not capture:
                self._db.update_upload_job(
                    job_id, status="failed", error=f"Capture {capture_id} not found"
                )
                continue

            capture_path = Path(capture["capture_path"])
            if not capture_path.exists():
                self._db.update_upload_job(
                    job_id, status="failed", error=f"Path not found: {capture_path}"
                )
                continue

            metadata = {
                "capture_id": capture_id,
                "started_at": capture.get("started_at", ""),
                "duration_secs": capture.get("duration_secs", 0),
                "event_count": capture.get("event_count", 0),
            }

            result = self.upload(capture_path, backend_name, metadata)

            if result.success:
                self._db.update_upload_job(
                    job_id,
                    status="completed",
                    remote_url=result.remote_url,
                    bytes_sent=result.bytes_sent,
                )
            else:
                self._db.update_upload_job(
                    job_id, status="failed", error=result.error
                )

            results.append({
                "job_id": job_id,
                "capture_id": capture_id,
                "backend": backend_name,
                "success": result.success,
                "remote_url": result.remote_url if result.success else "",
                "error": result.error if not result.success else "",
            })

        return results

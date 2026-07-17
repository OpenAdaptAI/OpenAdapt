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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger

from engine.audit import AuditLogger
from engine.backends.protocol import StorageBackend, UploadResult
from engine.config import EngineConfig
from engine.db import IndexDB
from engine.review import check_egress_allowed

# Durable/offline retry policy (spec section 5): jobs survive restarts (they
# live in the DB), retry with exponential backoff, and flush when connectivity
# returns. A permanent failure (missing capture/path) is NOT retried.
DEFAULT_MAX_ATTEMPTS = 6
_BACKOFF_BASE_S = 30
_BACKOFF_CAP_S = 3600


def _backoff_seconds(attempts: int) -> int:
    """Exponential backoff (capped) for the given attempt count."""
    return min(_BACKOFF_CAP_S, _BACKOFF_BASE_S * (2 ** max(0, attempts - 1)))


class UploadManager:
    """Manages a durable upload queue and dispatches to storage backends.

    The queue is persisted in ``index.db`` (survives restarts). Transient
    failures (e.g. network blips) are retried with exponential backoff and the
    manager exposes an ``offline`` signal for the tray's OFFLINE/SYNCING states.

    Args:
        config: Engine configuration.
        backends: List of active storage backend instances.
        db: Index database for persistent queue.
        audit: Audit logger for upload events.
        max_attempts: Attempts before a transient failure becomes permanent.
    """

    def __init__(
        self,
        config: EngineConfig,
        backends: list[StorageBackend],
        db: IndexDB,
        audit: AuditLogger,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self.config = config
        self.backends = {b.name: b for b in backends}
        self._db = db
        self._audit = audit
        self.max_attempts = max_attempts
        # True after a transient (retriable) failure this cycle; feeds tray state.
        self.offline = False

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
        """Process due uploads in the durable queue, retrying transient failures.

        Jobs whose retry backoff has not yet elapsed are skipped. A missing
        capture/path is a permanent failure (no retry); a backend/network error
        is transient -- the job returns to ``pending`` with exponential backoff
        until ``max_attempts`` is exhausted, then becomes ``failed``.

        Returns:
            List of result dicts for each job attempted this cycle.
        """
        due = self._db.get_due_jobs()
        results = []
        self.offline = False

        for job in due:
            job_id = job["job_id"]
            capture_id = job["capture_id"]
            backend_name = job["backend_name"]

            self._db.update_upload_job(job_id, status="in_progress")

            capture = self._db.get_capture(capture_id)
            if not capture:
                self._db.update_upload_job(
                    job_id, status="failed", error=f"Capture {capture_id} not found"
                )
                results.append(self._result(job_id, capture_id, backend_name, False,
                                            "", f"Capture {capture_id} not found"))
                continue

            capture_path = Path(capture["capture_path"])
            if not capture_path.exists():
                self._db.update_upload_job(
                    job_id, status="failed", error=f"Path not found: {capture_path}"
                )
                results.append(self._result(job_id, capture_id, backend_name, False,
                                            "", f"Path not found: {capture_path}"))
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
                self._schedule_retry(job, result.error)

            results.append(self._result(
                job_id, capture_id, backend_name, result.success,
                result.remote_url if result.success else "",
                result.error if not result.success else "",
            ))

        return results

    def _schedule_retry(self, job: dict, error: str) -> None:
        """Requeue a transiently-failed job with backoff, or fail it permanently."""
        attempts = (job.get("attempts") or 0) + 1
        self.offline = True
        if attempts >= self.max_attempts:
            self._db.update_upload_job(
                job["job_id"], status="failed", attempts=attempts, error=error
            )
            logger.warning(
                "Upload job {jid} permanently failed after {n} attempts",
                jid=job["job_id"], n=attempts,
            )
            return
        next_retry = datetime.now(timezone.utc) + timedelta(
            seconds=_backoff_seconds(attempts)
        )
        self._db.update_upload_job(
            job["job_id"],
            status="pending",
            attempts=attempts,
            next_retry_at=next_retry.isoformat(),
            error=error,
        )
        logger.info(
            "Upload job {jid} deferred (attempt {n}); retry at {t}",
            jid=job["job_id"], n=attempts, t=next_retry.isoformat(),
        )

    @staticmethod
    def _result(
        job_id: str, capture_id: str, backend: str, success: bool,
        remote_url: str, error: str,
    ) -> dict:
        return {
            "job_id": job_id,
            "capture_id": capture_id,
            "backend": backend,
            "success": success,
            "remote_url": remote_url,
            "error": error,
        }

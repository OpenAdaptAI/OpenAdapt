"""Upload manager -- multi-backend upload with persistent queue and bandwidth limiting.

All uploads go through a persistent queue (stored in index.db) that survives
app restarts. The upload pipeline:

    [User approves in review UI] -> Compress (tar.zst) -> Queue -> Upload Worker
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

from pathlib import Path

from engine.backends.protocol import StorageBackend, UploadResult
from engine.config import EngineConfig
from engine.review import check_egress_allowed


class UploadManager:
    """Manages upload queue and dispatches to storage backends.

    Args:
        config: Engine configuration.
        backends: List of active storage backend instances.
    """

    def __init__(self, config: EngineConfig, backends: list[StorageBackend]) -> None:
        self.config = config
        self.backends = {b.name: b for b in backends}
        self._queue: list[dict] = []

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
        check_egress_allowed(capture_id)

        if backend_name not in self.backends:
            raise ValueError(f"Backend not available: {backend_name}")

        # TODO: Create upload job record in index.db
        # TODO: Add to in-memory queue
        # TODO: Return job ID
        raise NotImplementedError

    def upload(self, archive_path: Path, backend_name: str, metadata: dict) -> UploadResult:
        """Upload an archive to a specific backend.

        Args:
            archive_path: Path to the tar.zst archive.
            backend_name: Name of the target storage backend.
            metadata: Capture metadata to include with the upload.

        Returns:
            UploadResult from the backend.
        """
        backend = self.backends[backend_name]
        # TODO: Apply bandwidth limiting
        # TODO: Log to audit trail
        # TODO: Handle resume on failure
        return backend.upload(archive_path, metadata)

    def get_queue_status(self) -> list[dict]:
        """Get the current state of the upload queue.

        Returns:
            List of pending and in-progress upload jobs.
        """
        # TODO: Query index.db for pending uploads
        raise NotImplementedError

    def get_active_backends(self) -> list[str]:
        """Get names of currently active storage backends.

        Returns:
            List of backend names.
        """
        return list(self.backends.keys())

    def process_queue(self) -> None:
        """Process pending uploads in the queue.

        Called periodically or when a new upload is enqueued.
        Respects bandwidth limits and upload schedule.
        """
        # TODO: Pick next queued upload
        # TODO: Compress capture to tar.zst if not already
        # TODO: Upload via backend
        # TODO: Update job status in index.db
        # TODO: Send progress events via IPC
        raise NotImplementedError

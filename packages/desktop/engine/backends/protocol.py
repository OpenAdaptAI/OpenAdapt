"""StorageBackend protocol -- interface that all storage backends implement.

Every storage destination implements this protocol. The upload manager
dispatches uploads to backends through this uniform interface.

All backends share the same upload pipeline:
    [User approves in review UI] -> Compress (tar.zst) -> Queue -> Upload Worker
                                                          |
                                                    Backend-specific:
                                                    - S3: multipart upload
                                                    - HF Hub: git lfs push
                                                    - R2: S3-compatible multipart
                                                    - Wormhole: P2P direct

See design doc Section 7.3 for the full protocol specification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass
class UploadResult:
    """Result of an upload operation.

    Attributes:
        success: Whether the upload succeeded.
        remote_url: URL or URI of the uploaded resource (if applicable).
        bytes_sent: Total bytes sent.
        error: Error message if the upload failed.
        metadata: Additional backend-specific metadata.
    """

    success: bool
    remote_url: str = ""
    bytes_sent: int = 0
    error: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class UploadRecord:
    """Record of a past upload, for listing/auditing.

    Attributes:
        recording_id: The capture session ID that was uploaded.
        backend: Name of the storage backend used.
        remote_url: URL or URI of the uploaded resource.
        uploaded_at: ISO 8601 timestamp of the upload.
        size_bytes: Size of the uploaded archive.
    """

    recording_id: str
    backend: str
    remote_url: str
    uploaded_at: str
    size_bytes: int


@runtime_checkable
class StorageBackend(Protocol):
    """Protocol that every storage backend must implement.

    Backends provide upload, optional delete, optional list, credential
    verification, and cost estimation.

    Attributes:
        name: Human-readable backend name (e.g., "s3", "huggingface").
        supports_delete: Whether uploaded resources can be deleted.
        supports_list: Whether uploaded resources can be listed.
    """

    name: str
    supports_delete: bool
    supports_list: bool

    def upload(self, archive_path: Path, metadata: dict) -> UploadResult:
        """Upload a capture archive to the backend.

        Args:
            archive_path: Path to the tar.zst archive to upload.
            metadata: Capture metadata (id, duration, event count, etc.).

        Returns:
            UploadResult with success status and remote URL.
        """
        ...

    def delete(self, recording_id: str) -> bool:
        """Delete a previously uploaded recording.

        Args:
            recording_id: The capture session ID to delete.

        Returns:
            True if deletion succeeded.

        Raises:
            NotImplementedError: If the backend does not support deletion.
        """
        ...

    def list_uploads(self) -> list[UploadRecord]:
        """List all uploads made through this backend.

        Returns:
            List of UploadRecord objects.

        Raises:
            NotImplementedError: If the backend does not support listing.
        """
        ...

    def verify_credentials(self) -> bool:
        """Verify that the backend credentials are valid.

        Returns:
            True if credentials are valid and the backend is reachable.
        """
        ...

    def estimate_cost(self, size_bytes: int) -> float | None:
        """Estimate the cost of uploading/storing the given amount of data.

        Args:
            size_bytes: Size of the data in bytes.

        Returns:
            Estimated cost in USD, or None if cost estimation is not available.
        """
        ...

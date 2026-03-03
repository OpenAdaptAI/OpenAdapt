"""HuggingFace Hub storage backend -- uploads recordings as dataset shards.

Uploads capture archives to a HuggingFace dataset repository using the
huggingface_hub Python library. Recordings are stored as dataset shards
in a community dataset repo (e.g., OpenAdaptAI/desktop-recordings).

Features:
    - Versioned via Git LFS
    - Built-in dataset viewer lets community browse without downloading
    - Supports both public and private (paid HF tier) datasets

Requires: huggingface_hub (installed via community or full extras)

See design doc Section 7.7 for implementation details.
"""

from __future__ import annotations

from pathlib import Path

from engine.backends.protocol import UploadRecord, UploadResult


class HuggingFaceBackend:
    """HuggingFace Hub storage backend.

    Args:
        repo: HuggingFace dataset repository (e.g., "OpenAdaptAI/desktop-recordings").
        token: HuggingFace API token.
        private: Whether to create a private dataset (requires paid HF tier).
    """

    name: str = "huggingface"
    supports_delete: bool = True
    supports_list: bool = True

    def __init__(
        self,
        repo: str = "OpenAdaptAI/desktop-recordings",
        token: str = "",
        private: bool = False,
    ) -> None:
        self.repo = repo
        self.token = token
        self.private = private
        # TODO: Initialize huggingface_hub API client

    def upload(self, archive_path: Path, metadata: dict) -> UploadResult:
        """Upload a capture archive to HuggingFace Hub.

        Args:
            archive_path: Path to the tar.zst archive.
            metadata: Capture metadata.

        Returns:
            UploadResult with the HF Hub URL.
        """
        # TODO: Use huggingface_hub.upload_file() or upload_folder()
        # TODO: Include metadata as dataset card or JSON sidecar
        raise NotImplementedError

    def delete(self, recording_id: str) -> bool:
        """Delete a recording from HuggingFace Hub.

        Args:
            recording_id: The capture session ID.

        Returns:
            True if deletion succeeded.
        """
        # TODO: Use huggingface_hub API to delete file
        raise NotImplementedError

    def list_uploads(self) -> list[UploadRecord]:
        """List all uploads in the HuggingFace dataset repo.

        Returns:
            List of UploadRecord objects.
        """
        # TODO: Use huggingface_hub.list_repo_files()
        raise NotImplementedError

    def verify_credentials(self) -> bool:
        """Verify HuggingFace token by calling the whoami endpoint.

        Returns:
            True if the token is valid.
        """
        # TODO: huggingface_hub.whoami(token)
        raise NotImplementedError

    def estimate_cost(self, size_bytes: int) -> float | None:
        """Estimate cost for HuggingFace Hub storage.

        Public datasets on HF Hub are free (unlimited storage).
        Private datasets require a paid HF tier.

        Args:
            size_bytes: Size of the data in bytes.

        Returns:
            0.0 for public datasets, None for private (varies by tier).
        """
        if not self.private:
            return 0.0
        return None

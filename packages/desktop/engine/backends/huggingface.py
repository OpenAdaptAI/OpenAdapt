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

    def upload(self, archive_path: Path, metadata: dict) -> UploadResult:
        """Upload a capture archive to HuggingFace Hub.

        Args:
            archive_path: Path to the archive file.
            metadata: Capture metadata.

        Returns:
            UploadResult with the HF Hub URL.
        """
        try:
            from huggingface_hub import HfApi

            api = HfApi(token=self.token)
            capture_id = metadata.get("capture_id", "unknown")
            path_in_repo = f"captures/{capture_id}/{archive_path.name}"

            api.upload_file(
                path_or_fileobj=str(archive_path),
                path_in_repo=path_in_repo,
                repo_id=self.repo,
                repo_type="dataset",
            )

            url = f"https://huggingface.co/datasets/{self.repo}/blob/main/{path_in_repo}"
            return UploadResult(
                success=True,
                remote_url=url,
                bytes_sent=archive_path.stat().st_size,
            )
        except ImportError:
            return UploadResult(success=False, error="huggingface_hub not installed")
        except Exception as e:
            return UploadResult(success=False, error=str(e))

    def delete(self, recording_id: str) -> bool:
        """Delete a recording from HuggingFace Hub.

        Args:
            recording_id: The capture session ID.

        Returns:
            True if deletion succeeded.
        """
        try:
            from huggingface_hub import HfApi

            api = HfApi(token=self.token)
            api.delete_folder(
                path_in_repo=f"captures/{recording_id}",
                repo_id=self.repo,
                repo_type="dataset",
            )
            return True
        except Exception:
            return False

    def list_uploads(self) -> list[UploadRecord]:
        """List all uploads in the HuggingFace dataset repo.

        Returns:
            List of UploadRecord objects.
        """
        try:
            from huggingface_hub import HfApi

            api = HfApi(token=self.token)
            files = api.list_repo_files(repo_id=self.repo, repo_type="dataset")
            records = []
            for f in files:
                if f.startswith("captures/"):
                    parts = f.split("/")
                    recording_id = parts[1] if len(parts) > 1 else "unknown"
                    records.append(UploadRecord(
                        recording_id=recording_id,
                        backend="huggingface",
                        remote_url=f"https://huggingface.co/datasets/{self.repo}/blob/main/{f}",
                        uploaded_at="",
                        size_bytes=0,
                    ))
            return records
        except Exception:
            return []

    def verify_credentials(self) -> bool:
        """Verify HuggingFace token by calling the whoami endpoint.

        Returns:
            True if the token is valid.
        """
        try:
            from huggingface_hub import HfApi

            api = HfApi(token=self.token)
            api.whoami()
            return True
        except Exception:
            return False

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

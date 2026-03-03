"""S3-compatible storage backend -- supports AWS S3, Cloudflare R2, and MinIO.

This backend handles multipart uploads to any S3-compatible storage service.
The specific service is determined by the endpoint configuration:
    - AWS S3:  Default endpoint (no custom endpoint needed)
    - R2:      OPENADAPT_S3_ENDPOINT=https://<account>.r2.cloudflarestorage.com
    - MinIO:   OPENADAPT_S3_ENDPOINT=https://minio.example.com:9000

Upload strategy:
    - Multipart upload (min 5 MB per part, max 10,000 parts)
    - Each capture session uploaded as a tar.zst archive
    - Upload queue persisted to index.db to survive app restarts
    - Bandwidth limiter using token bucket algorithm

Requires: boto3 (installed via enterprise or full extras)

See design doc Section 7.7 for implementation details.
"""

from __future__ import annotations

from pathlib import Path

from engine.backends.protocol import StorageBackend, UploadRecord, UploadResult


class S3Backend:
    """S3-compatible storage backend.

    Args:
        bucket: S3 bucket name.
        region: AWS region (default us-east-1).
        access_key_id: AWS access key ID.
        secret_access_key: AWS secret access key.
        endpoint: Custom endpoint URL for R2/MinIO (empty for AWS S3).
    """

    name: str = "s3"
    supports_delete: bool = True
    supports_list: bool = True

    def __init__(
        self,
        bucket: str,
        region: str = "us-east-1",
        access_key_id: str = "",
        secret_access_key: str = "",
        endpoint: str = "",
    ) -> None:
        self.bucket = bucket
        self.region = region
        self.endpoint = endpoint
        # TODO: Initialize boto3 S3 client
        # TODO: Configure custom endpoint if provided (R2, MinIO)
        self._client = None

    def upload(self, archive_path: Path, metadata: dict) -> UploadResult:
        """Upload a capture archive to S3 using multipart upload.

        Args:
            archive_path: Path to the tar.zst archive.
            metadata: Capture metadata.

        Returns:
            UploadResult with the S3 object URL.
        """
        # TODO: Generate S3 key from capture ID and timestamp
        # TODO: Multipart upload with progress callback
        # TODO: Set object metadata tags
        raise NotImplementedError

    def delete(self, recording_id: str) -> bool:
        """Delete a recording from S3.

        Args:
            recording_id: The capture session ID.

        Returns:
            True if deletion succeeded.
        """
        # TODO: Delete object from S3
        raise NotImplementedError

    def list_uploads(self) -> list[UploadRecord]:
        """List all uploads in the S3 bucket.

        Returns:
            List of UploadRecord objects.
        """
        # TODO: List objects with openadapt/ prefix
        raise NotImplementedError

    def verify_credentials(self) -> bool:
        """Verify S3 credentials by performing a HEAD bucket request.

        Returns:
            True if credentials are valid and bucket is accessible.
        """
        # TODO: boto3 head_bucket call
        raise NotImplementedError

    def estimate_cost(self, size_bytes: int) -> float | None:
        """Estimate S3 storage cost.

        Uses standard S3 pricing ($0.023/GB/month for first 50 TB).
        R2 has different pricing ($0.015/GB/month, free egress).

        Args:
            size_bytes: Size of the data in bytes.

        Returns:
            Estimated monthly storage cost in USD.
        """
        gb = size_bytes / (1024**3)
        if self.endpoint and "r2.cloudflarestorage.com" in self.endpoint:
            return round(gb * 0.015, 4)
        return round(gb * 0.023, 4)


# Ensure the class satisfies the protocol at import time.
assert isinstance(S3Backend.__new__(S3Backend), StorageBackend)  # type: ignore[arg-type]

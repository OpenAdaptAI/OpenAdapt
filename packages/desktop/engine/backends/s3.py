"""S3-compatible storage backend -- supports AWS S3, Cloudflare R2, and MinIO.

This backend handles uploads to any S3-compatible storage service.
The specific service is determined by the endpoint configuration:
    - AWS S3:  Default endpoint (no custom endpoint needed)
    - R2:      OPENADAPT_S3_ENDPOINT=https://<account>.r2.cloudflarestorage.com
    - MinIO:   OPENADAPT_S3_ENDPOINT=https://minio.example.com:9000

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
        self._client = None

        try:
            import boto3

            kwargs: dict = {
                "service_name": "s3",
                "region_name": region,
                "aws_access_key_id": access_key_id or None,
                "aws_secret_access_key": secret_access_key or None,
            }
            if endpoint:
                kwargs["endpoint_url"] = endpoint
            self._client = boto3.client(**kwargs)
        except ImportError:
            pass

    def upload(self, archive_path: Path, metadata: dict) -> UploadResult:
        """Upload a capture archive to S3.

        Args:
            archive_path: Path to the archive file.
            metadata: Capture metadata.

        Returns:
            UploadResult with the S3 object URL.
        """
        if self._client is None:
            return UploadResult(success=False, error="boto3 not installed")

        capture_id = metadata.get("capture_id", "unknown")
        key = f"openadapt/{capture_id}/{archive_path.name}"

        try:
            self._client.upload_file(str(archive_path), self.bucket, key)
            url = f"s3://{self.bucket}/{key}"
            return UploadResult(
                success=True,
                remote_url=url,
                bytes_sent=archive_path.stat().st_size,
            )
        except Exception as e:
            return UploadResult(success=False, error=str(e))

    def delete(self, recording_id: str) -> bool:
        """Delete a recording from S3.

        Args:
            recording_id: The capture session ID.

        Returns:
            True if deletion succeeded.
        """
        if self._client is None:
            return False

        try:
            # List and delete all objects with this prefix
            prefix = f"openadapt/{recording_id}/"
            response = self._client.list_objects_v2(Bucket=self.bucket, Prefix=prefix)
            for obj in response.get("Contents", []):
                self._client.delete_object(Bucket=self.bucket, Key=obj["Key"])
            return True
        except Exception:
            return False

    def list_uploads(self) -> list[UploadRecord]:
        """List all uploads in the S3 bucket.

        Returns:
            List of UploadRecord objects.
        """
        if self._client is None:
            return []

        try:
            response = self._client.list_objects_v2(Bucket=self.bucket, Prefix="openadapt/")
            records = []
            for obj in response.get("Contents", []):
                parts = obj["Key"].split("/")
                recording_id = parts[1] if len(parts) > 1 else "unknown"
                records.append(UploadRecord(
                    recording_id=recording_id,
                    backend="s3",
                    remote_url=f"s3://{self.bucket}/{obj['Key']}",
                    uploaded_at=obj["LastModified"].isoformat() if obj.get("LastModified") else "",
                    size_bytes=obj.get("Size", 0),
                ))
            return records
        except Exception:
            return []

    def verify_credentials(self) -> bool:
        """Verify S3 credentials by performing a HEAD bucket request.

        Returns:
            True if credentials are valid and bucket is accessible.
        """
        if self._client is None:
            return False

        try:
            self._client.head_bucket(Bucket=self.bucket)
            return True
        except Exception:
            return False

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

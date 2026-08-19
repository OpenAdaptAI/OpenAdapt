"""Tests for storage backends."""

from __future__ import annotations

import pytest

from engine.backends.hosted_ingest import HostedIngestBackend
from engine.backends.protocol import StorageBackend
from engine.backends.s3 import S3Backend


class TestProtocolConformance:
    """Verify all backends conform to the StorageBackend protocol."""

    def test_s3_has_required_attributes(self) -> None:
        """S3Backend should have all required protocol attributes."""
        assert hasattr(S3Backend, "name")
        assert hasattr(S3Backend, "supports_delete")
        assert hasattr(S3Backend, "supports_list")

    def test_hosted_ingest_has_required_attributes(self) -> None:
        """HostedIngestBackend should have all required protocol attributes."""
        assert hasattr(HostedIngestBackend, "name")
        assert hasattr(HostedIngestBackend, "supports_delete")
        assert hasattr(HostedIngestBackend, "supports_list")

    def test_hosted_ingest_is_storage_backend(self) -> None:
        """HostedIngestBackend should satisfy the StorageBackend protocol."""
        assert isinstance(HostedIngestBackend(), StorageBackend)


class TestS3Backend:
    """Tests for S3-compatible storage backend."""

    def test_estimate_cost_aws(self) -> None:
        """AWS S3 cost estimation should use standard pricing."""
        backend = S3Backend(bucket="test", region="us-east-1")
        cost = backend.estimate_cost(1024**3)  # 1 GB
        assert cost is not None
        assert cost == pytest.approx(0.023, abs=0.001)

    def test_estimate_cost_r2(self) -> None:
        """R2 cost estimation should use R2 pricing."""
        backend = S3Backend(
            bucket="test",
            endpoint="https://acct.r2.cloudflarestorage.com",
        )
        cost = backend.estimate_cost(1024**3)  # 1 GB
        assert cost is not None
        assert cost == pytest.approx(0.015, abs=0.001)


class TestHostedIngestBackend:
    """Tests for the hosted ingest backend."""

    def test_estimate_cost_none(self) -> None:
        """Hosted ingest surfaces no per-upload storage cost to the client."""
        assert HostedIngestBackend().estimate_cost(1024**3) is None

    def test_direct_upload_fails_closed(self) -> None:
        """The obsolete adapter never sends unverified archive bytes."""
        from pathlib import Path

        result = HostedIngestBackend().upload(Path("/nonexistent.zip"), {})
        assert result.success is False
        assert "Direct hosted ingest is disabled" in result.error

    def test_delete_not_supported(self) -> None:
        """Hosted ingest does not support client-side delete."""
        with pytest.raises(NotImplementedError):
            HostedIngestBackend().delete("any")

    def test_direct_upload_makes_no_network_request(self, tmp_path, monkeypatch) -> None:
        """Even valid-looking bytes and credentials cannot bypass Flow."""
        archive = tmp_path / "rec.zip"
        archive.write_bytes(b"zipdata")
        result = HostedIngestBackend().upload(archive, {})

        assert result.success is False
        assert result.bytes_sent == 0

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

    def test_upload_without_auth_fails(self, monkeypatch) -> None:
        """Upload without a resolvable bearer token fails closed."""
        monkeypatch.delenv("OPENADAPT_INGEST_TOKEN", raising=False)
        monkeypatch.setattr(
            "engine.backends.hosted_ingest.auth_header", lambda: {}
        )
        from pathlib import Path

        result = HostedIngestBackend().upload(Path("/nonexistent.zip"), {})
        assert result.success is False
        assert "Not logged in" in result.error

    def test_delete_not_supported(self) -> None:
        """Hosted ingest does not support client-side delete."""
        with pytest.raises(NotImplementedError):
            HostedIngestBackend().delete("any")

    def test_upload_success(self, tmp_path, monkeypatch) -> None:
        """A 201 response yields success + dashboard URL from workflow_id."""
        from .conftest import FakeResponse

        archive = tmp_path / "rec.zip"
        archive.write_bytes(b"zipdata")
        monkeypatch.setattr(
            "engine.backends.hosted_ingest.auth_header",
            lambda: {"Authorization": "Bearer oai_ingest_x"},
        )
        captured = {}

        def _post(url, headers=None, data=None, files=None, timeout=None):
            captured["url"] = url
            captured["data"] = data
            captured["has_file"] = files is not None and "file" in files
            return FakeResponse(201, {"ingest": {"workflow_id": "wf_7"}})

        monkeypatch.setattr("engine.backends.hosted_ingest.httpx.post", _post)
        result = HostedIngestBackend(host="https://app").upload(
            archive, {"kind": "recording", "name": "My Flow"}
        )
        assert result.success is True
        assert result.remote_url == "https://app/dashboard/workflows/wf_7"
        assert captured["url"] == "https://app/api/ingest"
        assert captured["data"]["kind"] == "recording"
        assert captured["has_file"] is True

    def test_upload_401(self, tmp_path, monkeypatch) -> None:
        """A 401 is surfaced as a failed upload."""
        from .conftest import FakeResponse

        archive = tmp_path / "rec.zip"
        archive.write_bytes(b"z")
        monkeypatch.setattr(
            "engine.backends.hosted_ingest.auth_header",
            lambda: {"Authorization": "Bearer bad"},
        )
        monkeypatch.setattr(
            "engine.backends.hosted_ingest.httpx.post",
            lambda *a, **k: FakeResponse(401, {}),
        )
        result = HostedIngestBackend().upload(archive, {})
        assert result.success is False
        assert "401" in result.error

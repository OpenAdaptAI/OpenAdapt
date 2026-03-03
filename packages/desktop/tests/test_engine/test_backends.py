"""Tests for storage backends."""

from __future__ import annotations

import pytest

from engine.backends.federated import FederatedBackend
from engine.backends.huggingface import HuggingFaceBackend
from engine.backends.s3 import S3Backend
from engine.backends.wormhole import WormholeBackend


class TestProtocolConformance:
    """Verify all backends conform to the StorageBackend protocol."""

    def test_s3_has_required_attributes(self) -> None:
        """S3Backend should have all required protocol attributes."""
        assert hasattr(S3Backend, "name")
        assert hasattr(S3Backend, "supports_delete")
        assert hasattr(S3Backend, "supports_list")

    def test_huggingface_has_required_attributes(self) -> None:
        """HuggingFaceBackend should have all required protocol attributes."""
        assert hasattr(HuggingFaceBackend, "name")
        assert hasattr(HuggingFaceBackend, "supports_delete")
        assert hasattr(HuggingFaceBackend, "supports_list")

    def test_wormhole_has_required_attributes(self) -> None:
        """WormholeBackend should have all required protocol attributes."""
        assert hasattr(WormholeBackend, "name")
        assert hasattr(WormholeBackend, "supports_delete")
        assert hasattr(WormholeBackend, "supports_list")

    def test_federated_has_required_attributes(self) -> None:
        """FederatedBackend should have all required protocol attributes."""
        assert hasattr(FederatedBackend, "name")
        assert hasattr(FederatedBackend, "supports_delete")
        assert hasattr(FederatedBackend, "supports_list")


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


class TestWormholeBackend:
    """Tests for Magic Wormhole backend."""

    def test_verify_credentials_always_true(self) -> None:
        """Wormhole requires no credentials."""
        backend = WormholeBackend()
        assert backend.verify_credentials() is True

    def test_estimate_cost_always_free(self) -> None:
        """Wormhole is always free."""
        backend = WormholeBackend()
        assert backend.estimate_cost(1024**3) == 0.0

    def test_delete_raises(self) -> None:
        """Wormhole does not support deletion."""
        backend = WormholeBackend()
        with pytest.raises(NotImplementedError):
            backend.delete("any-id")


class TestFederatedBackend:
    """Tests for federated learning backend."""

    def test_estimate_cost_always_free(self) -> None:
        """Federated learning is free for participants."""
        backend = FederatedBackend()
        assert backend.estimate_cost(1024**3) == 0.0

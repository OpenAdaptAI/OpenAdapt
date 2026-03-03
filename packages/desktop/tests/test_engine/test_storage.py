"""Tests for the storage manager."""

from __future__ import annotations

import pytest

from engine.config import EngineConfig
from engine.storage_manager import StorageManager


class TestStorageManager:
    """Tests for StorageManager initialization and operations."""

    @pytest.mark.skip(reason="Not yet implemented")
    def test_initialize_creates_directories(self, config: EngineConfig) -> None:
        """Initialization should create captures, archive, and tombstones directories."""
        manager = StorageManager(config)
        manager.initialize()
        assert (config.data_dir / "captures").exists()
        assert (config.data_dir / "archive").exists()
        assert (config.data_dir / "tombstones").exists()
        assert (config.data_dir / "index.db").exists()

    @pytest.mark.skip(reason="Not yet implemented")
    def test_get_storage_usage_empty(self, config: EngineConfig) -> None:
        """Storage usage should be zero when no captures exist."""
        manager = StorageManager(config)
        manager.initialize()
        usage = manager.get_storage_usage()
        assert usage["used_bytes"] == 0
        assert usage["capture_count"] == 0

    @pytest.mark.skip(reason="Not yet implemented")
    def test_cleanup_respects_max_storage(self, config: EngineConfig) -> None:
        """Cleanup should enforce the maximum storage limit."""
        manager = StorageManager(config)
        manager.initialize()
        # TODO: Create test captures that exceed storage limit
        # TODO: Run cleanup and verify oldest captures are removed

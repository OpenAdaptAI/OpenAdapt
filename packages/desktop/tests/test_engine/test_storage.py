"""Tests for the storage manager."""

from __future__ import annotations

import json
from pathlib import Path

from engine.config import EngineConfig
from engine.storage_manager import StorageManager


class TestStorageManager:
    """Tests for StorageManager initialization and operations."""

    def test_initialize_creates_directories(self, config: EngineConfig) -> None:
        """Initialization should create captures, archive, and tombstones directories."""
        manager = StorageManager(config)
        manager.initialize()
        assert (config.data_dir / "captures").exists()
        assert (config.data_dir / "archive").exists()
        assert (config.data_dir / "tombstones").exists()
        assert (config.data_dir / "index.db").exists()

    def test_get_storage_usage_empty(self, config: EngineConfig) -> None:
        """Storage usage should be zero when no captures exist."""
        manager = StorageManager(config)
        manager.initialize()
        usage = manager.get_storage_usage()
        assert usage["used_bytes"] == 0
        assert usage["capture_count"] == 0

    def test_register_and_get_captures(
        self, config: EngineConfig, sample_capture_dir: Path,
    ) -> None:
        """Registered captures should be retrievable."""
        manager = StorageManager(config)
        manager.initialize()
        manager.register_capture("test123", sample_capture_dir)
        caps = manager.get_captures(limit=10)
        assert len(caps) == 1
        assert caps[0]["capture_id"] == "test123"

    def test_get_storage_usage_with_data(
        self, config: EngineConfig, sample_capture_dir: Path,
    ) -> None:
        """Usage should reflect file sizes."""
        manager = StorageManager(config)
        manager.initialize()
        # Add a file to the capture
        (sample_capture_dir / "data.bin").write_bytes(b"x" * 1000)
        manager.register_capture("test123", sample_capture_dir)
        usage = manager.get_storage_usage()
        assert usage["used_bytes"] > 0
        assert usage["hot_bytes"] > 0

    def test_archive_capture(
        self, config: EngineConfig, sample_capture_dir: Path,
    ) -> None:
        """Archiving should create tar.gz and remove original."""
        manager = StorageManager(config)
        manager.initialize()
        (sample_capture_dir / "data.bin").write_bytes(b"test data")
        manager.register_capture("test123", sample_capture_dir)
        manager.db.update_capture("test123", stopped_at="2026-03-02T14:35:00Z")

        archive_path = manager.archive_capture("test123")
        assert archive_path.exists()
        assert archive_path.suffix == ".gz"
        assert not sample_capture_dir.exists()

        cap = manager.db.get_capture("test123")
        assert cap["tier"] == "warm"

    def test_delete_capture(
        self, config: EngineConfig, sample_capture_dir: Path,
    ) -> None:
        """Deleting should remove files and update DB."""
        manager = StorageManager(config)
        manager.initialize()
        manager.register_capture("test123", sample_capture_dir)

        manager.delete_capture("test123")
        assert not sample_capture_dir.exists()
        cap = manager.db.get_capture("test123")
        assert cap["tier"] == "deleted"

    def test_cleanup_respects_max_storage(self, config: EngineConfig) -> None:
        """Cleanup should enforce the maximum storage limit."""
        # Set a very small max
        config.max_storage_gb = 0.000001  # ~1 KB

        manager = StorageManager(config)
        manager.initialize()

        # Create a capture that exceeds the limit
        cap_dir = config.data_dir / "captures" / "2026-01-01_00-00-00_old1"
        cap_dir.mkdir(parents=True)
        (cap_dir / "meta.json").write_text(
            json.dumps({"capture_id": "old1", "started_at": "2026-01-01T00:00:00Z"})
        )
        (cap_dir / "data.bin").write_bytes(b"x" * 10000)
        manager.register_capture("old1", cap_dir)
        manager.db.update_capture("old1", stopped_at="2026-01-01T00:05:00Z")

        actions = manager.run_cleanup()
        # Should have archived or deleted something
        assert actions["archived"] > 0 or actions["deleted"] > 0

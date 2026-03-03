"""Storage manager -- local storage tiers, cleanup, and index database.

Manages the lifecycle of captures on local disk across three tiers:

    Hot  (last 24h):  Raw captures in captures/ directory. Fully queryable.
    Warm (1-7 days):  Compressed archives (tar.zst) in archive/ directory.
    Cold (7+ days):   Uploaded to cloud + local tombstone in tombstones/.

The global index database (~/.openadapt/index.db) contains metadata about
all captures (ID, path, start time, duration, event count, size, upload status,
review status). This enables fast browsing without opening each capture's
database.

Cleanup algorithm (runs every hour):
    1. Calculate total disk usage of captures/ + archive/.
    2. If usage > max, delete oldest cold-tier archives first.
    3. If still over, compress oldest hot-tier captures to warm tier.
    4. If still over, delete oldest warm-tier archives.
    5. Never delete hot-tier captures that are currently recording.
    6. Always respect minimum retention period (default 24 hours).

See design doc Section 6 for full storage design.
"""

from __future__ import annotations

from pathlib import Path

from engine.config import EngineConfig


class StorageManager:
    """Manages local storage tiers, cleanup policies, and the capture index.

    Args:
        config: Engine configuration with storage settings.
    """

    def __init__(self, config: EngineConfig) -> None:
        self.config = config
        self.captures_dir = config.data_dir / "captures"
        self.archive_dir = config.data_dir / "archive"
        self.tombstones_dir = config.data_dir / "tombstones"
        self.index_db_path = config.data_dir / "index.db"

    def initialize(self) -> None:
        """Create storage directories and initialize the index database.

        Creates:
            - captures/ for hot-tier raw capture sessions
            - archive/ for warm-tier compressed archives
            - tombstones/ for cold-tier metadata-only records
            - index.db SQLite database with WAL mode
        """
        # TODO: Create directories
        # TODO: Initialize SQLite index.db with WAL mode
        # TODO: Create schema (captures table with metadata columns)
        raise NotImplementedError

    def register_capture(self, capture_id: str, capture_path: Path) -> None:
        """Register a new capture in the index database.

        Args:
            capture_id: Unique identifier for the capture session.
            capture_path: Path to the capture directory.
        """
        # TODO: Read meta.json from capture_path
        # TODO: Insert row into index.db
        raise NotImplementedError

    def get_captures(self, limit: int = 10) -> list[dict]:
        """Get recent captures from the index database.

        Args:
            limit: Maximum number of captures to return.

        Returns:
            List of capture metadata dicts, newest first.
        """
        # TODO: Query index.db ordered by started_at DESC
        raise NotImplementedError

    def get_storage_usage(self) -> dict:
        """Calculate current storage usage across all tiers.

        Returns:
            Dict with used_bytes, max_bytes, capture_count, and per-tier breakdown.
        """
        # TODO: Sum sizes of captures/ + archive/ directories
        # TODO: Return usage stats
        raise NotImplementedError

    def run_cleanup(self) -> dict:
        """Run the cleanup algorithm to enforce storage limits.

        Returns:
            Dict summarizing cleanup actions (archives created, files deleted, bytes freed).
        """
        # TODO: Implement the cleanup algorithm from design doc Section 6.3
        raise NotImplementedError

    def archive_capture(self, capture_id: str) -> Path:
        """Compress a hot-tier capture into a warm-tier tar.zst archive.

        Args:
            capture_id: ID of the capture to archive.

        Returns:
            Path to the created archive file.
        """
        # TODO: Create tar.zst of the capture directory
        # TODO: Move to archive/ directory
        # TODO: Update index.db with new tier and path
        raise NotImplementedError

    def delete_capture(self, capture_id: str) -> None:
        """Delete a capture from all local storage.

        Args:
            capture_id: ID of the capture to delete.
        """
        # TODO: Remove files from disk (hot, warm, or cold tier)
        # TODO: Update index.db to mark as deleted
        raise NotImplementedError

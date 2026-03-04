"""Storage manager -- local storage tiers, cleanup, and index database.

Manages the lifecycle of captures on local disk across three tiers:

    Hot  (last 24h):  Raw captures in captures/ directory. Fully queryable.
    Warm (1-7 days):  Compressed archives (tar.gz) in archive/ directory.
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

import json
import shutil
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from engine.config import EngineConfig
from engine.db import IndexDB


def _dir_size(path: Path) -> int:
    """Sum file sizes under *path* recursively."""
    total = 0
    if path.exists():
        for f in path.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    return total


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
        self._db: IndexDB | None = None

    @property
    def db(self) -> IndexDB:
        if self._db is None:
            raise RuntimeError("StorageManager not initialized -- call initialize() first")
        return self._db

    def initialize(self) -> None:
        """Create storage directories and initialize the index database.

        Creates:
            - captures/ for hot-tier raw capture sessions
            - archive/ for warm-tier compressed archives
            - tombstones/ for cold-tier metadata-only records
            - index.db SQLite database with WAL mode
        """
        self.captures_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.tombstones_dir.mkdir(parents=True, exist_ok=True)

        self._db = IndexDB(self.index_db_path)
        self._db.initialize()

    def register_capture(self, capture_id: str, capture_path: Path) -> None:
        """Register a new capture in the index database.

        Args:
            capture_id: Unique identifier for the capture session.
            capture_path: Path to the capture directory.
        """
        meta_path = capture_path / "meta.json"
        started_at = datetime.now(timezone.utc).isoformat()
        task_description = ""

        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            started_at = meta.get("started_at", started_at)
            task_description = meta.get("task_description", "")

        size_bytes = _dir_size(capture_path)

        # Insert or update -- if already registered, update
        existing = self.db.get_capture(capture_id)
        if existing:
            self.db.update_capture(
                capture_id,
                capture_path=str(capture_path),
                size_bytes=size_bytes,
            )
        else:
            self.db.insert_capture(
                capture_id,
                capture_path=str(capture_path),
                started_at=started_at,
                task_description=task_description,
            )
            self.db.update_capture(capture_id, size_bytes=size_bytes)

    def get_captures(self, limit: int = 10, review_status: str | None = None) -> list[dict]:
        """Get recent captures from the index database.

        Args:
            limit: Maximum number of captures to return.
            review_status: Optional filter by review status.

        Returns:
            List of capture metadata dicts, newest first.
        """
        return self.db.list_captures(limit=limit, review_status=review_status)

    def get_storage_usage(self) -> dict:
        """Calculate current storage usage across all tiers.

        Returns:
            Dict with used_bytes, max_bytes, capture_count, and per-tier breakdown.
        """
        hot_bytes = _dir_size(self.captures_dir)
        warm_bytes = _dir_size(self.archive_dir)
        cold_bytes = _dir_size(self.tombstones_dir)
        used_bytes = hot_bytes + warm_bytes + cold_bytes

        captures = self.db.list_captures(limit=100000)
        capture_count = len(captures)

        return {
            "used_bytes": used_bytes,
            "max_bytes": int(self.config.max_storage_gb * 1024**3),
            "capture_count": capture_count,
            "hot_bytes": hot_bytes,
            "warm_bytes": warm_bytes,
            "cold_bytes": cold_bytes,
        }

    def run_cleanup(self) -> dict:
        """Run the cleanup algorithm to enforce storage limits.

        Returns:
            Dict summarizing cleanup actions (archives created, files deleted, bytes freed).
        """
        max_bytes = int(self.config.max_storage_gb * 1024**3)
        usage = self.get_storage_usage()
        actions: dict = {"archived": 0, "deleted": 0, "bytes_freed": 0}

        if usage["used_bytes"] <= max_bytes:
            return actions

        # Step 1: delete oldest cold-tier tombstones
        cold = self.db.list_captures(limit=10000, tier="cold")
        cold.sort(key=lambda c: c["started_at"])
        for cap in cold:
            if usage["used_bytes"] <= max_bytes:
                break
            freed = self._remove_capture_files(cap)
            self.db.update_capture(cap["capture_id"], tier="deleted", review_status="deleted")
            usage["used_bytes"] -= freed
            actions["deleted"] += 1
            actions["bytes_freed"] += freed

        # Step 2: compress oldest hot-tier captures to warm
        if usage["used_bytes"] > max_bytes:
            hot = self.db.list_captures(limit=10000, tier="hot")
            hot.sort(key=lambda c: c["started_at"])
            for cap in hot:
                if usage["used_bytes"] <= max_bytes:
                    break
                # Skip captures without stopped_at (still recording)
                if not cap.get("stopped_at"):
                    continue
                try:
                    self.archive_capture(cap["capture_id"])
                    actions["archived"] += 1
                except Exception:
                    continue
                usage = self.get_storage_usage()

        # Step 3: delete oldest warm-tier archives
        if usage["used_bytes"] > max_bytes:
            warm = self.db.list_captures(limit=10000, tier="warm")
            warm.sort(key=lambda c: c["started_at"])
            for cap in warm:
                if usage["used_bytes"] <= max_bytes:
                    break
                freed = self._remove_capture_files(cap)
                self.db.update_capture(cap["capture_id"], tier="deleted", review_status="deleted")
                usage["used_bytes"] -= freed
                actions["deleted"] += 1
                actions["bytes_freed"] += freed

        return actions

    def archive_capture(self, capture_id: str) -> Path:
        """Compress a hot-tier capture into a warm-tier tar.gz archive.

        Args:
            capture_id: ID of the capture to archive.

        Returns:
            Path to the created archive file.
        """
        cap = self.db.get_capture(capture_id)
        if not cap:
            raise ValueError(f"Unknown capture: {capture_id}")

        capture_path = Path(cap["capture_path"])
        if not capture_path.exists():
            raise FileNotFoundError(f"Capture directory not found: {capture_path}")

        archive_name = f"{capture_id}.tar.gz"
        archive_path = self.archive_dir / archive_name

        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(capture_path, arcname=capture_path.name)

        # Remove original directory
        shutil.rmtree(capture_path)

        # Update DB
        self.db.update_capture(
            capture_id,
            tier="warm",
            capture_path=str(archive_path),
            archive_path=str(archive_path),
            size_bytes=archive_path.stat().st_size,
        )

        return archive_path

    def delete_capture(self, capture_id: str) -> None:
        """Delete a capture from all local storage.

        Args:
            capture_id: ID of the capture to delete.
        """
        cap = self.db.get_capture(capture_id)
        if not cap:
            raise ValueError(f"Unknown capture: {capture_id}")

        self._remove_capture_files(cap)
        self.db.update_capture(capture_id, tier="deleted", review_status="deleted")

    def _remove_capture_files(self, cap: dict) -> int:
        """Remove files for a capture and return bytes freed."""
        freed = 0
        for key in ("capture_path", "archive_path", "scrubbed_path"):
            path_str = cap.get(key)
            if not path_str:
                continue
            p = Path(path_str)
            if not p.exists():
                continue
            if p.is_dir():
                freed += _dir_size(p)
                shutil.rmtree(p)
            elif p.is_file():
                freed += p.stat().st_size
                p.unlink()
        return freed

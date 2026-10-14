"""
Centralized recording catalog system for OpenAdapt ecosystem.

This module provides automatic discovery and indexing of:
- Recordings from openadapt-capture
- Segmentation results from openadapt-ml
- Episodes and boundaries

All data is stored in a SQLite database (~/.openadapt/catalog.db) that acts
as a single source of truth for all OpenAdapt viewers and tools.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class Recording(BaseModel):
    """A captured recording."""

    id: str
    name: str
    description: str | None = None
    path: str
    created_at: float
    duration_seconds: float | None = None
    frame_count: int | None = None
    event_count: int | None = None
    task_description: str | None = None
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SegmentationResult(BaseModel):
    """Segmentation results for a recording."""

    id: str
    recording_id: str
    path: str
    created_at: float
    episode_count: int = 0
    boundary_count: int = 0
    status: str = "complete"
    llm_model: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Episode(BaseModel):
    """An episode within a recording."""

    id: str
    segmentation_result_id: str
    recording_id: str
    name: str | None = None
    description: str | None = None
    start_time: float | None = None
    end_time: float | None = None
    start_frame: int | None = None
    end_frame: int | None = None
    confidence: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RecordingCatalog:
    """Centralized catalog for all OpenAdapt recordings and results."""

    def __init__(self, db_path: str | None = None):
        """
        Initialize the catalog.

        Args:
            db_path: Path to the SQLite database. If None, uses ~/.openadapt/catalog.db
        """
        if db_path is None:
            # Use ~/.openadapt/catalog.db as default
            openadapt_dir = Path.home() / ".openadapt"
            openadapt_dir.mkdir(exist_ok=True)
            db_path = str(openadapt_dir / "catalog.db")

        self.db_path = db_path
        self._init_database()

    def _init_database(self):
        """Create database tables if they don't exist."""
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS recordings (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT,
                    path TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    duration_seconds REAL,
                    frame_count INTEGER,
                    event_count INTEGER,
                    task_description TEXT,
                    tags TEXT,
                    metadata TEXT
                );

                CREATE TABLE IF NOT EXISTS segmentation_results (
                    id TEXT PRIMARY KEY,
                    recording_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    episode_count INTEGER DEFAULT 0,
                    boundary_count INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'complete',
                    llm_model TEXT,
                    metadata TEXT,
                    FOREIGN KEY (recording_id) REFERENCES recordings(id)
                );

                CREATE TABLE IF NOT EXISTS episodes (
                    id TEXT PRIMARY KEY,
                    segmentation_result_id TEXT NOT NULL,
                    recording_id TEXT NOT NULL,
                    name TEXT,
                    description TEXT,
                    start_time REAL,
                    end_time REAL,
                    start_frame INTEGER,
                    end_frame INTEGER,
                    confidence REAL,
                    metadata TEXT,
                    FOREIGN KEY (segmentation_result_id) REFERENCES segmentation_results(id),
                    FOREIGN KEY (recording_id) REFERENCES recordings(id)
                );

                CREATE INDEX IF NOT EXISTS idx_recordings_name ON recordings(name);
                CREATE INDEX IF NOT EXISTS idx_recordings_created_at ON recordings(created_at);
                CREATE INDEX IF NOT EXISTS idx_segmentation_recording ON segmentation_results(recording_id);
                CREATE INDEX IF NOT EXISTS idx_episodes_recording ON episodes(recording_id);
                CREATE INDEX IF NOT EXISTS idx_episodes_segmentation ON episodes(segmentation_result_id);
            """)

    def register_recording(
        self,
        recording_id: str,
        name: str,
        path: str,
        created_at: float | None = None,
        **kwargs
    ) -> Recording:
        """
        Register a recording in the catalog.

        Args:
            recording_id: Unique identifier for the recording
            name: Display name
            path: Absolute path to recording directory
            created_at: Unix timestamp of creation (defaults to now)
            **kwargs: Additional recording metadata

        Returns:
            The registered Recording object
        """
        if created_at is None:
            created_at = datetime.now(timezone.utc).timestamp()

        recording = Recording(
            id=recording_id,
            name=name,
            path=path,
            created_at=created_at,
            **kwargs
        )

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO recordings
                (id, name, description, path, created_at, duration_seconds,
                 frame_count, event_count, task_description, tags, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                recording.id,
                recording.name,
                recording.description,
                recording.path,
                recording.created_at,
                recording.duration_seconds,
                recording.frame_count,
                recording.event_count,
                recording.task_description,
                json.dumps(recording.tags),
                json.dumps(recording.metadata),
            ))

        return recording

    def register_segmentation(
        self,
        segmentation_id: str,
        recording_id: str,
        path: str,
        created_at: float | None = None,
        **kwargs
    ) -> SegmentationResult:
        """
        Register segmentation results in the catalog.

        Args:
            segmentation_id: Unique identifier for the segmentation result
            recording_id: ID of the source recording
            path: Path to the segmentation JSON file
            created_at: Unix timestamp of creation (defaults to now)
            **kwargs: Additional metadata

        Returns:
            The registered SegmentationResult object
        """
        if created_at is None:
            created_at = datetime.now(timezone.utc).timestamp()

        result = SegmentationResult(
            id=segmentation_id,
            recording_id=recording_id,
            path=path,
            created_at=created_at,
            **kwargs
        )

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO segmentation_results
                (id, recording_id, path, created_at, episode_count, boundary_count,
                 status, llm_model, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                result.id,
                result.recording_id,
                result.path,
                result.created_at,
                result.episode_count,
                result.boundary_count,
                result.status,
                result.llm_model,
                json.dumps(result.metadata),
            ))

        return result

    def register_episode(
        self,
        episode_id: str,
        segmentation_result_id: str,
        recording_id: str,
        **kwargs
    ) -> Episode:
        """
        Register an episode in the catalog.

        Args:
            episode_id: Unique identifier for the episode
            segmentation_result_id: ID of the parent segmentation result
            recording_id: ID of the source recording
            **kwargs: Episode metadata (name, description, timestamps, etc.)

        Returns:
            The registered Episode object
        """
        episode = Episode(
            id=episode_id,
            segmentation_result_id=segmentation_result_id,
            recording_id=recording_id,
            **kwargs
        )

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO episodes
                (id, segmentation_result_id, recording_id, name, description,
                 start_time, end_time, start_frame, end_frame, confidence, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                episode.id,
                episode.segmentation_result_id,
                episode.recording_id,
                episode.name,
                episode.description,
                episode.start_time,
                episode.end_time,
                episode.start_frame,
                episode.end_frame,
                episode.confidence,
                json.dumps(episode.metadata),
            ))

        return episode

    def get_all_recordings(self) -> list[Recording]:
        """Get all recordings in the catalog, ordered by creation date (newest first)."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT * FROM recordings
                ORDER BY created_at DESC
            """)

            recordings = []
            for row in cursor:
                recordings.append(Recording(
                    id=row["id"],
                    name=row["name"],
                    description=row["description"],
                    path=row["path"],
                    created_at=row["created_at"],
                    duration_seconds=row["duration_seconds"],
                    frame_count=row["frame_count"],
                    event_count=row["event_count"],
                    task_description=row["task_description"],
                    tags=json.loads(row["tags"]) if row["tags"] else [],
                    metadata=json.loads(row["metadata"]) if row["metadata"] else {},
                ))

            return recordings

    def get_recording(self, recording_id: str) -> Recording | None:
        """Get a specific recording by ID."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT * FROM recordings WHERE id = ?",
                (recording_id,)
            )

            row = cursor.fetchone()
            if not row:
                return None

            return Recording(
                id=row["id"],
                name=row["name"],
                description=row["description"],
                path=row["path"],
                created_at=row["created_at"],
                duration_seconds=row["duration_seconds"],
                frame_count=row["frame_count"],
                event_count=row["event_count"],
                task_description=row["task_description"],
                tags=json.loads(row["tags"]) if row["tags"] else [],
                metadata=json.loads(row["metadata"]) if row["metadata"] else {},
            )

    def get_segmentation_results(self, recording_id: str) -> list[SegmentationResult]:
        """Get all segmentation results for a recording."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT * FROM segmentation_results WHERE recording_id = ? ORDER BY created_at DESC",
                (recording_id,)
            )

            results = []
            for row in cursor:
                results.append(SegmentationResult(
                    id=row["id"],
                    recording_id=row["recording_id"],
                    path=row["path"],
                    created_at=row["created_at"],
                    episode_count=row["episode_count"],
                    boundary_count=row["boundary_count"],
                    status=row["status"],
                    llm_model=row["llm_model"],
                    metadata=json.loads(row["metadata"]) if row["metadata"] else {},
                ))

            return results

    def get_episodes(
        self,
        recording_id: str | None = None,
        segmentation_result_id: str | None = None
    ) -> list[Episode]:
        """
        Get episodes, optionally filtered by recording or segmentation result.

        Args:
            recording_id: Filter by recording ID
            segmentation_result_id: Filter by segmentation result ID

        Returns:
            List of Episode objects
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            if segmentation_result_id:
                cursor = conn.execute(
                    "SELECT * FROM episodes WHERE segmentation_result_id = ?",
                    (segmentation_result_id,)
                )
            elif recording_id:
                cursor = conn.execute(
                    "SELECT * FROM episodes WHERE recording_id = ?",
                    (recording_id,)
                )
            else:
                cursor = conn.execute("SELECT * FROM episodes")

            episodes = []
            for row in cursor:
                episodes.append(Episode(
                    id=row["id"],
                    segmentation_result_id=row["segmentation_result_id"],
                    recording_id=row["recording_id"],
                    name=row["name"],
                    description=row["description"],
                    start_time=row["start_time"],
                    end_time=row["end_time"],
                    start_frame=row["start_frame"],
                    end_frame=row["end_frame"],
                    confidence=row["confidence"],
                    metadata=json.loads(row["metadata"]) if row["metadata"] else {},
                ))

            return episodes

    def search_recordings(
        self,
        query: str | None = None,
        tags: list[str] | None = None
    ) -> list[Recording]:
        """
        Search recordings by name/description or tags.

        Args:
            query: Search term for name/description/task_description
            tags: Filter by tags (any match)

        Returns:
            List of matching Recording objects
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            sql = "SELECT * FROM recordings WHERE 1=1"
            params = []

            if query:
                sql += " AND (name LIKE ? OR description LIKE ? OR task_description LIKE ?)"
                search_term = f"%{query}%"
                params.extend([search_term, search_term, search_term])

            if tags:
                # Check if any tag matches
                tag_conditions = " OR ".join(["tags LIKE ?" for _ in tags])
                sql += f" AND ({tag_conditions})"
                params.extend([f'%"{tag}"%' for tag in tags])

            sql += " ORDER BY created_at DESC"

            cursor = conn.execute(sql, params)

            recordings = []
            for row in cursor:
                recordings.append(Recording(
                    id=row["id"],
                    name=row["name"],
                    description=row["description"],
                    path=row["path"],
                    created_at=row["created_at"],
                    duration_seconds=row["duration_seconds"],
                    frame_count=row["frame_count"],
                    event_count=row["event_count"],
                    task_description=row["task_description"],
                    tags=json.loads(row["tags"]) if row["tags"] else [],
                    metadata=json.loads(row["metadata"]) if row["metadata"] else {},
                ))

            return recordings

    def get_stats(self) -> dict[str, Any]:
        """Get catalog statistics."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT
                    (SELECT COUNT(*) FROM recordings) as recording_count,
                    (SELECT COUNT(*) FROM segmentation_results) as segmentation_count,
                    (SELECT COUNT(*) FROM episodes) as episode_count
            """)

            row = cursor.fetchone()

            return {
                "recording_count": row[0],
                "segmentation_count": row[1],
                "episode_count": row[2],
                "db_path": self.db_path,
            }

    def clean_missing(self) -> dict[str, int]:
        """
        Remove catalog entries for recordings/files that no longer exist.

        Returns:
            Dict with counts of removed entries
        """
        removed = {"recordings": 0, "segmentations": 0}

        with sqlite3.connect(self.db_path) as conn:
            # Check recordings
            cursor = conn.execute("SELECT id, path FROM recordings")
            recordings_to_remove = []

            for row in cursor:
                if not os.path.exists(row[1]):
                    recordings_to_remove.append(row[0])

            for recording_id in recordings_to_remove:
                # Remove recording and cascade
                conn.execute("DELETE FROM episodes WHERE recording_id = ?", (recording_id,))
                conn.execute("DELETE FROM segmentation_results WHERE recording_id = ?", (recording_id,))
                conn.execute("DELETE FROM recordings WHERE id = ?", (recording_id,))
                removed["recordings"] += 1

            # Check segmentation results
            cursor = conn.execute("SELECT id, path FROM segmentation_results")
            segmentations_to_remove = []

            for row in cursor:
                if not os.path.exists(row[1]):
                    segmentations_to_remove.append(row[0])

            for seg_id in segmentations_to_remove:
                conn.execute("DELETE FROM episodes WHERE segmentation_result_id = ?", (seg_id,))
                conn.execute("DELETE FROM segmentation_results WHERE id = ?", (seg_id,))
                removed["segmentations"] += 1

        return removed


# Global catalog instance
_catalog_instance: RecordingCatalog | None = None


def get_catalog(db_path: str | None = None) -> RecordingCatalog:
    """Get the global catalog instance (singleton pattern)."""
    global _catalog_instance

    if _catalog_instance is None or (db_path and db_path != _catalog_instance.db_path):
        _catalog_instance = RecordingCatalog(db_path)

    return _catalog_instance

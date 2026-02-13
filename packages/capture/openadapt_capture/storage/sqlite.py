"""SQLite storage backend for capture events.

This module provides a SQLite-based storage implementation that wraps the
existing CaptureStorage class with a more explicit interface.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

from openadapt_capture.events import (
    AudioChunkEvent,
    Event,
    EventType,
    KeyDownEvent,
    KeyTypeEvent,
    KeyUpEvent,
    MouseClickEvent,
    MouseDoubleClickEvent,
    MouseDownEvent,
    MouseDragEvent,
    MouseMoveEvent,
    MouseScrollEvent,
    MouseUpEvent,
    ScreenFrameEvent,
)

if TYPE_CHECKING:
    from openadapt_capture.storage_impl import Capture


# Event type to class mapping
EVENT_TYPE_MAP: dict[str, type[Event]] = {
    EventType.MOUSE_MOVE.value: MouseMoveEvent,
    EventType.MOUSE_DOWN.value: MouseDownEvent,
    EventType.MOUSE_UP.value: MouseUpEvent,
    EventType.MOUSE_SCROLL.value: MouseScrollEvent,
    EventType.KEY_DOWN.value: KeyDownEvent,
    EventType.KEY_UP.value: KeyUpEvent,
    EventType.SCREEN_FRAME.value: ScreenFrameEvent,
    EventType.AUDIO_CHUNK.value: AudioChunkEvent,
    EventType.MOUSE_SINGLECLICK.value: MouseClickEvent,
    EventType.MOUSE_DOUBLECLICK.value: MouseDoubleClickEvent,
    EventType.MOUSE_DRAG.value: MouseDragEvent,
    EventType.KEY_TYPE.value: KeyTypeEvent,
}


class SQLiteStorage:
    """SQLite-based storage for capture events.

    Provides efficient storage and retrieval of events with support for:
    - Streaming writes (events written immediately to disk)
    - Querying by timestamp range and event type
    - Parent-child event relationships (for merged events)
    - Thread-safe operations

    This is a standalone implementation that can be used independently
    of the existing CaptureStorage class.

    Usage:
        storage = SQLiteStorage("capture.db")

        # Initialize schema
        storage.init_schema()

        # Write events
        storage.write_event(event)

        # Query events
        events = storage.get_events(start_time=0.0, end_time=100.0)

        # Iterate over events efficiently
        for event in storage.iter_events():
            process(event)

        storage.close()
    """

    # SQL schema
    CREATE_EVENTS_TABLE = """
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL NOT NULL,
        type TEXT NOT NULL,
        data JSON NOT NULL,
        parent_id INTEGER,
        FOREIGN KEY (parent_id) REFERENCES events(id)
    )
    """

    CREATE_EVENTS_INDEX = """
    CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp)
    """

    CREATE_EVENTS_TYPE_INDEX = """
    CREATE INDEX IF NOT EXISTS idx_events_type ON events(type)
    """

    CREATE_CAPTURE_TABLE = """
    CREATE TABLE IF NOT EXISTS capture (
        id TEXT PRIMARY KEY,
        started_at REAL NOT NULL,
        ended_at REAL,
        platform TEXT NOT NULL,
        screen_width INTEGER NOT NULL,
        screen_height INTEGER NOT NULL,
        pixel_ratio REAL DEFAULT 1.0,
        task_description TEXT,
        double_click_interval_seconds REAL,
        double_click_distance_pixels REAL,
        video_start_time REAL,
        audio_start_time REAL,
        metadata JSON
    )
    """

    def __init__(self, db_path: str | Path, auto_init: bool = True) -> None:
        """Initialize SQLite storage.

        Args:
            db_path: Path to SQLite database file. Created if doesn't exist.
            auto_init: Whether to automatically initialize the schema.
        """
        self.db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

        if auto_init:
            self.init_schema()

    @property
    def is_open(self) -> bool:
        """Check if database connection is open."""
        return self._conn is not None

    @property
    def conn(self) -> sqlite3.Connection:
        """Get or create database connection."""
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
            )
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def init_schema(self) -> None:
        """Initialize database schema."""
        cursor = self.conn.cursor()
        cursor.execute(self.CREATE_CAPTURE_TABLE)
        cursor.execute(self.CREATE_EVENTS_TABLE)
        cursor.execute(self.CREATE_EVENTS_INDEX)
        cursor.execute(self.CREATE_EVENTS_TYPE_INDEX)
        self.conn.commit()

    def close(self) -> None:
        """Close database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "SQLiteStorage":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit."""
        self.close()

    # -------------------------------------------------------------------------
    # Capture metadata methods
    # -------------------------------------------------------------------------

    def save_capture(self, capture: "Capture") -> None:
        """Save capture metadata.

        Args:
            capture: Capture metadata to store.
        """
        cursor = self.conn.cursor()

        # Check if capture exists
        cursor.execute("SELECT id FROM capture WHERE id = ?", (capture.id,))
        exists = cursor.fetchone() is not None

        if exists:
            # Update existing
            cursor.execute(
                """
                UPDATE capture SET
                    ended_at = ?,
                    task_description = ?,
                    video_start_time = ?,
                    audio_start_time = ?,
                    metadata = ?
                WHERE id = ?
                """,
                (
                    capture.ended_at,
                    capture.task_description,
                    capture.video_start_time,
                    capture.audio_start_time,
                    json.dumps(capture.metadata),
                    capture.id,
                ),
            )
        else:
            # Insert new
            cursor.execute(
                """
                INSERT INTO capture (
                    id, started_at, ended_at, platform, screen_width, screen_height,
                    pixel_ratio, task_description, double_click_interval_seconds,
                    double_click_distance_pixels, video_start_time, audio_start_time, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    capture.id,
                    capture.started_at,
                    capture.ended_at,
                    capture.platform,
                    capture.screen_width,
                    capture.screen_height,
                    capture.pixel_ratio,
                    capture.task_description,
                    capture.double_click_interval_seconds,
                    capture.double_click_distance_pixels,
                    capture.video_start_time,
                    capture.audio_start_time,
                    json.dumps(capture.metadata),
                ),
            )
        self.conn.commit()

    def load_capture(self) -> "Capture | None":
        """Load capture metadata.

        Returns:
            Capture object or None if not found.
        """
        from openadapt_capture.storage_impl import Capture

        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM capture ORDER BY started_at DESC LIMIT 1")
        row = cursor.fetchone()

        if row is None:
            return None

        return Capture(
            id=row["id"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            platform=row["platform"],
            screen_width=row["screen_width"],
            screen_height=row["screen_height"],
            pixel_ratio=row["pixel_ratio"] if "pixel_ratio" in row.keys() else 1.0,
            task_description=row["task_description"],
            double_click_interval_seconds=row["double_click_interval_seconds"],
            double_click_distance_pixels=row["double_click_distance_pixels"],
            video_start_time=row["video_start_time"],
            audio_start_time=row["audio_start_time"] if "audio_start_time" in row.keys() else None,
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
        )

    # -------------------------------------------------------------------------
    # Event methods
    # -------------------------------------------------------------------------

    def write_event(self, event: Event, parent_id: int | None = None) -> int:
        """Write a single event to storage.

        Thread-safe: uses locking for concurrent access.

        Args:
            event: Event to write.
            parent_id: Optional parent event ID for merged events.

        Returns:
            ID of the inserted event.
        """
        with self._lock:
            cursor = self.conn.cursor()
            event_dict = event.model_dump(
                exclude={"children"} if hasattr(event, "children") else None
            )
            cursor.execute(
                "INSERT INTO events (timestamp, type, data, parent_id) VALUES (?, ?, ?, ?)",
                (
                    event.timestamp,
                    event.type if isinstance(event.type, str) else event.type.value,
                    json.dumps(event_dict),
                    parent_id,
                ),
            )
            event_id = cursor.lastrowid
            self.conn.commit()

        # Write children if present
        if hasattr(event, "children") and event.children:
            for child in event.children:
                self.write_event(child, parent_id=event_id)

        return event_id

    def write_events(self, events: list[Event]) -> list[int]:
        """Write multiple events in a single transaction.

        Args:
            events: List of events to write.

        Returns:
            List of inserted event IDs.
        """
        event_ids = []
        with self._lock:
            cursor = self.conn.cursor()
            for event in events:
                event_dict = event.model_dump(
                    exclude={"children"} if hasattr(event, "children") else None
                )
                cursor.execute(
                    "INSERT INTO events (timestamp, type, data, parent_id) VALUES (?, ?, ?, ?)",
                    (
                        event.timestamp,
                        event.type if isinstance(event.type, str) else event.type.value,
                        json.dumps(event_dict),
                        None,
                    ),
                )
                event_id = cursor.lastrowid
                event_ids.append(event_id)

                # Write children
                if hasattr(event, "children") and event.children:
                    for child in event.children:
                        child_dict = child.model_dump(
                            exclude={"children"} if hasattr(child, "children") else None
                        )
                        cursor.execute(
                            "INSERT INTO events (timestamp, type, data, parent_id) VALUES (?, ?, ?, ?)",
                            (
                                child.timestamp,
                                child.type if isinstance(child.type, str) else child.type.value,
                                json.dumps(child_dict),
                                event_id,
                            ),
                        )
            self.conn.commit()
        return event_ids

    def get_events(
        self,
        start_time: float | None = None,
        end_time: float | None = None,
        event_types: list[EventType | str] | None = None,
        include_children: bool = False,
        limit: int | None = None,
    ) -> list[Event]:
        """Query events from storage.

        Args:
            start_time: Minimum timestamp (inclusive).
            end_time: Maximum timestamp (inclusive).
            event_types: Filter by event types.
            include_children: Whether to include child events.
            limit: Maximum number of events to return.

        Returns:
            List of events matching the query.
        """
        cursor = self.conn.cursor()

        conditions = []
        params: list[Any] = []

        if not include_children:
            conditions.append("parent_id IS NULL")

        if start_time is not None:
            conditions.append("timestamp >= ?")
            params.append(start_time)

        if end_time is not None:
            conditions.append("timestamp <= ?")
            params.append(end_time)

        if event_types:
            placeholders = ",".join("?" for _ in event_types)
            conditions.append(f"type IN ({placeholders})")
            params.extend(
                t.value if isinstance(t, EventType) else t for t in event_types
            )

        where_clause = " AND ".join(conditions) if conditions else "1=1"
        query = f"SELECT * FROM events WHERE {where_clause} ORDER BY timestamp"

        if limit:
            query += f" LIMIT {limit}"

        cursor.execute(query, params)
        rows = cursor.fetchall()

        events = []
        for row in rows:
            event = self._deserialize_event(row)
            if event is not None:
                events.append(event)

        return events

    def _deserialize_event(self, row: sqlite3.Row) -> Event | None:
        """Deserialize an event from a database row."""
        event_type = row["type"]
        event_data = json.loads(row["data"])

        event_class = EVENT_TYPE_MAP.get(event_type)
        if event_class is None:
            return None

        return event_class(**event_data)

    def get_event_count(self, event_type: EventType | str | None = None) -> int:
        """Get count of events in storage.

        Args:
            event_type: Optional filter by event type.

        Returns:
            Number of events.
        """
        cursor = self.conn.cursor()
        if event_type is not None:
            type_value = event_type.value if isinstance(event_type, EventType) else event_type
            cursor.execute(
                "SELECT COUNT(*) FROM events WHERE type = ? AND parent_id IS NULL",
                (type_value,),
            )
        else:
            cursor.execute("SELECT COUNT(*) FROM events WHERE parent_id IS NULL")
        return cursor.fetchone()[0]

    def iter_events(
        self,
        batch_size: int = 1000,
        event_types: list[EventType | str] | None = None,
    ) -> Iterator[Event]:
        """Iterate over events in batches for memory efficiency.

        Args:
            batch_size: Number of events per batch.
            event_types: Filter by event types.

        Yields:
            Events one at a time.
        """
        cursor = self.conn.cursor()

        conditions = ["parent_id IS NULL"]
        params: list[Any] = []

        if event_types:
            placeholders = ",".join("?" for _ in event_types)
            conditions.append(f"type IN ({placeholders})")
            params.extend(
                t.value if isinstance(t, EventType) else t for t in event_types
            )

        where_clause = " AND ".join(conditions)
        query = f"SELECT * FROM events WHERE {where_clause} ORDER BY timestamp"

        cursor.execute(query, params)

        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            for row in rows:
                event = self._deserialize_event(row)
                if event is not None:
                    yield event

    def delete_events(
        self,
        start_time: float | None = None,
        end_time: float | None = None,
        event_types: list[EventType | str] | None = None,
    ) -> int:
        """Delete events from storage.

        Args:
            start_time: Minimum timestamp (inclusive).
            end_time: Maximum timestamp (inclusive).
            event_types: Filter by event types.

        Returns:
            Number of deleted events.
        """
        cursor = self.conn.cursor()

        conditions = []
        params: list[Any] = []

        if start_time is not None:
            conditions.append("timestamp >= ?")
            params.append(start_time)

        if end_time is not None:
            conditions.append("timestamp <= ?")
            params.append(end_time)

        if event_types:
            placeholders = ",".join("?" for _ in event_types)
            conditions.append(f"type IN ({placeholders})")
            params.extend(
                t.value if isinstance(t, EventType) else t for t in event_types
            )

        where_clause = " AND ".join(conditions) if conditions else "1=1"
        query = f"DELETE FROM events WHERE {where_clause}"

        cursor.execute(query, params)
        deleted = cursor.rowcount
        self.conn.commit()

        return deleted


__all__ = ["SQLiteStorage", "EVENT_TYPE_MAP"]

"""SQLite storage for capture events.

This module provides a simple SQLite-based storage system for capture events,
following OpenAdapt's approach but using Pydantic for serialization.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, Field

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

# =============================================================================
# Capture and Stream Models
# =============================================================================


class Stream(BaseModel):
    """A time-ordered sequence of events of a single type.

    Streams organize events by category: action (input), screen, or audio.
    """

    id: str = Field(description="Unique stream identifier")
    stream_type: str = Field(description="Stream type: 'action' | 'screen' | 'audio'")
    events: list[Event] = Field(default_factory=list, description="Time-ordered events")


class Capture(BaseModel):
    """A complete capture session containing multiple streams.

    The Capture is the top-level container for a recording session,
    containing action events, screen frames, and optionally audio.
    """

    id: str = Field(description="Unique capture identifier")
    started_at: float = Field(description="Unix timestamp when capture started")
    ended_at: float | None = Field(default=None, description="Unix timestamp when capture ended")
    platform: str = Field(description="Platform identifier: 'darwin' | 'win32' | 'linux'")
    screen_width: int = Field(description="Screen width in physical pixels")
    screen_height: int = Field(description="Screen height in physical pixels")
    pixel_ratio: float = Field(
        default=1.0,
        description="Display pixel ratio (physical/logical), e.g., 2.0 for Retina"
    )
    task_description: str | None = Field(default=None, description="User-provided task description")
    double_click_interval_seconds: float = Field(
        default=0.5, description="System double-click interval"
    )
    double_click_distance_pixels: float = Field(
        default=5.0, description="System double-click distance threshold"
    )
    video_start_time: float | None = Field(
        default=None, description="Start timestamp of video recording"
    )
    audio_start_time: float | None = Field(
        default=None, description="Start timestamp of audio recording"
    )
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")

    model_config = {"use_enum_values": True}


# =============================================================================
# SQLite Storage
# =============================================================================

# SQL schema for events table
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

# SQL schema for capture metadata
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


class CaptureStorage:
    """SQLite-based storage for capture events.

    Provides efficient storage and retrieval of events with support for:
    - Streaming writes (events written immediately to disk)
    - Querying by timestamp range and event type
    - Parent-child event relationships (for merged events)

    Usage:
        storage = CaptureStorage("capture.db")
        storage.init_capture(capture)

        # Write events as they come in
        storage.write_event(event)

        # Query events
        events = storage.get_events(start_time=0.0, end_time=100.0)

        storage.close()
    """

    def __init__(self, db_path: str | Path) -> None:
        """Initialize storage with database path.

        Args:
            db_path: Path to SQLite database file. Created if doesn't exist.
        """
        import threading
        self.db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

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
                check_same_thread=False,  # Allow multi-threaded access
            )
            self._conn.row_factory = sqlite3.Row
            self._init_schema()
        return self._conn

    def _init_schema(self) -> None:
        """Initialize database schema."""
        cursor = self.conn.cursor()
        cursor.execute(CREATE_CAPTURE_TABLE)
        cursor.execute(CREATE_EVENTS_TABLE)
        cursor.execute(CREATE_EVENTS_INDEX)
        cursor.execute(CREATE_EVENTS_TYPE_INDEX)
        self.conn.commit()

    def close(self) -> None:
        """Close database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "CaptureStorage":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit."""
        self.close()

    # -------------------------------------------------------------------------
    # Capture methods
    # -------------------------------------------------------------------------

    def init_capture(self, capture: Capture) -> None:
        """Initialize a new capture session.

        Args:
            capture: Capture metadata to store.
        """
        cursor = self.conn.cursor()
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

    def update_capture(self, capture: Capture) -> None:
        """Update capture metadata (e.g., when capture ends).

        Args:
            capture: Updated capture metadata.
        """
        cursor = self.conn.cursor()
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
        self.conn.commit()

    def get_capture(self) -> Capture | None:
        """Get capture metadata.

        Returns:
            Capture object or None if not initialized.
        """
        cursor = self.conn.cursor()
        # Get most recent capture (by started_at) to handle reused directories
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

        Thread-safe: uses locking for concurrent access from multiple threads.

        Args:
            event: Event to write.
            parent_id: Optional parent event ID for merged events.

        Returns:
            ID of the inserted event.
        """
        with self._lock:
            cursor = self.conn.cursor()
            # Serialize event to JSON, excluding children (stored separately)
            event_dict = event.model_dump(exclude={"children"} if hasattr(event, "children") else None)
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

        # Write children if present (outside lock to avoid deadlock with recursive calls)
        if hasattr(event, "children") and event.children:
            for child in event.children:
                self.write_event(child, parent_id=event_id)

        return event_id

    def write_events(self, events: list[Event]) -> None:
        """Write multiple events to storage in a single transaction.

        Args:
            events: List of events to write.
        """
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

            # Write children if present
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

    def get_events(
        self,
        start_time: float | None = None,
        end_time: float | None = None,
        event_types: list[EventType | str] | None = None,
        include_children: bool = False,
    ) -> list[Event]:
        """Query events from storage.

        Args:
            start_time: Minimum timestamp (inclusive).
            end_time: Maximum timestamp (inclusive).
            event_types: Filter by event types.
            include_children: Whether to include child events (for merged events).

        Returns:
            List of events matching the query.
        """
        cursor = self.conn.cursor()

        # Build query
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

        cursor.execute(query, params)
        rows = cursor.fetchall()

        # Deserialize events
        events = []
        for row in rows:
            event = self._deserialize_event(row)
            if event is not None:
                events.append(event)

        return events

    def _deserialize_event(self, row: sqlite3.Row) -> Event | None:
        """Deserialize an event from a database row.

        Args:
            row: Database row.

        Returns:
            Deserialized event or None if type unknown.
        """
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

        # Build query
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


# =============================================================================
# Convenience functions
# =============================================================================


def _detect_platform() -> str:
    """Detect the current platform."""
    import sys
    return sys.platform


def _detect_screen_size() -> tuple[int, int]:
    """Detect screen dimensions."""
    try:
        from PIL import ImageGrab
        screenshot = ImageGrab.grab()
        return screenshot.size
    except Exception:
        return (1920, 1080)  # Fallback default


def create_capture(
    capture_dir: str | Path,
    task_description: str | None = None,
    platform: str | None = None,
    screen_width: int | None = None,
    screen_height: int | None = None,
) -> tuple[Capture, CaptureStorage]:
    """Create a new capture with storage.

    Args:
        capture_dir: Directory for capture files.
        task_description: Optional description of the task being recorded.
        platform: Platform identifier (auto-detected if not provided).
        screen_width: Screen width in pixels (auto-detected if not provided).
        screen_height: Screen height in pixels (auto-detected if not provided).

    Returns:
        Tuple of (Capture, CaptureStorage).
    """
    import uuid

    capture_dir = Path(capture_dir)
    capture_dir.mkdir(parents=True, exist_ok=True)

    # Auto-detect platform and screen size if not provided
    if platform is None:
        platform = _detect_platform()

    if screen_width is None or screen_height is None:
        detected_width, detected_height = _detect_screen_size()
        screen_width = screen_width or detected_width
        screen_height = screen_height or detected_height

    capture_id = str(uuid.uuid4())[:8]
    started_at = time.time()

    capture = Capture(
        id=capture_id,
        started_at=started_at,
        platform=platform,
        screen_width=screen_width,
        screen_height=screen_height,
        task_description=task_description,
    )

    db_path = capture_dir / "capture.db"
    storage = CaptureStorage(db_path)
    storage.init_capture(capture)

    return capture, storage


def load_capture(capture_dir: str | Path) -> tuple[Capture | None, CaptureStorage]:
    """Load an existing capture from storage.

    Args:
        capture_dir: Directory containing capture files.

    Returns:
        Tuple of (Capture, CaptureStorage). Capture is None if not found.
    """
    capture_dir = Path(capture_dir)
    db_path = capture_dir / "capture.db"

    if not db_path.exists():
        raise FileNotFoundError(f"Capture database not found: {db_path}")

    storage = CaptureStorage(db_path)
    capture = storage.get_capture()

    return capture, storage

"""High-level capture loading and iteration API.

Provides time-aligned access to captured events with associated screenshots.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from openadapt_capture.events import (
    ActionEvent,
    EventType,
    KeyDownEvent,
    KeyTypeEvent,
    MouseMoveEvent,
    ScreenFrameEvent,
)
from openadapt_capture.processing import process_events
from openadapt_capture.storage import Capture as CaptureMetadata
from openadapt_capture.storage import CaptureStorage

if TYPE_CHECKING:
    from PIL import Image


@dataclass
class Action:
    """A processed action event with associated screenshot.

    Represents a user action (click, type, drag, etc.) along with
    the screen state at the time of the action.
    """

    event: ActionEvent
    _capture: "CaptureSession"

    @property
    def timestamp(self) -> float:
        """Unix timestamp of the action."""
        return self.event.timestamp

    @property
    def type(self) -> str:
        """Action type (e.g., 'mouse.singleclick', 'key.type')."""
        return self.event.type if isinstance(self.event.type, str) else self.event.type.value

    @property
    def x(self) -> float | None:
        """X coordinate for mouse actions (start position for drags)."""
        if hasattr(self.event, "x"):
            return self.event.x
        return None

    @property
    def y(self) -> float | None:
        """Y coordinate for mouse actions (start position for drags)."""
        if hasattr(self.event, "y"):
            return self.event.y
        return None

    @property
    def text(self) -> str | None:
        """Typed text for keyboard actions."""
        if isinstance(self.event, KeyTypeEvent):
            return self.event.text
        return None

    @property
    def keys(self) -> list[str] | None:
        """Key names for keyboard actions (useful when text is empty).

        Returns list of key names like ['ctrl', 'space'] or ['enter'].
        """
        if isinstance(self.event, KeyTypeEvent):
            key_names = []
            seen = set()
            for child in self.event.children:
                if isinstance(child, KeyDownEvent):
                    # Get key identifier
                    key_id = child.key_name or child.key_char or child.key_vk
                    if key_id and key_id not in seen:
                        seen.add(key_id)
                        key_names.append(key_id)
            return key_names if key_names else None
        return None

    @property
    def screenshot(self) -> "Image" | None:
        """Get the screenshot at the time of this action.

        Returns:
            PIL Image of the screen at action time, or None if not available.
        """
        return self._capture.get_frame_at(self.timestamp)


class CaptureSession:
    """A loaded capture session for analysis and replay.

    Provides access to time-aligned events and screenshots.

    Usage:
        capture = CaptureSession.load("./my_capture")

        for action in capture.actions():
            print(f"{action.type} at {action.timestamp}")
            img = action.screenshot
    """

    def __init__(
        self,
        capture_dir: str | Path,
        storage: CaptureStorage,
        metadata: CaptureMetadata,
    ) -> None:
        """Initialize capture session.

        Use CaptureSession.load() instead of calling this directly.
        """
        self.capture_dir = Path(capture_dir)
        self._storage = storage
        self._metadata = metadata
        self._video_container = None
        self._screen_events: list[ScreenFrameEvent] | None = None

    @classmethod
    def load(cls, capture_dir: str | Path) -> "CaptureSession":
        """Load a capture from disk.

        Args:
            capture_dir: Path to capture directory.

        Returns:
            CaptureSession instance.

        Raises:
            FileNotFoundError: If capture doesn't exist.
        """
        capture_dir = Path(capture_dir)
        db_path = capture_dir / "capture.db"

        if not db_path.exists():
            raise FileNotFoundError(f"Capture not found: {capture_dir}")

        storage = CaptureStorage(db_path)
        metadata = storage.get_capture()

        if metadata is None:
            raise FileNotFoundError(f"Invalid capture: {capture_dir}")

        return cls(capture_dir, storage, metadata)

    @property
    def id(self) -> str:
        """Capture ID."""
        return self._metadata.id

    @property
    def started_at(self) -> float:
        """Start timestamp."""
        return self._metadata.started_at

    @property
    def ended_at(self) -> float | None:
        """End timestamp."""
        return self._metadata.ended_at

    @property
    def duration(self) -> float | None:
        """Duration in seconds."""
        if self._metadata.ended_at is not None:
            return self._metadata.ended_at - self._metadata.started_at
        return None

    @property
    def platform(self) -> str:
        """Platform (darwin, win32, linux)."""
        return self._metadata.platform

    @property
    def screen_size(self) -> tuple[int, int]:
        """Screen dimensions (width, height) in physical pixels."""
        return (self._metadata.screen_width, self._metadata.screen_height)

    @property
    def pixel_ratio(self) -> float:
        """Display pixel ratio (physical/logical), e.g., 2.0 for Retina."""
        return self._metadata.pixel_ratio

    @property
    def task_description(self) -> str | None:
        """Task description."""
        return self._metadata.task_description

    @property
    def video_path(self) -> Path | None:
        """Path to video file if exists."""
        video_path = self.capture_dir / "video.mp4"
        return video_path if video_path.exists() else None

    @property
    def audio_path(self) -> Path | None:
        """Path to audio file if exists."""
        audio_path = self.capture_dir / "audio.flac"
        return audio_path if audio_path.exists() else None

    def raw_events(self) -> list[ActionEvent]:
        """Get all raw action events (unprocessed).

        Returns:
            List of raw mouse and keyboard events.
        """
        action_types = [
            EventType.MOUSE_MOVE,
            EventType.MOUSE_DOWN,
            EventType.MOUSE_UP,
            EventType.MOUSE_SCROLL,
            EventType.KEY_DOWN,
            EventType.KEY_UP,
        ]
        # Filter by capture's timestamp range to handle reused directories
        return self._storage.get_events(
            event_types=action_types,
            start_time=self._metadata.started_at,
            end_time=self._metadata.ended_at,
        )

    def actions(self, include_moves: bool = False) -> Iterator[Action]:
        """Iterate over processed actions.

        Yields time-ordered actions (clicks, drags, typed text) with
        associated screenshots.

        Args:
            include_moves: Whether to include mouse move events.

        Yields:
            Action objects with event data and screenshot access.
        """
        # Get and process raw events
        raw_events = self.raw_events()
        processed = process_events(
            raw_events,
            double_click_interval=self._metadata.double_click_interval_seconds,
            double_click_distance=self._metadata.double_click_distance_pixels,
        )

        # Filter out moves if not requested
        for event in processed:
            if not include_moves and isinstance(event, MouseMoveEvent):
                continue
            yield Action(event=event, _capture=self)

    def get_frame_at(self, timestamp: float, tolerance: float = 0.5) -> "Image" | None:
        """Get the screen frame closest to a timestamp.

        Args:
            timestamp: Unix timestamp.
            tolerance: Maximum time difference in seconds.

        Returns:
            PIL Image or None if not available.
        """
        video_path = self.video_path
        if video_path is None:
            return None

        try:
            from openadapt_capture.video import extract_frame

            # Convert to video-relative timestamp
            video_start = self._metadata.video_start_time or self._metadata.started_at
            video_timestamp = timestamp - video_start

            if video_timestamp < 0:
                video_timestamp = 0

            return extract_frame(video_path, video_timestamp, tolerance=tolerance)
        except Exception:
            return None

    def close(self) -> None:
        """Close the capture and release resources."""
        if self._storage is not None:
            self._storage.close()
            self._storage = None

    def __enter__(self) -> "CaptureSession":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        self.close()


# Alias for simpler import
Capture = CaptureSession

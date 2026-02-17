"""High-level capture loading and iteration API.

Provides time-aligned access to captured events with associated screenshots.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from openadapt_capture.events import (
    ActionEvent as PydanticActionEvent,
)
from openadapt_capture.events import (
    KeyDownEvent,
    KeyTypeEvent,
    KeyUpEvent,
    MouseButton,
    MouseDownEvent,
    MouseMoveEvent,
    MouseScrollEvent,
    MouseUpEvent,
)
from openadapt_capture.processing import process_events

if TYPE_CHECKING:
    from PIL import Image


def _convert_action_event(db_event) -> PydanticActionEvent | None:
    """Convert a SQLAlchemy ActionEvent to a Pydantic event.

    Args:
        db_event: SQLAlchemy ActionEvent instance.

    Returns:
        Pydantic event or None if unrecognized.
    """
    ts = db_event.timestamp

    if db_event.name == "move":
        return MouseMoveEvent(
            timestamp=ts,
            x=db_event.mouse_x or 0,
            y=db_event.mouse_y or 0,
        )
    elif db_event.name == "click":
        button = db_event.mouse_button_name or "left"
        try:
            button = MouseButton(button)
        except ValueError:
            button = MouseButton.LEFT

        if db_event.mouse_pressed is True:
            return MouseDownEvent(
                timestamp=ts,
                x=db_event.mouse_x or 0,
                y=db_event.mouse_y or 0,
                button=button,
            )
        elif db_event.mouse_pressed is False:
            return MouseUpEvent(
                timestamp=ts,
                x=db_event.mouse_x or 0,
                y=db_event.mouse_y or 0,
                button=button,
            )
        else:
            return None
    elif db_event.name == "scroll":
        return MouseScrollEvent(
            timestamp=ts,
            x=db_event.mouse_x or 0,
            y=db_event.mouse_y or 0,
            dx=db_event.mouse_dx or 0,
            dy=db_event.mouse_dy or 0,
        )
    elif db_event.name == "press":
        return KeyDownEvent(
            timestamp=ts,
            key_name=db_event.key_name,
            key_char=db_event.key_char,
            key_vk=db_event.key_vk,
            canonical_key_name=db_event.canonical_key_name,
            canonical_key_char=db_event.canonical_key_char,
            canonical_key_vk=db_event.canonical_key_vk,
        )
    elif db_event.name == "release":
        return KeyUpEvent(
            timestamp=ts,
            key_name=db_event.key_name,
            key_char=db_event.key_char,
            key_vk=db_event.key_vk,
            canonical_key_name=db_event.canonical_key_name,
            canonical_key_char=db_event.canonical_key_char,
            canonical_key_vk=db_event.canonical_key_vk,
        )
    return None


@dataclass
class Action:
    """A processed action event with associated screenshot.

    Represents a user action (click, type, drag, etc.) along with
    the screen state at the time of the action.
    """

    event: PydanticActionEvent
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
    def dx(self) -> float | None:
        """Horizontal displacement for scroll/drag actions."""
        if hasattr(self.event, "dx"):
            return self.event.dx
        return None

    @property
    def dy(self) -> float | None:
        """Vertical displacement for scroll/drag actions."""
        if hasattr(self.event, "dy"):
            return self.event.dy
        return None

    @property
    def button(self) -> str | None:
        """Mouse button for click/drag actions."""
        if hasattr(self.event, "button"):
            btn = self.event.button
            return btn.value if hasattr(btn, "value") else str(btn)
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
    Reads from the SQLAlchemy-based per-capture database (recording.db).

    Usage:
        capture = CaptureSession.load("./my_capture")

        for action in capture.actions():
            print(f"{action.type} at {action.timestamp}")
            img = action.screenshot
    """

    def __init__(
        self,
        capture_dir: str | Path,
        session,
        recording,
    ) -> None:
        """Initialize capture session.

        Use CaptureSession.load() instead of calling this directly.
        """
        self.capture_dir = Path(capture_dir)
        self._session = session
        self._recording = recording

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
        db_path = capture_dir / "recording.db"

        if not db_path.exists():
            raise FileNotFoundError(f"Capture not found: {capture_dir}")

        from openadapt_capture.db import get_session_for_path
        from openadapt_capture.db.models import Recording

        session = get_session_for_path(str(db_path))
        try:
            recording = session.query(Recording).first()
        except Exception:
            session.close()
            raise

        if recording is None:
            session.close()
            raise FileNotFoundError(f"Invalid capture (no recording found): {capture_dir}")

        return cls(capture_dir, session, recording)

    @property
    def id(self) -> str:
        """Capture ID."""
        return str(self._recording.id)

    @property
    def started_at(self) -> float:
        """Start timestamp."""
        return self._recording.timestamp

    @property
    def ended_at(self) -> float | None:
        """End timestamp (from last action event)."""
        if self._recording.action_events:
            return self._recording.action_events[-1].timestamp
        return None

    @property
    def duration(self) -> float | None:
        """Duration in seconds."""
        ended = self.ended_at
        if ended is not None:
            return ended - self._recording.timestamp
        return None

    @property
    def platform(self) -> str:
        """Platform (darwin, win32, linux)."""
        return self._recording.platform or ""

    @property
    def screen_size(self) -> tuple[int, int]:
        """Screen dimensions (width, height) in physical pixels."""
        return (
            self._recording.monitor_width or 0,
            self._recording.monitor_height or 0,
        )

    @property
    def task_description(self) -> str | None:
        """Task description."""
        return self._recording.task_description

    @property
    def video_path(self) -> Path | None:
        """Path to video file if exists."""
        # Legacy format: oa_recording-{timestamp}.mp4
        for p in self.capture_dir.glob("oa_recording-*.mp4"):
            return p
        # Fallback: video.mp4
        video_path = self.capture_dir / "video.mp4"
        return video_path if video_path.exists() else None

    @property
    def audio_path(self) -> Path | None:
        """Path to audio file if exists."""
        audio_path = self.capture_dir / "audio.flac"
        return audio_path if audio_path.exists() else None

    def raw_events(self) -> list[PydanticActionEvent]:
        """Get all raw action events (unprocessed).

        Converts SQLAlchemy ActionEvent models to Pydantic events.

        Returns:
            List of raw mouse and keyboard events.
        """
        events = []
        for db_event in self._recording.action_events:
            if getattr(db_event, "disabled", False):
                continue
            pydantic_event = _convert_action_event(db_event)
            if pydantic_event is not None:
                events.append(pydantic_event)
        return events

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
            double_click_interval=self._recording.double_click_interval_seconds or 0.5,
            double_click_distance=self._recording.double_click_distance_pixels or 5,
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
            video_start = self._recording.video_start_time or self._recording.timestamp
            video_timestamp = timestamp - video_start

            if video_timestamp < 0:
                video_timestamp = 0

            return extract_frame(video_path, video_timestamp, tolerance=tolerance)
        except Exception:
            return None

    def close(self) -> None:
        """Close the capture and release resources."""
        if self._session is not None:
            self._session.close()
            self._session = None

    def __enter__(self) -> "CaptureSession":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        self.close()


# Alias for simpler import
Capture = CaptureSession

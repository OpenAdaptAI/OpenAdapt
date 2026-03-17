"""High-level capture loading and iteration API.

Provides time-aligned access to captured events with associated screenshots.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from openadapt_capture.browser_events import (
    BoundingBox,
    BrowserClickEvent,
    BrowserEventType,
    BrowserFocusEvent,
    BrowserInputEvent,
    BrowserKeyEvent,
    BrowserMouseMoveEvent,
    BrowserNavigationEvent,
    BrowserScrollEvent,
    ElementState,
    NavigationType,
    SemanticElementRef,
)
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

    from openadapt_capture.browser_events import BrowserEvent


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


def _parse_element_ref(raw: dict | None) -> SemanticElementRef | None:
    """Parse a raw element dict into a SemanticElementRef.

    Handles field name variations between the content-script format
    (e.g. ``dataId``, ``tagName``, ``classList``) and snake_case alternatives.
    """
    if not raw or not isinstance(raw, dict):
        return None

    bbox_raw = raw.get("bbox", {})
    bbox = BoundingBox(
        x=bbox_raw.get("x", 0),
        y=bbox_raw.get("y", 0),
        width=bbox_raw.get("width", 0),
        height=bbox_raw.get("height", 0),
    )

    state_raw = raw.get("state", {})
    state = ElementState(
        enabled=state_raw.get("enabled", True),
        focused=state_raw.get("focused", False),
        visible=state_raw.get("visible", True),
        checked=state_raw.get("checked"),
        selected=state_raw.get("selected"),
        expanded=state_raw.get("expanded"),
        value=state_raw.get("value"),
    ) if isinstance(state_raw, dict) else ElementState()

    return SemanticElementRef(
        role=raw.get("role") or "",
        name=raw.get("name") or "",
        bbox=bbox,
        xpath=raw.get("xpath") or raw.get("dataId") or "",
        css_selector=raw.get("cssSelector") or raw.get("css_selector") or "",
        state=state,
        tag_name=raw.get("tagName") or raw.get("tag_name") or "",
        id=raw.get("id"),
        class_list=raw.get("classList") or raw.get("class_list") or [],
    )


def _convert_browser_event(db_event) -> "BrowserEvent | None":
    """Convert a SQLAlchemy BrowserEvent to a typed Pydantic browser event.

    The DB stores browser events as JSON in the `message` field.  The recorder
    wraps each raw WebSocket message as ``{"message": <raw_event>}``.

    Handles both flat (content-script) and payload-wrapped message formats.

    Args:
        db_event: SQLAlchemy BrowserEvent instance.

    Returns:
        Typed browser event or None if parsing fails.
    """
    msg = db_event.message
    if not isinstance(msg, dict):
        return None

    # Unwrap the recorder's {"message": <raw>} wrapper
    inner = msg.get("message", msg)
    if not isinstance(inner, dict):
        return None

    # Support both flat (content-script) and payload-wrapped (browser_bridge) formats
    payload = inner.get("payload", inner)

    raw_type = payload.get("eventType", inner.get("eventType", ""))
    try:
        event_type = BrowserEventType(raw_type)
    except ValueError:
        return None

    timestamp = db_event.timestamp or 0
    url = payload.get("url", inner.get("url", ""))
    tab_id = inner.get("tabId", payload.get("tab_id", 0))

    try:
        if event_type == BrowserEventType.CLICK:
            elem = _parse_element_ref(payload.get("element"))
            if elem is None:
                return None
            return BrowserClickEvent(
                timestamp=timestamp,
                url=url,
                tab_id=tab_id,
                client_x=payload.get("clientX", 0),
                client_y=payload.get("clientY", 0),
                page_x=payload.get("pageX", payload.get("clientX", 0)),
                page_y=payload.get("pageY", payload.get("clientY", 0)),
                button=payload.get("button", 0),
                click_count=payload.get("clickCount", 1),
                element=elem,
            )
        elif event_type in (BrowserEventType.KEYDOWN, BrowserEventType.KEYUP):
            element = _parse_element_ref(payload.get("element"))
            return BrowserKeyEvent(
                timestamp=timestamp,
                type=event_type,
                url=url,
                tab_id=tab_id,
                key=payload.get("key", ""),
                code=payload.get("code", ""),
                key_code=payload.get("keyCode", 0),
                shift_key=payload.get("shiftKey", False),
                ctrl_key=payload.get("ctrlKey", False),
                alt_key=payload.get("altKey", False),
                meta_key=payload.get("metaKey", False),
                element=element,
            )
        elif event_type == BrowserEventType.SCROLL:
            return BrowserScrollEvent(
                timestamp=timestamp,
                url=url,
                tab_id=tab_id,
                scroll_x=payload.get("scrollX", 0),
                scroll_y=payload.get("scrollY", 0),
                delta_x=payload.get("deltaX", payload.get("scrollDeltaX", 0)),
                delta_y=payload.get("deltaY", payload.get("scrollDeltaY", 0)),
            )
        elif event_type == BrowserEventType.INPUT:
            elem = _parse_element_ref(payload.get("element"))
            if elem is None:
                return None
            return BrowserInputEvent(
                timestamp=timestamp,
                url=url,
                tab_id=tab_id,
                input_type=payload.get("inputType", ""),
                data=payload.get("data"),
                value=payload.get("value", ""),
                element=elem,
            )
        elif event_type == BrowserEventType.NAVIGATE:
            nav_type = payload.get("navigationType", "link")
            valid = [e.value for e in NavigationType]
            return BrowserNavigationEvent(
                timestamp=timestamp,
                url=url,
                tab_id=tab_id,
                previous_url=payload.get("previousUrl", ""),
                navigation_type=(
                    NavigationType(nav_type)
                    if nav_type in valid
                    else NavigationType.LINK
                ),
            )
        elif event_type == BrowserEventType.MOUSEMOVE:
            element = _parse_element_ref(payload.get("element"))
            return BrowserMouseMoveEvent(
                timestamp=timestamp,
                url=url,
                tab_id=tab_id,
                client_x=payload.get("clientX", 0),
                client_y=payload.get("clientY", 0),
                screen_x=payload.get("screenX", 0),
                screen_y=payload.get("screenY", 0),
                element=element,
            )
        elif event_type in (BrowserEventType.FOCUS, BrowserEventType.BLUR):
            elem = _parse_element_ref(payload.get("element"))
            if elem is None:
                return None
            return BrowserFocusEvent(
                timestamp=timestamp,
                type=event_type,
                url=url,
                tab_id=tab_id,
                element=elem,
            )
    except Exception as e:
        import logging
        logging.getLogger(__name__).debug("Failed to parse browser event: %s", e)
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

    @property
    def pixel_ratio(self) -> float:
        """Display pixel ratio (physical/logical), e.g. 2.0 for Retina.

        Defaults to 1.0 if not stored in the recording.
        """
        # Check if the Recording model has a pixel_ratio column
        ratio = getattr(self._recording, "pixel_ratio", None)
        if ratio is not None:
            return float(ratio)
        # Check the config JSON for pixel_ratio
        config = getattr(self._recording, "config", None)
        if isinstance(config, dict) and "pixel_ratio" in config:
            return float(config["pixel_ratio"])
        return 1.0

    @property
    def audio_start_time(self) -> float | None:
        """Start timestamp of the audio recording, or None if unavailable."""
        # Check the AudioInfo relationship for the timestamp
        audio_infos = getattr(self._recording, "audio_info", None)
        if audio_infos:
            first = audio_infos[0] if isinstance(audio_infos, list) else audio_infos
            ts = getattr(first, "timestamp", None)
            if ts is not None:
                return float(ts)
        return None

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

    def browser_events(self) -> list["BrowserEvent"]:
        """Get all browser events as typed Pydantic models.

        Parses the JSON message field from each stored BrowserEvent into
        the appropriate typed event (BrowserClickEvent, BrowserKeyEvent, etc.).

        Returns:
            List of typed browser events, ordered by timestamp.
        """
        events: list[BrowserEvent] = []
        for db_event in self._recording.browser_events:
            parsed = _convert_browser_event(db_event)
            if parsed is not None:
                events.append(parsed)
        return events

    @property
    def browser_event_count(self) -> int:
        """Number of browser events in this capture."""
        return len(self._recording.browser_events)

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

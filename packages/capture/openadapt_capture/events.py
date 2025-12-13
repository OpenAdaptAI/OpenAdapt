"""Event schemas for GUI interaction capture.

This module defines Pydantic models for all event types captured during
GUI interaction recording. Events are designed to closely follow OpenAdapt's
battle-tested implementation.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class EventType(str, Enum):
    """Event type identifiers."""

    # Mouse events (raw)
    MOUSE_MOVE = "mouse.move"
    MOUSE_DOWN = "mouse.down"
    MOUSE_UP = "mouse.up"
    MOUSE_SCROLL = "mouse.scroll"

    # Keyboard events (raw)
    KEY_DOWN = "key.down"
    KEY_UP = "key.up"

    # Screen events
    SCREEN_FRAME = "screen.frame"

    # Audio events
    AUDIO_CHUNK = "audio.chunk"

    # Derived events (from post-processing)
    MOUSE_CLICK = "mouse.click"
    MOUSE_SINGLECLICK = "mouse.singleclick"
    MOUSE_DOUBLECLICK = "mouse.doubleclick"
    MOUSE_DRAG = "mouse.drag"
    KEY_TYPE = "key.type"
    KEY_SHORTCUT = "key.shortcut"


class MouseButton(str, Enum):
    """Mouse button names."""

    LEFT = "left"
    RIGHT = "right"
    MIDDLE = "middle"


class BaseEvent(BaseModel):
    """Base class for all events.

    All events have a timestamp and type. This mirrors OpenAdapt's
    Event namedtuple: Event = namedtuple("Event", ("timestamp", "type", "data"))
    """

    timestamp: float = Field(description="Unix timestamp in seconds (float for sub-ms precision)")
    type: EventType = Field(description="Event type identifier")

    model_config = {"use_enum_values": True}


# =============================================================================
# Mouse Events
# =============================================================================


class MouseMoveEvent(BaseEvent):
    """Mouse cursor movement event.

    Corresponds to OpenAdapt's ActionEvent with name="move".
    """

    type: Literal[EventType.MOUSE_MOVE] = EventType.MOUSE_MOVE
    x: float = Field(description="Mouse X position in pixels")
    y: float = Field(description="Mouse Y position in pixels")


class MouseDownEvent(BaseEvent):
    """Mouse button press event.

    Corresponds to OpenAdapt's ActionEvent with name="click" and mouse_pressed=True.
    """

    type: Literal[EventType.MOUSE_DOWN] = EventType.MOUSE_DOWN
    x: float = Field(description="Mouse X position in pixels")
    y: float = Field(description="Mouse Y position in pixels")
    button: MouseButton = Field(description="Mouse button name")


class MouseUpEvent(BaseEvent):
    """Mouse button release event.

    Corresponds to OpenAdapt's ActionEvent with name="click" and mouse_pressed=False.
    """

    type: Literal[EventType.MOUSE_UP] = EventType.MOUSE_UP
    x: float = Field(description="Mouse X position in pixels")
    y: float = Field(description="Mouse Y position in pixels")
    button: MouseButton = Field(description="Mouse button name")


class MouseScrollEvent(BaseEvent):
    """Mouse scroll wheel event.

    Corresponds to OpenAdapt's ActionEvent with name="scroll".
    """

    type: Literal[EventType.MOUSE_SCROLL] = EventType.MOUSE_SCROLL
    x: float = Field(description="Mouse X position in pixels")
    y: float = Field(description="Mouse Y position in pixels")
    dx: float = Field(description="Horizontal scroll delta")
    dy: float = Field(description="Vertical scroll delta")


# =============================================================================
# Keyboard Events
# =============================================================================


class KeyDownEvent(BaseEvent):
    """Keyboard key press event.

    Corresponds to OpenAdapt's ActionEvent with name="press".
    """

    type: Literal[EventType.KEY_DOWN] = EventType.KEY_DOWN
    key_name: str | None = Field(default=None, description="Key name (e.g., 'shift', 'ctrl')")
    key_char: str | None = Field(default=None, description="Character typed (e.g., 'a', '1')")
    key_vk: str | None = Field(default=None, description="Virtual key code")
    canonical_key_name: str | None = Field(default=None, description="Canonical key name")
    canonical_key_char: str | None = Field(default=None, description="Canonical character")
    canonical_key_vk: str | None = Field(default=None, description="Canonical virtual key code")


class KeyUpEvent(BaseEvent):
    """Keyboard key release event.

    Corresponds to OpenAdapt's ActionEvent with name="release".
    """

    type: Literal[EventType.KEY_UP] = EventType.KEY_UP
    key_name: str | None = Field(default=None, description="Key name")
    key_char: str | None = Field(default=None, description="Character")
    key_vk: str | None = Field(default=None, description="Virtual key code")
    canonical_key_name: str | None = Field(default=None, description="Canonical key name")
    canonical_key_char: str | None = Field(default=None, description="Canonical character")
    canonical_key_vk: str | None = Field(default=None, description="Canonical virtual key code")


# =============================================================================
# Screen Events
# =============================================================================


class ScreenFrameEvent(BaseEvent):
    """Screen capture event.

    References a frame in the video file or a screenshot image.
    """

    type: Literal[EventType.SCREEN_FRAME] = EventType.SCREEN_FRAME
    video_timestamp: float | None = Field(
        default=None, description="Timestamp within video file (seconds)"
    )
    image_path: str | None = Field(default=None, description="Path to screenshot image file")
    width: int = Field(description="Frame width in pixels")
    height: int = Field(description="Frame height in pixels")


# =============================================================================
# Audio Events
# =============================================================================


class AudioChunkEvent(BaseEvent):
    """Audio capture event.

    References a segment of the audio recording.
    """

    type: Literal[EventType.AUDIO_CHUNK] = EventType.AUDIO_CHUNK
    start_time: float = Field(description="Start time within audio file (seconds)")
    end_time: float = Field(description="End time within audio file (seconds)")
    transcription: str | None = Field(default=None, description="Transcribed text for this chunk")


# =============================================================================
# Derived Events (from post-processing)
# =============================================================================


class MouseClickEvent(BaseEvent):
    """Combined mouse click event (down + up).

    Corresponds to OpenAdapt's ActionEvent with name="singleclick".
    Created by merge_consecutive_mouse_click_events().
    """

    type: Literal[EventType.MOUSE_SINGLECLICK] = EventType.MOUSE_SINGLECLICK
    x: float = Field(description="Mouse X position in pixels")
    y: float = Field(description="Mouse Y position in pixels")
    button: MouseButton = Field(description="Mouse button name")
    children: list[MouseDownEvent | MouseUpEvent] = Field(
        default_factory=list, description="Child events that were merged"
    )


class MouseDoubleClickEvent(BaseEvent):
    """Double click event.

    Corresponds to OpenAdapt's ActionEvent with name="doubleclick".
    Created by merge_consecutive_mouse_click_events().
    """

    type: Literal[EventType.MOUSE_DOUBLECLICK] = EventType.MOUSE_DOUBLECLICK
    x: float = Field(description="Mouse X position in pixels")
    y: float = Field(description="Mouse Y position in pixels")
    button: MouseButton = Field(description="Mouse button name")
    children: list[MouseDownEvent | MouseUpEvent] = Field(
        default_factory=list, description="Child events that were merged"
    )


class MouseDragEvent(BaseEvent):
    """Mouse drag event (down + moves + up).

    Uses x/y for start position and dx/dy for displacement (like MouseScrollEvent).
    End position can be computed as (x + dx, y + dy).
    """

    type: Literal[EventType.MOUSE_DRAG] = EventType.MOUSE_DRAG
    x: float = Field(description="Starting X position in pixels")
    y: float = Field(description="Starting Y position in pixels")
    dx: float = Field(description="Horizontal displacement (end_x - start_x)")
    dy: float = Field(description="Vertical displacement (end_y - start_y)")
    button: MouseButton = Field(description="Mouse button name")
    children: list[MouseDownEvent | MouseMoveEvent | MouseUpEvent] = Field(
        default_factory=list, description="Child events that were merged"
    )


class KeyTypeEvent(BaseEvent):
    """Sequence of typed characters.

    Corresponds to OpenAdapt's ActionEvent with name="type".
    Created by merge_consecutive_keyboard_events().
    """

    type: Literal[EventType.KEY_TYPE] = EventType.KEY_TYPE
    text: str = Field(description="The typed text")
    children: list[KeyDownEvent | KeyUpEvent] = Field(
        default_factory=list, description="Child events that were merged"
    )


# =============================================================================
# Union type for all events
# =============================================================================

ActionEvent = (
    MouseMoveEvent
    | MouseDownEvent
    | MouseUpEvent
    | MouseScrollEvent
    | KeyDownEvent
    | KeyUpEvent
    | MouseClickEvent
    | MouseDoubleClickEvent
    | MouseDragEvent
    | KeyTypeEvent
)

ScreenEvent = ScreenFrameEvent

AudioEvent = AudioChunkEvent

Event = ActionEvent | ScreenEvent | AudioEvent

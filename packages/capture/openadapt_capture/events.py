"""Event schemas for GUI interaction capture.

This module defines Pydantic models for all event types captured during
GUI interaction recording. Events are designed to closely follow OpenAdapt's
battle-tested implementation.
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openadapt_capture.structural import StructuralObservation
from openadapt_capture.window_capture import (
    WINDOW_CAPTURE_SCHEMA_VERSION,
    window_capture_evidence_sha256,
    window_geometry_epoch_sha256,
)


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
    source_ordinal: int | None = Field(
        default=None,
        ge=1,
        description="Exact 1-based position in the ordered native source journal",
    )
    type: EventType = Field(description="Event type identifier")

    model_config = {"use_enum_values": True}


class WindowCaptureStateV2(BaseModel):
    """Exact native geometry retained with one atomic screen frame."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["openadapt.capture.window-scoped/v2"]
    window_capture: Literal[True]
    window_id: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    pid: int = Field(gt=0)
    process_start_time: float = Field(gt=0)
    coordinate_source: str = Field(min_length=1)
    capture_source: str = Field(default="platform-window-image", min_length=1)
    visibility_independent: bool = False
    frame_status: Literal["complete", "idle"] | None = None
    frame_display_time: int | None = Field(default=None, ge=0)
    pixel_display_time: int | None = Field(default=None, ge=0)
    stream_generation: int | None = Field(default=None, ge=1)
    stream_sequence: int | None = Field(default=None, ge=1)
    capture_evidence_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    geometry_generation: int = Field(ge=1)
    geometry_epoch_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    display_topology_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bounds: tuple[float, float, float, float]
    scale: float = Field(gt=0)
    scale_x: float = Field(gt=0)
    scale_y: float = Field(gt=0)
    viewport: tuple[int, int]
    source_viewport: tuple[int, int]
    content_rect: tuple[int, int, int, int]
    fit_scale: float = Field(gt=0)
    on_screen: bool

    @model_validator(mode="after")
    def _closed_geometry(self) -> "WindowCaptureStateV2":
        if self.schema_version != WINDOW_CAPTURE_SCHEMA_VERSION:
            raise ValueError("unsupported window-capture schema version")
        numeric = (
            self.process_start_time,
            *self.bounds,
            self.scale,
            self.scale_x,
            self.scale_y,
            self.fit_scale,
        )
        if not all(math.isfinite(float(value)) for value in numeric):
            raise ValueError("window capture geometry must be finite")
        if self.bounds[2] <= 0 or self.bounds[3] <= 0:
            raise ValueError("window capture bounds must have positive dimensions")
        if not self.on_screen:
            if not self.visibility_independent:
                raise ValueError(
                    "an off-screen window requires a visibility-independent provider"
                )
            if self.capture_source not in {
                "macos-screencapturekit",
                "macos-screencapturekit-stream",
                "macos-screencapture-utility",
            }:
                raise ValueError(
                    "an off-screen window requires a proven exact-window capture source"
                )
        if self.capture_source == "macos-screencapturekit-stream":
            if (
                self.frame_status is None
                or self.frame_display_time is None
                or self.pixel_display_time is None
                or self.stream_generation is None
                or self.stream_sequence is None
                or self.capture_evidence_sha256 is None
            ):
                raise ValueError(
                    "a ScreenCaptureKit stream frame requires retained frame evidence"
                )
            if self.pixel_display_time > self.frame_display_time:
                raise ValueError(
                    "ScreenCaptureKit pixel time cannot follow its frame proof time"
                )
            if (
                self.frame_status == "complete"
                and self.pixel_display_time != self.frame_display_time
            ):
                raise ValueError(
                    "a complete ScreenCaptureKit frame must bind its pixel time"
                )
        elif any(
            value is not None
            for value in (
                self.frame_status,
                self.frame_display_time,
                self.pixel_display_time,
                self.stream_generation,
                self.stream_sequence,
            )
        ):
            raise ValueError(
                "non-stream capture sources cannot retain ScreenCaptureKit frame evidence"
            )
        if any(value <= 0 for value in (*self.viewport, *self.source_viewport)):
            raise ValueError("window capture viewports must be positive")
        left, top, width, height = self.content_rect
        if (
            left < 0
            or top < 0
            or width <= 0
            or height <= 0
            or left + width > self.viewport[0]
            or top + height > self.viewport[1]
        ):
            raise ValueError("window capture content rectangle is outside its viewport")
        expected_fit = min(
            self.viewport[0] / self.source_viewport[0],
            self.viewport[1] / self.source_viewport[1],
        )
        expected_width = max(
            1,
            min(self.viewport[0], round(self.source_viewport[0] * expected_fit)),
        )
        expected_height = max(
            1,
            min(self.viewport[1], round(self.source_viewport[1] * expected_fit)),
        )
        expected_rect = (
            (self.viewport[0] - expected_width) // 2,
            (self.viewport[1] - expected_height) // 2,
            expected_width,
            expected_height,
        )
        if not math.isclose(self.fit_scale, expected_fit) or self.content_rect != expected_rect:
            raise ValueError("window capture normalization differs from its viewports")
        expected_scale_x = width / self.bounds[2]
        expected_scale_y = height / self.bounds[3]
        if not math.isclose(self.scale_x, expected_scale_x) or not math.isclose(
            self.scale_y,
            expected_scale_y,
        ):
            raise ValueError("window capture axis scales differ from its content geometry")
        if not math.isclose(self.scale, self.scale_x):
            raise ValueError("legacy window scale differs from exact x scale")
        if self.geometry_epoch_sha256 != window_geometry_epoch_sha256(
            self.model_dump(mode="json", exclude={"geometry_epoch_sha256"})
        ):
            raise ValueError("window geometry epoch digest is invalid")
        if (
            self.capture_evidence_sha256 is not None
            and self.capture_evidence_sha256
            != window_capture_evidence_sha256(
                self.model_dump(
                    mode="json",
                    exclude={"capture_evidence_sha256"},
                )
            )
        ):
            raise ValueError("window capture evidence digest is invalid")
        return self


class CapturedWindowEvent(BaseModel):
    """Public validated view of one stored WindowEvent row."""

    model_config = ConfigDict(extra="forbid")

    timestamp: float
    source_ordinal: int | None = Field(default=None, ge=1)
    title: str | None = None
    left: int | None = None
    top: int | None = None
    width: int | None = None
    height: int | None = None
    window_id: str | None = None
    state: dict[str, Any]

    @property
    def window_capture_v2(self) -> WindowCaptureStateV2 | None:
        if self.state.get("schema_version") != WINDOW_CAPTURE_SCHEMA_VERSION:
            return None
        return WindowCaptureStateV2.model_validate(self.state)


class ActionBaseEvent(BaseEvent):
    """Base event for native actions with optional structural evidence."""

    structural_observation: StructuralObservation | None = Field(
        default=None,
        description="Versioned accessibility evidence observed at action time",
    )
    screenshot_timestamp: float | None = Field(
        default=None,
        description=(
            "Exact capture timestamp of the retained screen frame this action "
            "is bound to. Extract that exact frame instead of guessing a "
            "nearest one."
        ),
    )
    screenshot_source_ordinal: int | None = Field(
        default=None,
        ge=1,
        description="Source journal ordinal of the exact retained screen frame",
    )
    after_screenshot_timestamp: float | None = Field(
        default=None,
        description="Exact timestamp of the first retained frame after this action",
    )
    after_screenshot_source_ordinal: int | None = Field(
        default=None,
        ge=1,
        description="Source journal ordinal of the exact retained after frame",
    )
    window_event_timestamp: float | None = Field(
        default=None,
        description="Exact WindowEvent timestamp paired with the bound frame",
    )
    window_event_source_ordinal: int | None = Field(
        default=None,
        ge=1,
        description="Source journal ordinal of the geometry paired with the frame",
    )
    after_window_event_timestamp: float | None = Field(
        default=None,
        description="Exact WindowEvent timestamp paired with the retained after frame",
    )
    after_window_event_source_ordinal: int | None = Field(
        default=None,
        ge=1,
        description="Source journal ordinal of geometry paired with the after frame",
    )
    window_geometry_generation: int | None = Field(
        default=None,
        ge=1,
        description="Exact native geometry generation bound to this action",
    )
    after_window_geometry_generation: int | None = Field(
        default=None,
        ge=1,
        description="Exact native geometry generation paired with the after frame",
    )


# =============================================================================
# Mouse Events
# =============================================================================


class MouseMoveEvent(ActionBaseEvent):
    """Mouse cursor movement event.

    Corresponds to OpenAdapt's ActionEvent with name="move".
    """

    type: Literal[EventType.MOUSE_MOVE] = EventType.MOUSE_MOVE
    x: float = Field(description="Mouse X position in pixels")
    y: float = Field(description="Mouse Y position in pixels")


class MouseDownEvent(ActionBaseEvent):
    """Mouse button press event.

    Corresponds to OpenAdapt's ActionEvent with name="click" and mouse_pressed=True.
    """

    type: Literal[EventType.MOUSE_DOWN] = EventType.MOUSE_DOWN
    x: float = Field(description="Mouse X position in pixels")
    y: float = Field(description="Mouse Y position in pixels")
    button: str = Field(description="Native mouse button name")


class MouseUpEvent(ActionBaseEvent):
    """Mouse button release event.

    Corresponds to OpenAdapt's ActionEvent with name="click" and mouse_pressed=False.
    """

    type: Literal[EventType.MOUSE_UP] = EventType.MOUSE_UP
    x: float = Field(description="Mouse X position in pixels")
    y: float = Field(description="Mouse Y position in pixels")
    button: str = Field(description="Native mouse button name")


class MouseScrollEvent(ActionBaseEvent):
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


class KeyDownEvent(ActionBaseEvent):
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


class KeyUpEvent(ActionBaseEvent):
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


class MouseClickEvent(ActionBaseEvent):
    """Combined mouse click event (down + up).

    Corresponds to OpenAdapt's ActionEvent with name="singleclick".
    Created by merge_consecutive_mouse_click_events().
    """

    type: Literal[EventType.MOUSE_SINGLECLICK] = EventType.MOUSE_SINGLECLICK
    x: float = Field(description="Mouse X position in pixels")
    y: float = Field(description="Mouse Y position in pixels")
    button: str = Field(description="Native mouse button name")
    children: list[MouseDownEvent | MouseUpEvent] = Field(
        default_factory=list, description="Child events that were merged"
    )


class MouseDoubleClickEvent(ActionBaseEvent):
    """Double click event.

    Corresponds to OpenAdapt's ActionEvent with name="doubleclick".
    Created by merge_consecutive_mouse_click_events().
    """

    type: Literal[EventType.MOUSE_DOUBLECLICK] = EventType.MOUSE_DOUBLECLICK
    x: float = Field(description="Mouse X position in pixels")
    y: float = Field(description="Mouse Y position in pixels")
    button: str = Field(description="Native mouse button name")
    children: list[MouseDownEvent | MouseUpEvent] = Field(
        default_factory=list, description="Child events that were merged"
    )


class MouseDragEvent(ActionBaseEvent):
    """Mouse drag event (down + moves + up).

    Uses x/y for start position and dx/dy for displacement (like MouseScrollEvent).
    End position can be computed as (x + dx, y + dy).
    """

    type: Literal[EventType.MOUSE_DRAG] = EventType.MOUSE_DRAG
    x: float = Field(description="Starting X position in pixels")
    y: float = Field(description="Starting Y position in pixels")
    dx: float = Field(description="Horizontal displacement (end_x - start_x)")
    dy: float = Field(description="Vertical displacement (end_y - start_y)")
    button: str = Field(description="Native mouse button name")
    children: list[MouseDownEvent | MouseMoveEvent | MouseUpEvent] = Field(
        default_factory=list, description="Child events that were merged"
    )


class KeyTypeEvent(ActionBaseEvent):
    """Sequence of typed characters.

    Corresponds to OpenAdapt's ActionEvent with name="type".
    Created by merge_consecutive_keyboard_events().
    """

    type: Literal[EventType.KEY_TYPE] = EventType.KEY_TYPE
    text: str = Field(description="The typed text")
    children: list[KeyDownEvent | KeyUpEvent] = Field(
        default_factory=list, description="Child events that were merged"
    )


class KeyShortcutEvent(ActionBaseEvent):
    """A modifier chord demonstrated by the operator.

    ``modifiers`` is canonicalized by the processing pipeline (``ctrl``,
    ``alt``, ``shift``, ``meta``). ``key`` is the single non-modifier trigger.
    The raw down/up sequence remains in ``children`` so conversion never has to
    reconstruct the demonstrated chord from display text.
    """

    type: Literal[EventType.KEY_SHORTCUT] = EventType.KEY_SHORTCUT
    modifiers: list[str] = Field(
        min_length=1,
        description="Canonical modifier keys held for the chord",
    )
    key: str = Field(min_length=1, description="Non-modifier trigger key")
    children: list[KeyDownEvent | KeyUpEvent] = Field(
        default_factory=list,
        description="Raw keyboard events retained for provenance",
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
    | KeyShortcutEvent
)

ScreenEvent = ScreenFrameEvent

AudioEvent = AudioChunkEvent

Event = ActionEvent | ScreenEvent | AudioEvent

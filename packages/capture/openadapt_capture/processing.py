"""Event processing pipeline for merging raw events into higher-level actions.

This module ports OpenAdapt's event processing functions to work with
the openadapt-capture Pydantic event models.
"""

from __future__ import annotations

from typing import Any, TypeVar

from openadapt_capture.events import (
    ActionEvent,
    Event,
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
)

# Type variable for event types
E = TypeVar("E", bound=Event)

# =============================================================================
# Configuration
# =============================================================================

# Thresholds for event merging
MOUSE_MOVE_MERGE_DISTANCE_THRESHOLD = 1  # pixels
MOUSE_MOVE_MERGE_MIN_IDX_DELTA = 5  # minimum events between groups
DOUBLE_CLICK_INTERVAL_SECONDS = 0.5  # default double-click interval
DOUBLE_CLICK_DISTANCE_PIXELS = 5.0  # default double-click distance
KEY_TYPE_MERGE_INTERVAL_SECONDS = 0.5  # merge adjacent KeyTypeEvents within this interval


# =============================================================================
# Event Processing Functions
# =============================================================================


def process_events(
    events: list[ActionEvent],
    double_click_interval: float = DOUBLE_CLICK_INTERVAL_SECONDS,
    double_click_distance: float = DOUBLE_CLICK_DISTANCE_PIXELS,
) -> list[ActionEvent]:
    """Process raw events through the full pipeline.

    Applies all processing functions in sequence:
    1. Remove invalid keyboard events
    2. Remove redundant mouse move events
    3. Merge consecutive keyboard events → KeyTypeEvent
    4. Merge consecutive mouse move events
    5. Merge consecutive mouse scroll events
    6. Merge consecutive mouse click events → MouseClickEvent/MouseDoubleClickEvent
    7. Detect drag events → MouseDragEvent

    Args:
        events: Raw action events.
        double_click_interval: Time threshold for double-click detection (seconds).
        double_click_distance: Distance threshold for double-click detection (pixels).

    Returns:
        Processed events with merged actions.
    """
    # Apply processing pipeline
    events = remove_invalid_keyboard_events(events)
    events = remove_redundant_mouse_move_events(events)
    events = merge_consecutive_keyboard_events(events)
    events = merge_consecutive_mouse_move_events(events)
    events = merge_consecutive_mouse_scroll_events(events)
    events = merge_consecutive_mouse_click_events(
        events,
        double_click_interval=double_click_interval,
        double_click_distance=double_click_distance,
    )
    events = detect_drag_events(events)

    return events


def remove_invalid_keyboard_events(events: list[ActionEvent]) -> list[ActionEvent]:
    """Remove invalid keyboard events (e.g., invalid key codes).

    Args:
        events: List of events.

    Returns:
        Filtered list of events.
    """
    valid_events = []
    for event in events:
        if isinstance(event, (KeyDownEvent, KeyUpEvent)):
            # Filter out events with no key information
            if not any([event.key_name, event.key_char, event.key_vk]):
                continue
        valid_events.append(event)
    return valid_events


def remove_redundant_mouse_move_events(events: list[ActionEvent]) -> list[ActionEvent]:
    """Remove mouse move events that don't change position.

    Args:
        events: List of events.

    Returns:
        Filtered list with redundant moves removed.
    """

    def is_same_position(e1: MouseMoveEvent, e2: MouseMoveEvent) -> bool:
        return e1.x == e2.x and e1.y == e2.y

    result = []
    prev_move: MouseMoveEvent | None = None

    for event in events:
        if isinstance(event, MouseMoveEvent):
            if prev_move is not None and is_same_position(prev_move, event):
                # Skip redundant move
                continue
            prev_move = event
        result.append(event)

    return result


def merge_consecutive_keyboard_events(events: list[ActionEvent]) -> list[ActionEvent]:
    """Merge consecutive keyboard events into KeyTypeEvent.

    Groups key press/release sequences into typed text.

    Args:
        events: List of events.

    Returns:
        Events with keyboard sequences merged into KeyTypeEvent.
    """
    result = []
    keyboard_buffer: list[KeyDownEvent | KeyUpEvent] = []
    pressed_keys: set[str] = set()

    def flush_buffer() -> None:
        """Convert buffer to KeyTypeEvent and add to result."""
        if not keyboard_buffer:
            return

        # Extract typed characters from press events
        chars = []
        for event in keyboard_buffer:
            if isinstance(event, KeyDownEvent) and event.key_char:
                chars.append(event.key_char)

        text = "".join(chars)
        if text or keyboard_buffer:
            first_event = keyboard_buffer[0]
            type_event = KeyTypeEvent(
                timestamp=first_event.timestamp,
                text=text,
                children=list(keyboard_buffer),
            )
            result.append(type_event)

        keyboard_buffer.clear()
        pressed_keys.clear()

    for event in events:
        if isinstance(event, KeyDownEvent):
            key_id = event.key_name or event.key_char or event.key_vk or ""
            pressed_keys.add(key_id)
            keyboard_buffer.append(event)
        elif isinstance(event, KeyUpEvent):
            key_id = event.key_name or event.key_char or event.key_vk or ""
            pressed_keys.discard(key_id)
            keyboard_buffer.append(event)

            # If no keys pressed, flush the buffer
            if not pressed_keys:
                flush_buffer()
        else:
            # Non-keyboard event: flush buffer and add event
            flush_buffer()
            result.append(event)

    # Flush any remaining keyboard events
    flush_buffer()

    return result


def merge_consecutive_mouse_move_events(events: list[ActionEvent]) -> list[ActionEvent]:
    """Merge consecutive mouse move events.

    Reduces move event density while preserving important positions.

    Args:
        events: List of events.

    Returns:
        Events with mouse moves merged.
    """
    result = []
    move_buffer: list[MouseMoveEvent] = []

    def flush_buffer() -> None:
        """Merge move buffer and add to result."""
        if not move_buffer:
            return

        if len(move_buffer) == 1:
            result.append(move_buffer[0])
        else:
            # Create merged move event with final position
            first = move_buffer[0]
            last = move_buffer[-1]
            merged = MouseMoveEvent(
                timestamp=first.timestamp,
                x=last.x,
                y=last.y,
            )
            result.append(merged)

        move_buffer.clear()

    for event in events:
        if isinstance(event, MouseMoveEvent):
            move_buffer.append(event)
        else:
            flush_buffer()
            result.append(event)

    flush_buffer()

    return result


def merge_consecutive_mouse_scroll_events(events: list[ActionEvent]) -> list[ActionEvent]:
    """Merge consecutive mouse scroll events.

    Combines scroll deltas from consecutive scroll events.

    Args:
        events: List of events.

    Returns:
        Events with scrolls merged.
    """
    result = []
    scroll_buffer: list[MouseScrollEvent] = []

    def flush_buffer() -> None:
        """Merge scroll buffer and add to result."""
        if not scroll_buffer:
            return

        if len(scroll_buffer) == 1:
            result.append(scroll_buffer[0])
        else:
            # Sum scroll deltas
            first = scroll_buffer[0]
            total_dx = sum(e.dx for e in scroll_buffer)
            total_dy = sum(e.dy for e in scroll_buffer)
            merged = MouseScrollEvent(
                timestamp=first.timestamp,
                x=first.x,
                y=first.y,
                dx=total_dx,
                dy=total_dy,
            )
            result.append(merged)

        scroll_buffer.clear()

    for event in events:
        if isinstance(event, MouseScrollEvent):
            scroll_buffer.append(event)
        else:
            flush_buffer()
            result.append(event)

    flush_buffer()

    return result


def merge_consecutive_mouse_click_events(
    events: list[ActionEvent],
    double_click_interval: float = DOUBLE_CLICK_INTERVAL_SECONDS,
    double_click_distance: float = DOUBLE_CLICK_DISTANCE_PIXELS,
    drag_distance_threshold: float = 10.0,
) -> list[ActionEvent]:
    """Merge mouse down/up events into click events.

    Uses timestamp mapping approach (like OpenAdapt) to match down/up events
    even when other events occur between them.

    Detects single clicks and double clicks based on timing and distance.
    Does NOT merge if the down→up distance exceeds drag_distance_threshold
    (those will be handled by detect_drag_events).

    Args:
        events: List of events.
        double_click_interval: Time threshold for double-click (seconds).
        double_click_distance: Distance threshold for double-click (pixels).
        drag_distance_threshold: If down→up distance exceeds this, don't merge (pixels).

    Returns:
        Events with clicks merged into MouseClickEvent/MouseDoubleClickEvent.
    """
    def calculate_distance(x1: float, y1: float, x2: float, y2: float) -> float:
        return ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5

    # Build timestamp mappings for down -> up and down -> next_down (for double-click)
    down_events: list[MouseDownEvent] = []
    down_to_up: dict[float, MouseUpEvent] = {}
    down_to_next_down: dict[float, MouseDownEvent] = {}

    # First pass: collect all down events and map to their up events
    prev_down: MouseDownEvent | None = None
    for event in events:
        if isinstance(event, MouseDownEvent):
            down_events.append(event)
            # Check if this could be second click of a double-click
            if prev_down is not None:
                dt = event.timestamp - prev_down.timestamp
                dx = abs(event.x - prev_down.x)
                dy = abs(event.y - prev_down.y)
                if (
                    dt <= double_click_interval
                    and dx <= double_click_distance
                    and dy <= double_click_distance
                    and event.button == prev_down.button
                ):
                    down_to_next_down[prev_down.timestamp] = event
            prev_down = event
        elif isinstance(event, MouseUpEvent):
            # Find the most recent unmatched down with same button
            for down in reversed(down_events):
                if down.button == event.button and down.timestamp not in down_to_up:
                    # Only map if distance is small enough (not a drag)
                    dist = calculate_distance(down.x, down.y, event.x, event.y)
                    if dist <= drag_distance_threshold:
                        down_to_up[down.timestamp] = event
                    break

    # Second pass: generate merged events
    result = []
    skip_timestamps: set[float] = set()

    for event in events:
        if event.timestamp in skip_timestamps:
            continue

        if isinstance(event, MouseDownEvent):
            down = event

            if down.timestamp in down_to_up:
                up = down_to_up[down.timestamp]

                # Check if this is the start of a double-click
                if down.timestamp in down_to_next_down:
                    next_down = down_to_next_down[down.timestamp]
                    if next_down.timestamp in down_to_up:
                        next_up = down_to_up[next_down.timestamp]

                        # Create double-click
                        double_click = MouseDoubleClickEvent(
                            timestamp=down.timestamp,
                            x=down.x,
                            y=down.y,
                            button=down.button,
                            children=[down, up, next_down, next_up],
                        )
                        result.append(double_click)
                        skip_timestamps.add(up.timestamp)
                        skip_timestamps.add(next_down.timestamp)
                        skip_timestamps.add(next_up.timestamp)
                        continue

                # Create single click
                single_click = MouseClickEvent(
                    timestamp=down.timestamp,
                    x=down.x,
                    y=down.y,
                    button=down.button,
                    children=[down, up],
                )
                result.append(single_click)
                skip_timestamps.add(up.timestamp)
            else:
                # Unmatched down event
                result.append(event)

        elif isinstance(event, MouseUpEvent):
            # Already handled via down event mapping, or orphaned
            result.append(event)
        else:
            result.append(event)

    return result


def detect_drag_events(
    events: list[ActionEvent],
    min_distance: float = 10.0,
) -> list[ActionEvent]:
    """Detect drag events from mouse down → move → up sequences.

    A drag is detected when:
    1. Mouse down occurs
    2. One or more mouse moves occur while button is held
    3. Mouse up occurs at a different position than mouse down

    Args:
        events: List of events (should already have clicks merged).
        min_distance: Minimum drag distance in pixels.

    Returns:
        Events with drags detected as MouseDragEvent.
    """
    result = []
    drag_state: dict[str, Any] | None = None  # {down: MouseDownEvent, moves: [...]}

    def calculate_distance(x1: float, y1: float, x2: float, y2: float) -> float:
        return ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5

    for event in events:
        if isinstance(event, MouseDownEvent):
            # Start potential drag
            drag_state = {"down": event, "moves": []}
        elif isinstance(event, MouseMoveEvent) and drag_state is not None:
            # Track moves during potential drag
            drag_state["moves"].append(event)
        elif isinstance(event, MouseUpEvent) and drag_state is not None:
            down_event: MouseDownEvent = drag_state["down"]
            moves: list[MouseMoveEvent] = drag_state["moves"]

            if down_event.button == event.button:
                distance = calculate_distance(down_event.x, down_event.y, event.x, event.y)

                if distance >= min_distance and moves:
                    # Create drag event with dx/dy displacement
                    drag = MouseDragEvent(
                        timestamp=down_event.timestamp,
                        x=down_event.x,
                        y=down_event.y,
                        dx=event.x - down_event.x,
                        dy=event.y - down_event.y,
                        button=down_event.button,
                        children=[down_event] + moves + [event],
                    )
                    result.append(drag)
                else:
                    # Not a drag, output as separate events
                    result.append(down_event)
                    result.extend(moves)
                    result.append(event)
            else:
                # Button mismatch
                result.append(down_event)
                result.extend(moves)
                result.append(event)

            drag_state = None
        elif isinstance(event, (MouseClickEvent, MouseDoubleClickEvent)):
            # Already merged click, just pass through
            if drag_state is not None:
                # Flush incomplete drag state
                result.append(drag_state["down"])
                result.extend(drag_state["moves"])
                drag_state = None
            result.append(event)
        else:
            # Other event types
            if drag_state is not None:
                # Interrupted drag sequence
                result.append(drag_state["down"])
                result.extend(drag_state["moves"])
                drag_state = None
            result.append(event)

    # Handle any remaining drag state
    if drag_state is not None:
        result.append(drag_state["down"])
        result.extend(drag_state["moves"])

    return result


# =============================================================================
# Utility Functions
# =============================================================================


def get_action_events(events: list[Event]) -> list[ActionEvent]:
    """Filter to only action events (mouse, keyboard).

    Args:
        events: All events.

    Returns:
        Only action events.
    """
    action_types = (
        MouseMoveEvent,
        MouseDownEvent,
        MouseUpEvent,
        MouseScrollEvent,
        KeyDownEvent,
        KeyUpEvent,
        MouseClickEvent,
        MouseDoubleClickEvent,
        MouseDragEvent,
        KeyTypeEvent,
    )
    return [e for e in events if isinstance(e, action_types)]


def get_screen_events(events: list[Event]) -> list[Event]:
    """Filter to only screen events.

    Args:
        events: All events.

    Returns:
        Only screen frame events.
    """
    from openadapt_capture.events import ScreenFrameEvent

    return [e for e in events if isinstance(e, ScreenFrameEvent)]


def get_audio_events(events: list[Event]) -> list[Event]:
    """Filter to only audio events.

    Args:
        events: All events.

    Returns:
        Only audio chunk events.
    """
    from openadapt_capture.events import AudioChunkEvent

    return [e for e in events if isinstance(e, AudioChunkEvent)]

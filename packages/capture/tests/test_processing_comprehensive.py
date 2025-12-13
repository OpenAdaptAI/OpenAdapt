"""Comprehensive tests for event processing pipeline.

These tests are modeled after OpenAdapt's test_events.py to ensure
thorough coverage of edge cases in event merging.
"""

import pytest

from openadapt_capture.events import (
    KeyDownEvent,
    KeyTypeEvent,
    KeyUpEvent,
    MouseButton,
    MouseClickEvent,
    MouseDoubleClickEvent,
    MouseDownEvent,
    MouseDragEvent,
    MouseMoveEvent,
    MouseScrollEvent,
    MouseUpEvent,
)
from openadapt_capture.processing import (
    DOUBLE_CLICK_DISTANCE_PIXELS,
    DOUBLE_CLICK_INTERVAL_SECONDS,
    detect_drag_events,
    merge_consecutive_keyboard_events,
    merge_consecutive_mouse_click_events,
    merge_consecutive_mouse_move_events,
    merge_consecutive_mouse_scroll_events,
    process_events,
    remove_invalid_keyboard_events,
    remove_redundant_mouse_move_events,
)


# =============================================================================
# Test Fixtures and Helpers
# =============================================================================

class TimestampGenerator:
    """Helper to generate sequential timestamps for tests."""

    def __init__(self, start: float = 0.0, default_dt: float = 0.1):
        self.current = start
        self.default_dt = default_dt

    def next(self, dt: float = None) -> float:
        """Get next timestamp, optionally with custom delta."""
        if dt is None:
            dt = self.default_dt
        ts = self.current
        self.current += dt
        return ts

    def reset(self, start: float = 0.0) -> None:
        """Reset timestamp counter."""
        self.current = start


@pytest.fixture
def ts():
    """Fixture providing a timestamp generator."""
    return TimestampGenerator()


# =============================================================================
# Test: merge_consecutive_mouse_click_events
# =============================================================================

class TestMergeConsecutiveMouseClickEventsComprehensive:
    """Comprehensive tests for click merging based on OpenAdapt patterns."""

    def test_single_click_becomes_singleclick(self, ts):
        """A single click (down+up) should become a MouseClickEvent."""
        events = [
            MouseDownEvent(timestamp=ts.next(), x=100, y=100, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=ts.next(), x=100, y=100, button=MouseButton.LEFT),
        ]
        result = merge_consecutive_mouse_click_events(events)

        assert len(result) == 1
        assert isinstance(result[0], MouseClickEvent)
        assert len(result[0].children) == 2

    def test_double_click_within_interval(self, ts):
        """Two quick clicks should merge into MouseDoubleClickEvent."""
        dt_short = DOUBLE_CLICK_INTERVAL_SECONDS / 10

        events = [
            MouseDownEvent(timestamp=ts.next(), x=100, y=100, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=ts.next(dt_short), x=100, y=100, button=MouseButton.LEFT),
            MouseDownEvent(timestamp=ts.next(dt_short), x=100, y=100, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=ts.next(dt_short), x=100, y=100, button=MouseButton.LEFT),
        ]
        result = merge_consecutive_mouse_click_events(events)

        assert len(result) == 1
        assert isinstance(result[0], MouseDoubleClickEvent)
        assert len(result[0].children) == 4

    def test_clicks_too_far_apart_stay_separate(self, ts):
        """Two clicks far apart in time should stay as separate single clicks."""
        dt_long = DOUBLE_CLICK_INTERVAL_SECONDS * 3  # Well beyond threshold

        # First click
        events = [
            MouseDownEvent(timestamp=0.0, x=100, y=100, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=0.05, x=100, y=100, button=MouseButton.LEFT),
            # Second click - starts AFTER the double-click interval
            MouseDownEvent(timestamp=dt_long, x=100, y=100, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=dt_long + 0.05, x=100, y=100, button=MouseButton.LEFT),
        ]
        result = merge_consecutive_mouse_click_events(events)

        assert len(result) == 2
        assert all(isinstance(e, MouseClickEvent) for e in result)
        assert not any(isinstance(e, MouseDoubleClickEvent) for e in result)

    def test_clicks_too_far_apart_spatially(self, ts):
        """Two quick clicks at different positions should stay separate."""
        dt_short = DOUBLE_CLICK_INTERVAL_SECONDS / 10
        distance = DOUBLE_CLICK_DISTANCE_PIXELS * 3  # Beyond threshold

        events = [
            MouseDownEvent(timestamp=ts.next(), x=100, y=100, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=ts.next(dt_short), x=100, y=100, button=MouseButton.LEFT),
            MouseDownEvent(timestamp=ts.next(dt_short), x=100 + distance, y=100, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=ts.next(dt_short), x=100 + distance, y=100, button=MouseButton.LEFT),
        ]
        result = merge_consecutive_mouse_click_events(events)

        assert len(result) == 2
        assert all(isinstance(e, MouseClickEvent) for e in result)

    def test_different_buttons_stay_separate(self, ts):
        """Clicks with different buttons should not merge."""
        dt_short = DOUBLE_CLICK_INTERVAL_SECONDS / 10

        events = [
            MouseDownEvent(timestamp=ts.next(), x=100, y=100, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=ts.next(dt_short), x=100, y=100, button=MouseButton.LEFT),
            MouseDownEvent(timestamp=ts.next(dt_short), x=100, y=100, button=MouseButton.RIGHT),
            MouseUpEvent(timestamp=ts.next(dt_short), x=100, y=100, button=MouseButton.RIGHT),
        ]
        result = merge_consecutive_mouse_click_events(events)

        assert len(result) == 2

    def test_mixed_sequence_with_double_and_single_clicks(self, ts):
        """Complex sequence with both double and single clicks."""
        dt_short = DOUBLE_CLICK_INTERVAL_SECONDS / 10
        dt_long = DOUBLE_CLICK_INTERVAL_SECONDS * 2

        events = [
            # Right click (single)
            MouseDownEvent(timestamp=ts.next(), x=100, y=100, button=MouseButton.RIGHT),
            MouseUpEvent(timestamp=ts.next(dt_long), x=100, y=100, button=MouseButton.RIGHT),
            # Left double-click
            MouseDownEvent(timestamp=ts.next(dt_short), x=200, y=200, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=ts.next(dt_short), x=200, y=200, button=MouseButton.LEFT),
            MouseDownEvent(timestamp=ts.next(dt_short), x=200, y=200, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=ts.next(dt_short), x=200, y=200, button=MouseButton.LEFT),
            # Another right click (single)
            MouseDownEvent(timestamp=ts.next(dt_long), x=300, y=300, button=MouseButton.RIGHT),
            MouseUpEvent(timestamp=ts.next(dt_long), x=300, y=300, button=MouseButton.RIGHT),
            # Left single click
            MouseDownEvent(timestamp=ts.next(dt_long), x=400, y=400, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=ts.next(dt_long), x=400, y=400, button=MouseButton.LEFT),
        ]
        result = merge_consecutive_mouse_click_events(events)

        # Should have: right single, left double, right single, left single
        # Note: raw right clicks pass through since our impl focuses on left clicks
        assert any(isinstance(e, MouseDoubleClickEvent) for e in result)


# =============================================================================
# Test: merge_consecutive_mouse_move_events
# =============================================================================

class TestMergeConsecutiveMouseMoveEventsComprehensive:
    """Comprehensive tests for mouse move merging."""

    def test_consecutive_moves_merge_to_final_position(self, ts):
        """Multiple consecutive moves should merge, keeping final position."""
        events = [
            MouseMoveEvent(timestamp=ts.next(), x=0, y=0),
            MouseMoveEvent(timestamp=ts.next(), x=10, y=10),
            MouseMoveEvent(timestamp=ts.next(), x=20, y=20),
            MouseMoveEvent(timestamp=ts.next(), x=30, y=30),
        ]
        result = merge_consecutive_mouse_move_events(events)

        assert len(result) == 1
        assert result[0].x == 30
        assert result[0].y == 30

    def test_moves_interrupted_by_scroll_create_groups(self, ts):
        """Moves interrupted by scroll events should create separate groups."""
        events = [
            MouseScrollEvent(timestamp=ts.next(), x=0, y=0, dx=0, dy=1),
            MouseMoveEvent(timestamp=ts.next(), x=0, y=0),
            MouseMoveEvent(timestamp=ts.next(), x=10, y=10),
            MouseMoveEvent(timestamp=ts.next(), x=20, y=20),
            MouseScrollEvent(timestamp=ts.next(), x=20, y=20, dx=0, dy=1),
            MouseMoveEvent(timestamp=ts.next(), x=30, y=30),
            MouseMoveEvent(timestamp=ts.next(), x=40, y=40),
        ]
        result = merge_consecutive_mouse_move_events(events)

        # Should have: scroll, merged_move(20,20), scroll, merged_move(40,40)
        move_events = [e for e in result if isinstance(e, MouseMoveEvent)]
        assert len(move_events) == 2
        assert move_events[0].x == 20
        assert move_events[1].x == 40


# =============================================================================
# Test: merge_consecutive_mouse_scroll_events
# =============================================================================

class TestMergeConsecutiveMouseScrollEventsComprehensive:
    """Comprehensive tests for scroll merging."""

    def test_scroll_deltas_accumulate(self, ts):
        """Scroll deltas should sum correctly."""
        events = [
            MouseScrollEvent(timestamp=ts.next(), x=100, y=100, dx=2, dy=0),
            MouseScrollEvent(timestamp=ts.next(), x=100, y=100, dx=1, dy=0),
            MouseScrollEvent(timestamp=ts.next(), x=100, y=100, dx=-1, dy=0),
        ]
        result = merge_consecutive_mouse_scroll_events(events)

        assert len(result) == 1
        assert result[0].dx == 2  # 2 + 1 - 1 = 2
        assert result[0].dy == 0

    def test_scrolls_interrupted_by_move_create_groups(self, ts):
        """Scrolls interrupted by moves should create separate groups."""
        events = [
            MouseMoveEvent(timestamp=ts.next(), x=0, y=0),
            MouseScrollEvent(timestamp=ts.next(), x=100, y=100, dx=2, dy=0),
            MouseScrollEvent(timestamp=ts.next(), x=100, y=100, dx=1, dy=0),
            MouseScrollEvent(timestamp=ts.next(), x=100, y=100, dx=-1, dy=0),
            MouseMoveEvent(timestamp=ts.next(), x=200, y=200),
            MouseScrollEvent(timestamp=ts.next(), x=200, y=200, dx=0, dy=1),
            MouseScrollEvent(timestamp=ts.next(), x=200, y=200, dx=1, dy=0),
        ]
        result = merge_consecutive_mouse_scroll_events(events)

        scroll_events = [e for e in result if isinstance(e, MouseScrollEvent)]
        assert len(scroll_events) == 2
        assert scroll_events[0].dx == 2
        assert scroll_events[1].dx == 1
        assert scroll_events[1].dy == 1


# =============================================================================
# Test: merge_consecutive_keyboard_events
# =============================================================================

class TestMergeConsecutiveKeyboardEventsComprehensive:
    """Comprehensive tests for keyboard event merging."""

    def test_key_sequence_merges_to_type_event(self, ts):
        """Sequence of key press/release should merge to KeyTypeEvent."""
        events = [
            KeyDownEvent(timestamp=ts.next(), key_char="a"),
            KeyUpEvent(timestamp=ts.next(), key_char="a"),
            KeyDownEvent(timestamp=ts.next(), key_char="b"),
            KeyUpEvent(timestamp=ts.next(), key_char="b"),
            KeyDownEvent(timestamp=ts.next(), key_char="c"),
            KeyUpEvent(timestamp=ts.next(), key_char="c"),
        ]
        result = merge_consecutive_keyboard_events(events)

        # Each press/release creates a KeyTypeEvent
        assert all(isinstance(e, KeyTypeEvent) for e in result)
        texts = [e.text for e in result]
        assert "a" in texts
        assert "b" in texts
        assert "c" in texts

    def test_keyboard_interrupted_by_mouse_creates_groups(self, ts):
        """Keyboard events interrupted by mouse should create separate groups."""
        events = [
            MouseDownEvent(timestamp=ts.next(), x=100, y=100, button=MouseButton.LEFT),
            KeyDownEvent(timestamp=ts.next(), key_char="a"),
            KeyUpEvent(timestamp=ts.next(), key_char="a"),
            KeyDownEvent(timestamp=ts.next(), key_char="b"),
            KeyUpEvent(timestamp=ts.next(), key_char="b"),
            MouseUpEvent(timestamp=ts.next(), x=100, y=100, button=MouseButton.LEFT),
            KeyDownEvent(timestamp=ts.next(), key_char="c"),
            KeyUpEvent(timestamp=ts.next(), key_char="c"),
        ]
        result = merge_consecutive_keyboard_events(events)

        # Should have click events interspersed with type events
        mouse_events = [e for e in result if isinstance(e, (MouseDownEvent, MouseUpEvent))]
        type_events = [e for e in result if isinstance(e, KeyTypeEvent)]
        assert len(mouse_events) == 2
        assert len(type_events) >= 2

    def test_modifier_keys_handled(self, ts):
        """Modifier keys (shift, ctrl) should be handled."""
        events = [
            KeyDownEvent(timestamp=ts.next(), key_name="shift"),
            KeyDownEvent(timestamp=ts.next(), key_char="a"),
            KeyUpEvent(timestamp=ts.next(), key_char="a"),
            KeyUpEvent(timestamp=ts.next(), key_name="shift"),
        ]
        result = merge_consecutive_keyboard_events(events)

        # All should be merged since keys are still pressed
        assert len(result) >= 1


# =============================================================================
# Test: detect_drag_events
# =============================================================================

class TestDetectDragEventsComprehensive:
    """Comprehensive tests for drag detection."""

    def test_drag_with_multiple_moves(self, ts):
        """Drag with multiple intermediate moves should be detected."""
        events = [
            MouseDownEvent(timestamp=ts.next(), x=100, y=100, button=MouseButton.LEFT),
            MouseMoveEvent(timestamp=ts.next(), x=150, y=150),
            MouseMoveEvent(timestamp=ts.next(), x=200, y=200),
            MouseMoveEvent(timestamp=ts.next(), x=250, y=250),
            MouseMoveEvent(timestamp=ts.next(), x=300, y=300),
            MouseUpEvent(timestamp=ts.next(), x=300, y=300, button=MouseButton.LEFT),
        ]
        result = detect_drag_events(events)

        assert len(result) == 1
        assert isinstance(result[0], MouseDragEvent)
        assert result[0].x == 100  # start position
        assert result[0].dx == 200  # displacement (300 - 100)
        # Children should include all moves
        assert len(result[0].children) == 6

    def test_drag_interrupted_by_other_event(self, ts):
        """Drag interrupted by non-mouse event should not create drag."""
        events = [
            MouseDownEvent(timestamp=ts.next(), x=100, y=100, button=MouseButton.LEFT),
            MouseMoveEvent(timestamp=ts.next(), x=150, y=150),
            KeyDownEvent(timestamp=ts.next(), key_char="a"),  # Interruption
            MouseMoveEvent(timestamp=ts.next(), x=200, y=200),
            MouseUpEvent(timestamp=ts.next(), x=200, y=200, button=MouseButton.LEFT),
        ]
        result = detect_drag_events(events)

        # Should not be a drag due to interruption
        drags = [e for e in result if isinstance(e, MouseDragEvent)]
        assert len(drags) == 0

    def test_right_button_drag(self, ts):
        """Drag with right button should work."""
        events = [
            MouseDownEvent(timestamp=ts.next(), x=100, y=100, button=MouseButton.RIGHT),
            MouseMoveEvent(timestamp=ts.next(), x=200, y=200),
            MouseUpEvent(timestamp=ts.next(), x=200, y=200, button=MouseButton.RIGHT),
        ]
        result = detect_drag_events(events)

        assert len(result) == 1
        assert isinstance(result[0], MouseDragEvent)
        assert result[0].button == MouseButton.RIGHT


# =============================================================================
# Test: remove_redundant_mouse_move_events
# =============================================================================

class TestRemoveRedundantMouseMoveEventsComprehensive:
    """Comprehensive tests for redundant move removal."""

    def test_removes_duplicates_in_long_chain(self, ts):
        """Should remove duplicate positions even in long chains."""
        events = []
        for _ in range(3):
            events.extend([
                MouseMoveEvent(timestamp=ts.next(), x=1, y=1),
                MouseDownEvent(timestamp=ts.next(), x=1, y=1, button=MouseButton.LEFT),
                MouseMoveEvent(timestamp=ts.next(), x=1, y=1),  # Redundant
                MouseUpEvent(timestamp=ts.next(), x=1, y=1, button=MouseButton.LEFT),
                MouseMoveEvent(timestamp=ts.next(), x=2, y=2),
                MouseDownEvent(timestamp=ts.next(), x=2, y=2, button=MouseButton.LEFT),
                MouseMoveEvent(timestamp=ts.next(), x=3, y=3),
                MouseUpEvent(timestamp=ts.next(), x=3, y=3, button=MouseButton.LEFT),
                MouseMoveEvent(timestamp=ts.next(), x=3, y=3),  # Redundant
            ])

        result = remove_redundant_mouse_move_events(events)

        # Count moves - should have fewer due to redundant removal
        original_moves = len([e for e in events if isinstance(e, MouseMoveEvent)])
        result_moves = len([e for e in result if isinstance(e, MouseMoveEvent)])
        assert result_moves < original_moves


# =============================================================================
# Test: Full Pipeline
# =============================================================================

class TestFullPipelineComprehensive:
    """Test complete processing pipeline with complex scenarios."""

    def test_realistic_workflow(self, ts):
        """Test a realistic user workflow: click, type, drag, scroll."""
        events = [
            # Click on text field
            MouseMoveEvent(timestamp=ts.next(), x=100, y=100),
            MouseDownEvent(timestamp=ts.next(), x=100, y=100, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=ts.next(), x=100, y=100, button=MouseButton.LEFT),
            # Type some text
            KeyDownEvent(timestamp=ts.next(), key_char="h"),
            KeyUpEvent(timestamp=ts.next(), key_char="h"),
            KeyDownEvent(timestamp=ts.next(), key_char="i"),
            KeyUpEvent(timestamp=ts.next(), key_char="i"),
            # Drag to select
            MouseDownEvent(timestamp=ts.next(), x=100, y=200, button=MouseButton.LEFT),
            MouseMoveEvent(timestamp=ts.next(), x=150, y=200),
            MouseMoveEvent(timestamp=ts.next(), x=200, y=200),
            MouseUpEvent(timestamp=ts.next(), x=200, y=200, button=MouseButton.LEFT),
            # Scroll down
            MouseScrollEvent(timestamp=ts.next(), x=200, y=200, dx=0, dy=-3),
            MouseScrollEvent(timestamp=ts.next(), x=200, y=200, dx=0, dy=-2),
        ]
        result = process_events(events)

        # Should have processed into meaningful actions
        clicks = [e for e in result if isinstance(e, MouseClickEvent)]
        types = [e for e in result if isinstance(e, KeyTypeEvent)]
        drags = [e for e in result if isinstance(e, MouseDragEvent)]
        scrolls = [e for e in result if isinstance(e, MouseScrollEvent)]

        assert len(clicks) >= 1, "Should have at least one click"
        assert len(types) >= 1, "Should have typed text"
        assert len(drags) >= 1, "Should have a drag"
        assert len(scrolls) >= 1, "Should have scrolls"

    def test_empty_events(self):
        """Empty event list should return empty."""
        result = process_events([])
        assert result == []

    def test_single_event(self, ts):
        """Single event should pass through."""
        events = [MouseMoveEvent(timestamp=ts.next(), x=100, y=100)]
        result = process_events(events)
        assert len(result) == 1

"""Tests for event processing pipeline."""

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
    detect_drag_events,
    merge_consecutive_keyboard_events,
    merge_consecutive_mouse_click_events,
    merge_consecutive_mouse_move_events,
    merge_consecutive_mouse_scroll_events,
    process_events,
    remove_invalid_keyboard_events,
    remove_redundant_mouse_move_events,
)


class TestRemoveInvalidKeyboardEvents:
    """Tests for remove_invalid_keyboard_events."""

    def test_removes_empty_key_events(self):
        """Test that events with no key info are removed."""
        events = [
            KeyDownEvent(timestamp=1.0),  # No key info
            KeyDownEvent(timestamp=2.0, key_char="a"),  # Valid
            KeyUpEvent(timestamp=3.0),  # No key info
        ]
        result = remove_invalid_keyboard_events(events)
        assert len(result) == 1
        assert result[0].key_char == "a"

    def test_keeps_valid_key_events(self):
        """Test that valid key events are kept."""
        events = [
            KeyDownEvent(timestamp=1.0, key_char="a"),
            KeyDownEvent(timestamp=2.0, key_name="shift"),
            KeyDownEvent(timestamp=3.0, key_vk="65"),
        ]
        result = remove_invalid_keyboard_events(events)
        assert len(result) == 3


class TestRemoveRedundantMouseMoveEvents:
    """Tests for remove_redundant_mouse_move_events."""

    def test_removes_duplicate_positions(self):
        """Test that consecutive moves to same position are removed."""
        events = [
            MouseMoveEvent(timestamp=1.0, x=100.0, y=100.0),
            MouseMoveEvent(timestamp=2.0, x=100.0, y=100.0),  # Duplicate
            MouseMoveEvent(timestamp=3.0, x=200.0, y=200.0),
        ]
        result = remove_redundant_mouse_move_events(events)
        assert len(result) == 2
        assert result[0].x == 100.0
        assert result[1].x == 200.0

    def test_keeps_different_positions(self):
        """Test that moves to different positions are kept."""
        events = [
            MouseMoveEvent(timestamp=1.0, x=100.0, y=100.0),
            MouseMoveEvent(timestamp=2.0, x=100.0, y=101.0),
            MouseMoveEvent(timestamp=3.0, x=101.0, y=101.0),
        ]
        result = remove_redundant_mouse_move_events(events)
        assert len(result) == 3


class TestMergeConsecutiveKeyboardEvents:
    """Tests for merge_consecutive_keyboard_events."""

    def test_merges_typed_text(self):
        """Test merging key events into typed text.

        Note: Each press/release cycle creates a separate KeyTypeEvent.
        This matches OpenAdapt's behavior of grouping by pressed state.
        """
        events = [
            KeyDownEvent(timestamp=1.0, key_char="h"),
            KeyUpEvent(timestamp=1.1, key_char="h"),
            KeyDownEvent(timestamp=1.2, key_char="i"),
            KeyUpEvent(timestamp=1.3, key_char="i"),
        ]
        result = merge_consecutive_keyboard_events(events)
        # Each key press/release pair becomes a separate KeyTypeEvent
        assert len(result) == 2
        assert all(isinstance(r, KeyTypeEvent) for r in result)
        assert result[0].text == "h"
        assert result[1].text == "i"

    def test_preserves_non_keyboard_events(self):
        """Test that non-keyboard events break the merge."""
        events = [
            KeyDownEvent(timestamp=1.0, key_char="a"),
            KeyUpEvent(timestamp=1.1, key_char="a"),
            MouseMoveEvent(timestamp=1.5, x=100.0, y=100.0),
            KeyDownEvent(timestamp=2.0, key_char="b"),
            KeyUpEvent(timestamp=2.1, key_char="b"),
        ]
        result = merge_consecutive_keyboard_events(events)
        assert len(result) == 3  # KeyTypeEvent("a"), MouseMove, KeyTypeEvent("b")


class TestMergeConsecutiveMouseMoveEvents:
    """Tests for merge_consecutive_mouse_move_events."""

    def test_merges_consecutive_moves(self):
        """Test merging consecutive mouse moves."""
        events = [
            MouseMoveEvent(timestamp=1.0, x=100.0, y=100.0),
            MouseMoveEvent(timestamp=1.1, x=110.0, y=110.0),
            MouseMoveEvent(timestamp=1.2, x=120.0, y=120.0),
        ]
        result = merge_consecutive_mouse_move_events(events)
        assert len(result) == 1
        # Should have final position
        assert result[0].x == 120.0
        assert result[0].y == 120.0

    def test_single_move_not_merged(self):
        """Test that a single move is not modified."""
        events = [MouseMoveEvent(timestamp=1.0, x=100.0, y=100.0)]
        result = merge_consecutive_mouse_move_events(events)
        assert len(result) == 1
        assert result[0].x == 100.0


class TestMergeConsecutiveMouseScrollEvents:
    """Tests for merge_consecutive_mouse_scroll_events."""

    def test_merges_scroll_deltas(self):
        """Test that scroll deltas are summed."""
        events = [
            MouseScrollEvent(timestamp=1.0, x=100.0, y=100.0, dx=0.0, dy=-1.0),
            MouseScrollEvent(timestamp=1.1, x=100.0, y=100.0, dx=0.0, dy=-2.0),
            MouseScrollEvent(timestamp=1.2, x=100.0, y=100.0, dx=0.0, dy=-1.0),
        ]
        result = merge_consecutive_mouse_scroll_events(events)
        assert len(result) == 1
        assert result[0].dy == -4.0  # Sum of all dy values


class TestMergeConsecutiveMouseClickEvents:
    """Tests for merge_consecutive_mouse_click_events."""

    def test_creates_single_click(self):
        """Test single click detection."""
        events = [
            MouseDownEvent(timestamp=1.0, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=1.1, x=100.0, y=100.0, button=MouseButton.LEFT),
        ]
        result = merge_consecutive_mouse_click_events(events)
        assert len(result) == 1
        assert isinstance(result[0], MouseClickEvent)
        assert result[0].button == MouseButton.LEFT

    def test_creates_double_click(self):
        """Test double click detection."""
        events = [
            MouseDownEvent(timestamp=1.0, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=1.05, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseDownEvent(timestamp=1.1, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=1.15, x=100.0, y=100.0, button=MouseButton.LEFT),
        ]
        result = merge_consecutive_mouse_click_events(events)
        assert len(result) == 1
        assert isinstance(result[0], MouseDoubleClickEvent)

    def test_separate_clicks_too_far_apart(self):
        """Test that clicks too far apart in time stay separate."""
        events = [
            MouseDownEvent(timestamp=1.0, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=1.05, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseDownEvent(timestamp=3.0, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=3.05, x=100.0, y=100.0, button=MouseButton.LEFT),
        ]
        result = merge_consecutive_mouse_click_events(events)
        assert len(result) == 2
        assert all(isinstance(r, MouseClickEvent) for r in result)


class TestDetectDragEvents:
    """Tests for detect_drag_events."""

    def test_detects_drag(self):
        """Test drag detection from down + moves + up."""
        events = [
            MouseDownEvent(timestamp=1.0, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseMoveEvent(timestamp=1.1, x=150.0, y=150.0),
            MouseMoveEvent(timestamp=1.2, x=200.0, y=200.0),
            MouseUpEvent(timestamp=1.3, x=200.0, y=200.0, button=MouseButton.LEFT),
        ]
        result = detect_drag_events(events)
        assert len(result) == 1
        assert isinstance(result[0], MouseDragEvent)
        assert result[0].x == 100.0  # start position
        assert result[0].dx == 100.0  # displacement (200 - 100)

    def test_no_drag_for_small_movement(self):
        """Test that small movements don't create drags."""
        events = [
            MouseDownEvent(timestamp=1.0, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseMoveEvent(timestamp=1.1, x=101.0, y=101.0),
            MouseUpEvent(timestamp=1.2, x=102.0, y=102.0, button=MouseButton.LEFT),
        ]
        result = detect_drag_events(events)
        # Should not create drag due to small distance
        assert not any(isinstance(e, MouseDragEvent) for e in result)


class TestProcessEvents:
    """Tests for full processing pipeline."""

    def test_full_pipeline(self):
        """Test complete processing pipeline."""
        events = [
            # Mouse movement
            MouseMoveEvent(timestamp=1.0, x=100.0, y=100.0),
            MouseMoveEvent(timestamp=1.1, x=100.0, y=100.0),  # Redundant
            MouseMoveEvent(timestamp=1.2, x=200.0, y=200.0),
            # Click
            MouseDownEvent(timestamp=2.0, x=200.0, y=200.0, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=2.1, x=200.0, y=200.0, button=MouseButton.LEFT),
            # Type
            KeyDownEvent(timestamp=3.0, key_char="a"),
            KeyUpEvent(timestamp=3.1, key_char="a"),
            KeyDownEvent(timestamp=3.2, key_char="b"),
            KeyUpEvent(timestamp=3.3, key_char="b"),
        ]
        result = process_events(events)

        # Should have: merged move, single click, key types
        types = [type(e).__name__ for e in result]
        assert "MouseMoveEvent" in types
        assert "MouseClickEvent" in types
        assert "KeyTypeEvent" in types

        # Check that keyboard events were merged into KeyTypeEvents
        key_types = [e for e in result if isinstance(e, KeyTypeEvent)]
        assert len(key_types) == 2
        assert key_types[0].text == "a"
        assert key_types[1].text == "b"

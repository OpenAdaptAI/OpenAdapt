"""Tests for event schemas."""

import pytest

from openadapt_capture.events import (
    AudioChunkEvent,
    EventType,
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
    ScreenFrameEvent,
)


class TestMouseEvents:
    """Tests for mouse event types."""

    def test_mouse_move_event(self):
        """Test MouseMoveEvent creation and serialization."""
        event = MouseMoveEvent(
            timestamp=1234567890.123,
            x=100.5,
            y=200.5,
        )
        assert event.type == EventType.MOUSE_MOVE
        assert event.x == 100.5
        assert event.y == 200.5
        assert event.timestamp == 1234567890.123

        # Test serialization
        data = event.model_dump()
        assert data["type"] == "mouse.move"
        assert data["x"] == 100.5

    def test_mouse_down_event(self):
        """Test MouseDownEvent creation."""
        event = MouseDownEvent(
            timestamp=1234567890.123,
            x=100.0,
            y=200.0,
            button=MouseButton.LEFT,
        )
        assert event.type == EventType.MOUSE_DOWN
        assert event.button == MouseButton.LEFT

    def test_mouse_up_event(self):
        """Test MouseUpEvent creation."""
        event = MouseUpEvent(
            timestamp=1234567890.123,
            x=100.0,
            y=200.0,
            button=MouseButton.RIGHT,
        )
        assert event.type == EventType.MOUSE_UP
        assert event.button == MouseButton.RIGHT

    def test_mouse_scroll_event(self):
        """Test MouseScrollEvent creation."""
        event = MouseScrollEvent(
            timestamp=1234567890.123,
            x=100.0,
            y=200.0,
            dx=0.0,
            dy=-3.0,
        )
        assert event.type == EventType.MOUSE_SCROLL
        assert event.dy == -3.0

    def test_mouse_click_event(self):
        """Test MouseClickEvent with children."""
        down = MouseDownEvent(
            timestamp=1.0,
            x=100.0,
            y=200.0,
            button=MouseButton.LEFT,
        )
        up = MouseUpEvent(
            timestamp=1.1,
            x=100.0,
            y=200.0,
            button=MouseButton.LEFT,
        )
        click = MouseClickEvent(
            timestamp=1.0,
            x=100.0,
            y=200.0,
            button=MouseButton.LEFT,
            children=[down, up],
        )
        assert click.type == EventType.MOUSE_SINGLECLICK
        assert len(click.children) == 2

    def test_mouse_double_click_event(self):
        """Test MouseDoubleClickEvent."""
        event = MouseDoubleClickEvent(
            timestamp=1.0,
            x=100.0,
            y=200.0,
            button=MouseButton.LEFT,
        )
        assert event.type == EventType.MOUSE_DOUBLECLICK

    def test_mouse_drag_event(self):
        """Test MouseDragEvent."""
        event = MouseDragEvent(
            timestamp=1.0,
            x=100.0,
            y=200.0,
            dx=200.0,  # end_x - start_x = 300 - 100
            dy=200.0,  # end_y - start_y = 400 - 200
            button=MouseButton.LEFT,
        )
        assert event.type == EventType.MOUSE_DRAG
        assert event.x == 100.0
        assert event.dx == 200.0


class TestKeyboardEvents:
    """Tests for keyboard event types."""

    def test_key_down_event(self):
        """Test KeyDownEvent creation."""
        event = KeyDownEvent(
            timestamp=1234567890.123,
            key_char="a",
        )
        assert event.type == EventType.KEY_DOWN
        assert event.key_char == "a"

    def test_key_down_event_with_name(self):
        """Test KeyDownEvent with key name (special key)."""
        event = KeyDownEvent(
            timestamp=1.0,
            key_name="shift",
        )
        assert event.key_name == "shift"
        assert event.key_char is None

    def test_key_up_event(self):
        """Test KeyUpEvent creation."""
        event = KeyUpEvent(
            timestamp=1.0,
            key_char="b",
        )
        assert event.type == EventType.KEY_UP

    def test_key_type_event(self):
        """Test KeyTypeEvent with children."""
        children = [
            KeyDownEvent(timestamp=1.0, key_char="h"),
            KeyUpEvent(timestamp=1.1, key_char="h"),
            KeyDownEvent(timestamp=1.2, key_char="i"),
            KeyUpEvent(timestamp=1.3, key_char="i"),
        ]
        event = KeyTypeEvent(
            timestamp=1.0,
            text="hi",
            children=children,
        )
        assert event.type == EventType.KEY_TYPE
        assert event.text == "hi"
        assert len(event.children) == 4


class TestScreenEvents:
    """Tests for screen event types."""

    def test_screen_frame_event_with_video(self):
        """Test ScreenFrameEvent with video timestamp."""
        event = ScreenFrameEvent(
            timestamp=1.0,
            video_timestamp=0.5,
            width=1920,
            height=1080,
        )
        assert event.type == EventType.SCREEN_FRAME
        assert event.video_timestamp == 0.5
        assert event.image_path is None

    def test_screen_frame_event_with_image(self):
        """Test ScreenFrameEvent with image path."""
        event = ScreenFrameEvent(
            timestamp=1.0,
            image_path="/path/to/screenshot.png",
            width=1920,
            height=1080,
        )
        assert event.image_path == "/path/to/screenshot.png"
        assert event.video_timestamp is None


class TestAudioEvents:
    """Tests for audio event types."""

    def test_audio_chunk_event(self):
        """Test AudioChunkEvent creation."""
        event = AudioChunkEvent(
            timestamp=1.0,
            start_time=1.0,
            end_time=31.0,
            transcription="Hello world",
        )
        assert event.type == EventType.AUDIO_CHUNK
        assert event.transcription == "Hello world"
        assert event.end_time - event.start_time == 30.0

    def test_audio_chunk_event_no_transcription(self):
        """Test AudioChunkEvent without transcription."""
        event = AudioChunkEvent(
            timestamp=1.0,
            start_time=1.0,
            end_time=31.0,
        )
        assert event.transcription is None


class TestEventSerialization:
    """Tests for event serialization."""

    def test_event_to_json(self):
        """Test event serialization to JSON."""
        event = MouseMoveEvent(
            timestamp=1234567890.123,
            x=100.5,
            y=200.5,
        )
        json_str = event.model_dump_json()
        assert "mouse.move" in json_str
        assert "100.5" in json_str

    def test_event_from_dict(self):
        """Test event creation from dict."""
        data = {
            "timestamp": 1234567890.123,
            "x": 100.5,
            "y": 200.5,
        }
        event = MouseMoveEvent(**data)
        assert event.x == 100.5

    def test_nested_event_serialization(self):
        """Test serialization of events with children."""
        down = MouseDownEvent(
            timestamp=1.0,
            x=100.0,
            y=200.0,
            button=MouseButton.LEFT,
        )
        up = MouseUpEvent(
            timestamp=1.1,
            x=100.0,
            y=200.0,
            button=MouseButton.LEFT,
        )
        click = MouseClickEvent(
            timestamp=1.0,
            x=100.0,
            y=200.0,
            button=MouseButton.LEFT,
            children=[down, up],
        )

        data = click.model_dump()
        assert len(data["children"]) == 2
        assert data["children"][0]["type"] == "mouse.down"

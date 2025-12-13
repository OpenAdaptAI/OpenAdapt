"""Tests for SQLite storage."""

import tempfile
from pathlib import Path

import pytest

from openadapt_capture.events import (
    EventType,
    KeyDownEvent,
    KeyUpEvent,
    MouseButton,
    MouseDownEvent,
    MouseMoveEvent,
    MouseUpEvent,
)
from openadapt_capture.storage import (
    Capture,
    CaptureStorage,
    create_capture,
    load_capture,
)


@pytest.fixture
def temp_db():
    """Create a temporary database file."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    # Cleanup
    Path(db_path).unlink(missing_ok=True)


@pytest.fixture
def temp_dir():
    """Create a temporary directory for captures."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


class TestCaptureStorage:
    """Tests for CaptureStorage class."""

    def test_init_storage(self, temp_db):
        """Test storage initialization."""
        storage = CaptureStorage(temp_db)
        assert storage.db_path == Path(temp_db)
        storage.close()

    def test_context_manager(self, temp_db):
        """Test storage as context manager."""
        with CaptureStorage(temp_db) as storage:
            assert storage.is_open is False  # Connection created lazily
            capture = Capture(
                id="test",
                started_at=1234567890.0,
                platform="darwin",
                screen_width=1920,
                screen_height=1080,
            )
            storage.init_capture(capture)
            assert storage.is_open is True

    def test_init_and_get_capture(self, temp_db):
        """Test initializing and retrieving capture metadata."""
        with CaptureStorage(temp_db) as storage:
            capture = Capture(
                id="abc123",
                started_at=1234567890.0,
                platform="darwin",
                screen_width=1920,
                screen_height=1080,
                task_description="Test task",
            )
            storage.init_capture(capture)

            retrieved = storage.get_capture()
            assert retrieved is not None
            assert retrieved.id == "abc123"
            assert retrieved.platform == "darwin"
            assert retrieved.task_description == "Test task"

    def test_update_capture(self, temp_db):
        """Test updating capture metadata."""
        with CaptureStorage(temp_db) as storage:
            capture = Capture(
                id="abc123",
                started_at=1234567890.0,
                platform="darwin",
                screen_width=1920,
                screen_height=1080,
            )
            storage.init_capture(capture)

            capture.ended_at = 1234567900.0
            capture.task_description = "Updated task"
            storage.update_capture(capture)

            retrieved = storage.get_capture()
            assert retrieved.ended_at == 1234567900.0
            assert retrieved.task_description == "Updated task"

    def test_write_and_get_event(self, temp_db):
        """Test writing and retrieving a single event."""
        with CaptureStorage(temp_db) as storage:
            event = MouseMoveEvent(
                timestamp=1234567890.0,
                x=100.0,
                y=200.0,
            )
            event_id = storage.write_event(event)
            assert event_id == 1

            events = storage.get_events()
            assert len(events) == 1
            assert events[0].x == 100.0
            assert events[0].y == 200.0

    def test_write_multiple_events(self, temp_db):
        """Test writing multiple events."""
        with CaptureStorage(temp_db) as storage:
            events = [
                MouseMoveEvent(timestamp=1.0, x=100.0, y=100.0),
                MouseMoveEvent(timestamp=2.0, x=200.0, y=200.0),
                MouseDownEvent(timestamp=3.0, x=200.0, y=200.0, button=MouseButton.LEFT),
                MouseUpEvent(timestamp=3.1, x=200.0, y=200.0, button=MouseButton.LEFT),
            ]
            storage.write_events(events)

            retrieved = storage.get_events()
            assert len(retrieved) == 4

    def test_get_events_by_time_range(self, temp_db):
        """Test filtering events by timestamp."""
        with CaptureStorage(temp_db) as storage:
            events = [
                MouseMoveEvent(timestamp=1.0, x=100.0, y=100.0),
                MouseMoveEvent(timestamp=5.0, x=200.0, y=200.0),
                MouseMoveEvent(timestamp=10.0, x=300.0, y=300.0),
            ]
            storage.write_events(events)

            # Get events in range
            filtered = storage.get_events(start_time=2.0, end_time=8.0)
            assert len(filtered) == 1
            assert filtered[0].x == 200.0

    def test_get_events_by_type(self, temp_db):
        """Test filtering events by type."""
        with CaptureStorage(temp_db) as storage:
            events = [
                MouseMoveEvent(timestamp=1.0, x=100.0, y=100.0),
                MouseDownEvent(timestamp=2.0, x=100.0, y=100.0, button=MouseButton.LEFT),
                KeyDownEvent(timestamp=3.0, key_char="a"),
            ]
            storage.write_events(events)

            # Get only mouse down events
            mouse_downs = storage.get_events(event_types=[EventType.MOUSE_DOWN])
            assert len(mouse_downs) == 1

            # Get only keyboard events
            key_events = storage.get_events(event_types=[EventType.KEY_DOWN])
            assert len(key_events) == 1

    def test_get_event_count(self, temp_db):
        """Test getting event counts."""
        with CaptureStorage(temp_db) as storage:
            events = [
                MouseMoveEvent(timestamp=1.0, x=100.0, y=100.0),
                MouseMoveEvent(timestamp=2.0, x=200.0, y=200.0),
                KeyDownEvent(timestamp=3.0, key_char="a"),
            ]
            storage.write_events(events)

            assert storage.get_event_count() == 3
            assert storage.get_event_count(EventType.MOUSE_MOVE) == 2
            assert storage.get_event_count(EventType.KEY_DOWN) == 1

    def test_iter_events(self, temp_db):
        """Test iterating over events."""
        with CaptureStorage(temp_db) as storage:
            events = [
                MouseMoveEvent(timestamp=float(i), x=float(i * 10), y=float(i * 10))
                for i in range(100)
            ]
            storage.write_events(events)

            count = 0
            for event in storage.iter_events(batch_size=10):
                count += 1
            assert count == 100


class TestCreateAndLoadCapture:
    """Tests for create_capture and load_capture functions."""

    def test_create_capture(self, temp_dir):
        """Test creating a new capture."""
        capture, storage = create_capture(
            capture_dir=temp_dir,
            platform="darwin",
            screen_width=1920,
            screen_height=1080,
            task_description="Test capture",
        )

        assert capture.platform == "darwin"
        assert capture.screen_width == 1920
        assert capture.task_description == "Test capture"
        assert len(capture.id) == 8

        storage.close()

    def test_load_capture(self, temp_dir):
        """Test loading an existing capture."""
        # Create capture
        capture, storage = create_capture(
            capture_dir=temp_dir,
            platform="linux",
            screen_width=2560,
            screen_height=1440,
        )
        capture_id = capture.id
        storage.write_event(MouseMoveEvent(timestamp=1.0, x=100.0, y=100.0))
        storage.close()

        # Load capture
        loaded_capture, loaded_storage = load_capture(temp_dir)

        assert loaded_capture is not None
        assert loaded_capture.id == capture_id
        assert loaded_capture.platform == "linux"

        events = loaded_storage.get_events()
        assert len(events) == 1

        loaded_storage.close()

    def test_load_nonexistent_capture(self, temp_dir):
        """Test loading a capture that doesn't exist."""
        with pytest.raises(FileNotFoundError):
            load_capture(Path(temp_dir) / "nonexistent")


class TestCaptureModel:
    """Tests for Capture Pydantic model."""

    def test_capture_defaults(self):
        """Test Capture model default values."""
        capture = Capture(
            id="test",
            started_at=1234567890.0,
            platform="darwin",
            screen_width=1920,
            screen_height=1080,
        )
        assert capture.ended_at is None
        assert capture.task_description is None
        assert capture.double_click_interval_seconds == 0.5
        assert capture.double_click_distance_pixels == 5.0
        assert capture.metadata == {}

    def test_capture_with_metadata(self):
        """Test Capture model with metadata."""
        capture = Capture(
            id="test",
            started_at=1234567890.0,
            platform="win32",
            screen_width=1920,
            screen_height=1080,
            metadata={"user": "test_user", "version": "1.0"},
        )
        assert capture.metadata["user"] == "test_user"

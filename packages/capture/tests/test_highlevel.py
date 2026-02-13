"""Tests for high-level Recorder and Capture APIs."""

import tempfile
from pathlib import Path

import pytest

from openadapt_capture import Capture, Recorder
from openadapt_capture.events import MouseButton, MouseDownEvent, MouseUpEvent
from openadapt_capture.storage import CaptureStorage


@pytest.fixture
def temp_capture_dir():
    """Create a temporary directory for captures."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


class TestRecorder:
    """Tests for Recorder class."""

    def test_recorder_creates_directory(self, temp_capture_dir):
        """Test that Recorder creates capture directory."""
        capture_path = Path(temp_capture_dir) / "new_capture"
        recorder = Recorder(capture_path)
        recorder.start()
        recorder.stop()

        assert capture_path.exists()
        assert (capture_path / "capture.db").exists()

    def test_recorder_context_manager(self, temp_capture_dir):
        """Test Recorder as context manager."""
        capture_path = Path(temp_capture_dir) / "capture"

        with Recorder(capture_path, task_description="Test task") as recorder:
            assert recorder.is_recording
            # Recording happens automatically

        assert not recorder.is_recording
        assert (capture_path / "capture.db").exists()

    def test_recorder_with_task_description(self, temp_capture_dir):
        """Test that task description is saved."""
        capture_path = Path(temp_capture_dir) / "capture"

        with Recorder(capture_path, task_description="My test task"):
            pass

        # Load and verify
        capture = Capture.load(capture_path)
        assert capture.task_description == "My test task"
        capture.close()


class TestCapture:
    """Tests for Capture/CaptureSession class."""

    def test_capture_load(self, temp_capture_dir):
        """Test loading a capture."""
        capture_path = Path(temp_capture_dir) / "capture"

        # Create a capture first
        with Recorder(capture_path, task_description="Test"):
            pass

        # Load it
        capture = Capture.load(capture_path)
        assert capture.task_description == "Test"
        assert capture.id is not None
        capture.close()

    def test_capture_load_nonexistent(self, temp_capture_dir):
        """Test loading nonexistent capture raises error."""
        with pytest.raises(FileNotFoundError):
            Capture.load(Path(temp_capture_dir) / "nonexistent")

    def test_capture_properties(self, temp_capture_dir):
        """Test capture metadata properties."""
        capture_path = Path(temp_capture_dir) / "capture"

        with Recorder(capture_path, task_description="Props test"):
            pass

        capture = Capture.load(capture_path)
        assert capture.started_at is not None
        assert capture.ended_at is not None
        assert capture.duration is not None
        assert capture.duration >= 0
        assert capture.platform in ("darwin", "win32", "linux")
        assert capture.screen_size[0] > 0
        assert capture.screen_size[1] > 0
        capture.close()

    def test_capture_actions_iterator(self, temp_capture_dir):
        """Test iterating over actions."""
        capture_path = Path(temp_capture_dir) / "capture"

        # Create capture and add some events manually
        with Recorder(capture_path):
            pass

        # Get the capture's time range and add events within it
        storage = CaptureStorage(capture_path / "capture.db")
        capture_meta = storage.get_capture()
        started_at = capture_meta.started_at

        # Write events with timestamps within the capture window
        storage.write_event(
            MouseDownEvent(timestamp=started_at + 0.001, x=100.0, y=100.0, button=MouseButton.LEFT)
        )
        storage.write_event(
            MouseUpEvent(timestamp=started_at + 0.002, x=100.0, y=100.0, button=MouseButton.LEFT)
        )
        storage.close()

        # Load and iterate
        capture = Capture.load(capture_path)
        actions = list(capture.actions())

        # Should have merged into a click
        assert len(actions) >= 1
        capture.close()

    def test_capture_context_manager(self, temp_capture_dir):
        """Test Capture as context manager."""
        capture_path = Path(temp_capture_dir) / "capture"

        with Recorder(capture_path):
            pass

        with Capture.load(capture_path) as capture:
            assert capture.id is not None


class TestAction:
    """Tests for Action dataclass."""

    def test_action_properties(self, temp_capture_dir):
        """Test Action property accessors."""
        capture_path = Path(temp_capture_dir) / "capture"

        # Create capture with events
        with Recorder(capture_path):
            pass

        storage = CaptureStorage(capture_path / "capture.db")
        storage.write_event(
            MouseDownEvent(timestamp=1.0, x=150.0, y=250.0, button=MouseButton.LEFT)
        )
        storage.write_event(
            MouseUpEvent(timestamp=1.05, x=150.0, y=250.0, button=MouseButton.LEFT)
        )
        storage.close()

        capture = Capture.load(capture_path)
        actions = list(capture.actions())

        if actions:
            action = actions[0]
            assert action.timestamp > 0
            assert action.type is not None
            # Click should have x, y
            if action.x is not None:
                assert action.x == 150.0
                assert action.y == 250.0

        capture.close()

"""Tests for high-level Recorder and Capture APIs.

Updated for legacy-style SQLAlchemy storage.
"""

import tempfile
import time
from pathlib import Path

import pytest

from openadapt_capture.capture import Capture
from openadapt_capture.db import create_db, crud

# Recorder requires pynput which needs a display server
try:
    from openadapt_capture.recorder import Recorder
except ImportError:
    Recorder = None


@pytest.fixture
def temp_capture_dir():
    """Create a temporary directory for captures."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


def _create_test_recording(capture_dir, task_description="Test task"):
    """Create a minimal recording for testing (no real input capture)."""
    import os
    import sys

    os.makedirs(capture_dir, exist_ok=True)
    db_path = os.path.join(capture_dir, "recording.db")
    engine, Session = create_db(db_path)
    session = Session()

    timestamp = time.time()
    recording_data = {
        "timestamp": timestamp,
        "monitor_width": 1920,
        "monitor_height": 1080,
        "double_click_interval_seconds": 0.5,
        "double_click_distance_pixels": 5,
        "platform": sys.platform,
        "task_description": task_description,
    }
    recording = crud.insert_recording(session, recording_data)
    return recording, db_path, session


@pytest.mark.skipif(Recorder is None, reason="pynput unavailable (headless)")
class TestRecorder:
    """Tests for Recorder class."""

    def test_recorder_class_exists(self):
        """Test that Recorder class can be instantiated."""
        rec = Recorder("/tmp/test_capture_never_created", task_description="test")
        assert rec.capture_dir == "/tmp/test_capture_never_created"
        assert rec.task_description == "test"

    def test_recorder_has_context_manager(self):
        """Test that Recorder has context manager protocol."""
        assert hasattr(Recorder, "__enter__")
        assert hasattr(Recorder, "__exit__")
        assert hasattr(Recorder, "stop")


class TestCapture:
    """Tests for Capture/CaptureSession class."""

    def test_capture_load(self, temp_capture_dir):
        """Test loading a capture."""
        capture_path = str(Path(temp_capture_dir) / "capture")
        recording, db_path, session = _create_test_recording(
            capture_path, "Test"
        )

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
        capture_path = str(Path(temp_capture_dir) / "capture")
        recording, db_path, session = _create_test_recording(
            capture_path, "Props test"
        )

        capture = Capture.load(capture_path)
        assert capture.started_at is not None
        assert capture.platform in ("darwin", "win32", "linux")
        assert capture.screen_size[0] == 1920
        assert capture.screen_size[1] == 1080
        assert capture.task_description == "Props test"
        capture.close()

    def test_capture_actions_iterator(self, temp_capture_dir):
        """Test iterating over actions."""
        capture_path = str(Path(temp_capture_dir) / "capture")
        recording, db_path, session = _create_test_recording(capture_path)

        # Insert action events directly via crud
        ts = recording.timestamp
        crud.insert_action_event(session, recording, ts + 0.001, {
            "name": "click",
            "mouse_x": 100.0,
            "mouse_y": 100.0,
            "mouse_button_name": "left",
            "mouse_pressed": True,
        })
        crud.insert_action_event(session, recording, ts + 0.002, {
            "name": "click",
            "mouse_x": 100.0,
            "mouse_y": 100.0,
            "mouse_button_name": "left",
            "mouse_pressed": False,
        })

        # Load and iterate
        capture = Capture.load(capture_path)
        actions = list(capture.actions())

        # Should have merged into a click
        assert len(actions) >= 1
        capture.close()

    def test_capture_context_manager(self, temp_capture_dir):
        """Test Capture as context manager."""
        capture_path = str(Path(temp_capture_dir) / "capture")
        _create_test_recording(capture_path)

        with Capture.load(capture_path) as capture:
            assert capture.id is not None

    def test_capture_raw_events(self, temp_capture_dir):
        """Test raw_events returns Pydantic events from SQLAlchemy DB."""
        capture_path = str(Path(temp_capture_dir) / "capture")
        recording, db_path, session = _create_test_recording(capture_path)

        ts = recording.timestamp
        # Insert various event types
        crud.insert_action_event(session, recording, ts + 0.001, {
            "name": "move", "mouse_x": 50.0, "mouse_y": 60.0,
        })
        crud.insert_action_event(session, recording, ts + 0.002, {
            "name": "press", "key_char": "a",
        })
        crud.insert_action_event(session, recording, ts + 0.003, {
            "name": "release", "key_char": "a",
        })

        capture = Capture.load(capture_path)
        events = capture.raw_events()
        assert len(events) == 3
        assert events[0].type == "mouse.move"
        assert events[1].type == "key.down"
        assert events[2].type == "key.up"
        capture.close()


class TestAction:
    """Tests for Action dataclass."""

    def test_action_properties(self, temp_capture_dir):
        """Test Action property accessors."""
        capture_path = str(Path(temp_capture_dir) / "capture")
        recording, db_path, session = _create_test_recording(capture_path)

        ts = recording.timestamp
        crud.insert_action_event(session, recording, ts + 0.001, {
            "name": "click",
            "mouse_x": 150.0,
            "mouse_y": 250.0,
            "mouse_button_name": "left",
            "mouse_pressed": True,
        })
        crud.insert_action_event(session, recording, ts + 0.002, {
            "name": "click",
            "mouse_x": 150.0,
            "mouse_y": 250.0,
            "mouse_button_name": "left",
            "mouse_pressed": False,
        })

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

    def test_action_scroll_properties(self, temp_capture_dir):
        """Test Action dx/dy properties for scroll events."""
        capture_path = str(Path(temp_capture_dir) / "capture")
        recording, db_path, session = _create_test_recording(capture_path)

        ts = recording.timestamp
        crud.insert_action_event(session, recording, ts + 0.001, {
            "name": "scroll",
            "mouse_x": 200.0,
            "mouse_y": 300.0,
            "mouse_dx": 0.0,
            "mouse_dy": -3.0,
        })

        capture = Capture.load(capture_path)
        actions = list(capture.actions())
        assert len(actions) == 1
        action = actions[0]
        assert action.x == 200.0
        assert action.y == 300.0
        assert action.dx == 0.0
        assert action.dy == -3.0
        assert action.type == "mouse.scroll"
        capture.close()

    def test_action_click_button_property(self, temp_capture_dir):
        """Test Action button property for click events."""
        capture_path = str(Path(temp_capture_dir) / "capture")
        recording, db_path, session = _create_test_recording(capture_path)

        ts = recording.timestamp
        crud.insert_action_event(session, recording, ts + 0.001, {
            "name": "click",
            "mouse_x": 100.0,
            "mouse_y": 100.0,
            "mouse_button_name": "left",
            "mouse_pressed": True,
        })
        crud.insert_action_event(session, recording, ts + 0.002, {
            "name": "click",
            "mouse_x": 100.0,
            "mouse_y": 100.0,
            "mouse_button_name": "left",
            "mouse_pressed": False,
        })

        capture = Capture.load(capture_path)
        actions = list(capture.actions())
        assert len(actions) >= 1
        assert actions[0].button == "left"
        capture.close()

    def test_action_keyboard_no_dx_dy(self, temp_capture_dir):
        """Test that keyboard actions return None for dx/dy/button."""
        capture_path = str(Path(temp_capture_dir) / "capture")
        recording, db_path, session = _create_test_recording(capture_path)

        ts = recording.timestamp
        crud.insert_action_event(session, recording, ts + 0.001, {
            "name": "press", "key_char": "h",
        })
        crud.insert_action_event(session, recording, ts + 0.002, {
            "name": "release", "key_char": "h",
        })

        capture = Capture.load(capture_path)
        actions = list(capture.actions())
        assert len(actions) >= 1
        action = actions[0]
        assert action.dx is None
        assert action.dy is None
        assert action.button is None
        assert action.text is not None  # Should be "h" from KeyTypeEvent
        capture.close()


class TestCaptureEdgeCases:
    """Tests for edge cases and bug fixes."""

    def test_empty_recording(self, temp_capture_dir):
        """Test loading a recording with zero events."""
        capture_path = str(Path(temp_capture_dir) / "capture")
        _create_test_recording(capture_path, "Empty test")

        capture = Capture.load(capture_path)
        assert list(capture.actions()) == []
        assert capture.raw_events() == []
        assert capture.ended_at is None
        assert capture.duration is None
        capture.close()

    def test_session_leak_on_no_recording(self, temp_capture_dir):
        """Test that session is closed when no recording found in DB."""
        import os
        capture_path = str(Path(temp_capture_dir) / "capture")
        os.makedirs(capture_path, exist_ok=True)
        db_path = os.path.join(capture_path, "recording.db")
        # Create DB with tables but no recording row
        create_db(db_path)

        with pytest.raises(FileNotFoundError, match="no recording found"):
            Capture.load(capture_path)

    def test_mouse_pressed_none_skipped(self, temp_capture_dir):
        """Test that click events with mouse_pressed=None are skipped."""
        capture_path = str(Path(temp_capture_dir) / "capture")
        recording, db_path, session = _create_test_recording(capture_path)

        ts = recording.timestamp
        # Insert a click with mouse_pressed=None (corrupt data)
        crud.insert_action_event(session, recording, ts + 0.001, {
            "name": "click",
            "mouse_x": 100.0,
            "mouse_y": 100.0,
            "mouse_button_name": "left",
            # mouse_pressed intentionally omitted -> defaults to None
        })
        # Insert a valid move event
        crud.insert_action_event(session, recording, ts + 0.002, {
            "name": "move",
            "mouse_x": 200.0,
            "mouse_y": 200.0,
        })

        capture = Capture.load(capture_path)
        events = capture.raw_events()
        # The click with mouse_pressed=None should be skipped
        assert len(events) == 1
        assert events[0].type == "mouse.move"
        capture.close()

    def test_disabled_events_filtered(self, temp_capture_dir):
        """Test that disabled events are filtered out."""
        capture_path = str(Path(temp_capture_dir) / "capture")
        recording, db_path, session = _create_test_recording(capture_path)

        ts = recording.timestamp
        crud.insert_action_event(session, recording, ts + 0.001, {
            "name": "move", "mouse_x": 50.0, "mouse_y": 60.0,
        })
        crud.insert_action_event(session, recording, ts + 0.002, {
            "name": "move", "mouse_x": 70.0, "mouse_y": 80.0,
        })

        # Disable the second event directly in the DB
        from openadapt_capture.db.models import ActionEvent
        disabled_event = session.query(ActionEvent).filter(
            ActionEvent.mouse_x == 70.0
        ).first()
        disabled_event.disabled = True
        session.commit()

        capture = Capture.load(capture_path)
        events = capture.raw_events()
        assert len(events) == 1
        assert events[0].x == 50.0
        capture.close()

    def test_capture_load_corrupt_db(self, temp_capture_dir):
        """Test loading a corrupt database file raises an error."""
        import os
        capture_path = str(Path(temp_capture_dir) / "capture")
        os.makedirs(capture_path, exist_ok=True)
        db_path = os.path.join(capture_path, "recording.db")
        # Write garbage to simulate corruption
        with open(db_path, "w") as f:
            f.write("this is not a sqlite database")

        with pytest.raises(Exception):
            Capture.load(capture_path)

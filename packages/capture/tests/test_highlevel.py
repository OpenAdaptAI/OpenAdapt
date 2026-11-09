"""Tests for high-level Recorder and Capture APIs.

Updated for legacy-style SQLAlchemy storage.
"""

import multiprocessing
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from openadapt_capture import recorder as recorder_module
from openadapt_capture import video
from openadapt_capture.capture import Capture, CaptureSession, InvalidCaptureEvent
from openadapt_capture.db import create_db, crud
from openadapt_capture.platform import DisplayMetricsUnavailable
from openadapt_capture.recorder import Recorder
from openadapt_capture.terminal import CaptureSealError

# Sessions/engines created by _create_test_recording, released by the
# temp_capture_dir teardown BEFORE the TemporaryDirectory is removed. Both
# must be released: crud.insert_recording ends with session.refresh(), which
# leaves the connection checked out until the session closes, and the engine
# pool keeps the SQLite file handle open until dispose(). On Windows an open
# recording.db makes the rmtree fail with WinError 32 (sharing violation).
_HELPER_SESSIONS = []
_HELPER_ENGINES = []


def _spawn_write_screen_with_explicit_retention(
    db_path: str,
    recording_id: int,
    perf_queue,
) -> None:
    """Spawn target whose imported config keeps the default image posture."""
    from openadapt_capture import utils
    from openadapt_capture.db import get_session_for_path
    from openadapt_capture.db.models import Recording
    from openadapt_capture.recorder import Event, write_screen_event

    utils.set_start_time()
    session = get_session_for_path(db_path)
    try:
        recording = session.get(Recording, recording_id)
        write_screen_event(
            session,
            recording,
            Event(2.0, "screen", Image.new("RGB", (2, 1), "blue"), 1),
            perf_queue,
            record_images=True,
        )
    finally:
        session.close()


@pytest.fixture
def temp_capture_dir():
    """Create a temporary directory for captures."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir
        while _HELPER_SESSIONS:
            _HELPER_SESSIONS.pop().close()
        while _HELPER_ENGINES:
            _HELPER_ENGINES.pop().dispose()


def _create_test_recording(capture_dir, task_description="Test task"):
    """Create a minimal recording for testing (no real input capture)."""
    import os
    import sys

    os.makedirs(capture_dir, exist_ok=True)
    db_path = os.path.join(capture_dir, "recording.db")
    engine, Session = create_db(db_path)
    _HELPER_ENGINES.append(engine)
    session = Session()
    _HELPER_SESSIONS.append(session)

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


class TestRecorder:
    """Tests for Recorder class."""

    def test_recorder_class_exists(self):
        """Test that Recorder class can be instantiated."""
        rec = Recorder("/tmp/test_capture_never_created", task_description="test")
        assert rec.capture_dir.endswith("test_capture_never_created")
        assert rec.task_description == "test"

    def test_recorder_has_context_manager(self):
        """Test that Recorder has context manager protocol."""
        assert hasattr(Recorder, "__enter__")
        assert hasattr(Recorder, "__exit__")
        assert hasattr(Recorder, "stop")

    def test_recorder_accepts_capture_params(self):
        """Test Recorder accepts capture_video, capture_audio, etc."""
        rec = Recorder(
            "/tmp/test_never_created",
            task_description="test",
            capture_video=True,
            capture_audio=False,
            capture_images=True,
            capture_full_video=False,
            plot_performance=False,
        )
        assert rec.task_description == "test"

    def test_explicit_image_retention_crosses_the_spawn_boundary(self, tmp_path):
        db_path = str(tmp_path / "recording.db")
        engine, session_factory = create_db(db_path)
        session = session_factory()
        recording = crud.insert_recording(
            session,
            {
                "timestamp": 1.0,
                "monitor_width": 2,
                "monitor_height": 1,
                "double_click_interval_seconds": 0.5,
                "double_click_distance_pixels": 5,
                "platform": "test",
                "task_description": "spawn override",
            },
        )
        recording_id = recording.id
        session.close()
        engine.dispose()
        context = multiprocessing.get_context("spawn")
        perf_queue = context.Queue()
        process = context.Process(
            target=_spawn_write_screen_with_explicit_retention,
            args=(db_path, recording_id, perf_queue),
        )

        process.start()
        process.join(timeout=30)

        assert process.exitcode == 0
        engine, session_factory = create_db(db_path)
        session = session_factory()
        try:
            screenshot = session.query(crud.Screenshot).one()
            assert screenshot.source_ordinal == 1
            assert screenshot.png_data
        finally:
            session.close()
            engine.dispose()

    def test_recorder_event_count_property(self):
        """Test Recorder has event_count property starting at 0."""
        rec = Recorder("/tmp/test_never_created")
        assert rec.event_count == 0

    def test_recorder_is_recording_property(self):
        """Test Recorder has is_recording property (False before start)."""
        rec = Recorder("/tmp/test_never_created")
        assert rec.is_recording is False

    def test_recorder_stats_property(self):
        """Test Recorder has stats property returning dict."""
        rec = Recorder("/tmp/test_never_created")
        stats = rec.stats
        assert isinstance(stats, dict)
        assert "action_events" in stats
        assert "screen_events" in stats
        assert "video_frames" in stats
        assert "is_recording" in stats
        assert stats["action_events"] == 0

    def test_recorder_wait_for_ready_method(self):
        """Test Recorder has wait_for_ready method."""
        rec = Recorder("/tmp/test_never_created")
        assert callable(rec.wait_for_ready)

    def test_recorder_capture_property_before_recording(self):
        """Test Recorder.capture is None before recording."""
        rec = Recorder("/tmp/test_never_created")
        assert rec.capture is None

    def test_recorder_capture_waits_for_seal_and_uses_verified_loader(
        self,
        monkeypatch,
        tmp_path,
    ):
        rec = Recorder(str(tmp_path / "capture"))
        rec._record_thread = SimpleNamespace(is_alive=lambda: True)
        rec._terminate_processing.set()
        assert rec.capture is None
        capture_dir = tmp_path / "capture"
        capture_dir.mkdir()
        (capture_dir / "recording.db").touch()

        loaded = object()
        monkeypatch.setattr(
            CaptureSession,
            "load_verified",
            classmethod(lambda _cls, _path: loaded),
        )
        rec._finalized_event.set()

        assert rec.capture is loaded

    def test_recorder_capture_does_not_hide_missing_seal_metadata(self, tmp_path):
        capture_dir = tmp_path / "capture"
        capture_dir.mkdir()
        (capture_dir / "recording.db").touch()
        rec = Recorder(str(capture_dir))
        rec._finalized_event.set()

        with pytest.raises(CaptureSealError, match="capture artifact is missing"):
            _ = rec.capture

    def test_recorder_screen_count_property(self):
        """Test Recorder has screen_count property."""
        rec = Recorder("/tmp/test_never_created")
        assert rec.screen_count == 0

    def test_recorder_video_frame_count_property(self):
        """Test Recorder has video_frame_count property."""
        rec = Recorder("/tmp/test_never_created")
        assert rec.video_frame_count == 0

    def test_stop_during_incomplete_startup_returns_promptly(
        self, monkeypatch, tmp_path
    ):
        """A worker that never announces readiness cannot trap context teardown."""
        worker_entered = threading.Event()

        def stalled_record(
            *,
            terminate_processing,
            terminate_recording,
            status_pipe,
            **_kwargs,
        ):
            def wait_for_shutdown():
                worker_entered.set()
                terminate_processing.wait()

            worker = threading.Thread(target=wait_for_shutdown, daemon=True)
            worker.start()
            tasks = {"never_ready": worker}
            started_events = {"never_ready": threading.Event()}

            assert not recorder_module._wait_for_tasks_started(
                tasks,
                started_events,
                terminate_processing,
            )
            assert not recorder_module._join_tasks(
                tasks,
                ["never_ready"],
                timeout=0.5,
            )
            status_pipe.send({"type": "record.stopped"})
            terminate_recording.set()

        monkeypatch.setattr(recorder_module, "record", stalled_record)
        recorder = Recorder(str(tmp_path / "incomplete-startup"))

        with recorder:
            assert worker_entered.wait(timeout=1)
            stop_started = time.monotonic()
            recorder.stop()

        assert time.monotonic() - stop_started < 1
        assert recorder.wait_for_ready(timeout=0) is False

    def test_spawned_video_writer_failure_surfaces_through_recorder(
        self, monkeypatch, tmp_path
    ):
        """A child finalization failure cannot be reported as a stopped capture."""

        def record_with_failed_video_writer(**_kwargs):
            process = multiprocessing.get_context("spawn").Process(
                target=video._run_checked,
                args=(["/definitely-missing-openadapt-ffmpeg"],),
                kwargs={"timeout": 1},
            )
            process.start()
            tasks = {"video_writer": process}
            recorder_module._join_tasks(tasks, ["video_writer"])
            recorder_module._raise_for_failed_processes(tasks)

        monkeypatch.setattr(
            recorder_module,
            "record",
            record_with_failed_video_writer,
        )
        recorder = Recorder(str(tmp_path / "failed-finalization"))

        with pytest.raises(
            RuntimeError,
            match=r"video_writer \(exit code 1\)",
        ):
            with recorder:
                pass


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
        # Create DB with tables but no recording row (dispose the setup
        # engine so only Capture.load's own handle is under test)
        engine, _ = create_db(db_path)
        engine.dispose()

        with pytest.raises(FileNotFoundError, match="no recording found"):
            Capture.load(capture_path)

    def test_mouse_pressed_none_is_refused(self, temp_capture_dir):
        """A corrupt click cannot disappear from the replay event stream."""
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
        with pytest.raises(InvalidCaptureEvent, match="pressed/released"):
            capture.raw_events()
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


class TestRecordingConfig:
    """Tests for RecordingConfig and config_override."""

    def test_config_override_applies_and_restores(self):
        """Test that config_override patches and restores config."""
        from openadapt_capture.config import (
            RecordingConfig,
            config,
            config_override,
        )

        original_video = config.RECORD_VIDEO
        original_audio = config.RECORD_AUDIO
        original_plot = config.PLOT_PERFORMANCE

        rc = RecordingConfig(
            capture_video=False,
            capture_audio=True,
            plot_performance=True,
        )
        with config_override(rc):
            assert config.RECORD_VIDEO is False
            assert config.RECORD_AUDIO is True
            assert config.PLOT_PERFORMANCE is True

        # Restored
        assert config.RECORD_VIDEO == original_video
        assert config.RECORD_AUDIO == original_audio
        assert config.PLOT_PERFORMANCE == original_plot

    def test_config_override_none_values_unchanged(self):
        """Test that None values in RecordingConfig don't change config."""
        from openadapt_capture.config import (
            RecordingConfig,
            config,
            config_override,
        )

        original_video = config.RECORD_VIDEO
        rc = RecordingConfig()  # all None
        with config_override(rc):
            assert config.RECORD_VIDEO == original_video

    def test_config_override_restores_on_exception(self):
        """Test that config is restored even if body raises."""
        from openadapt_capture.config import (
            RecordingConfig,
            config,
            config_override,
        )

        original_video = config.RECORD_VIDEO
        rc = RecordingConfig(capture_video=not original_video)

        with pytest.raises(ValueError):
            with config_override(rc):
                assert config.RECORD_VIDEO != original_video
                raise ValueError("test")

        assert config.RECORD_VIDEO == original_video


class TestPixelRatio:
    """Tests for pixel_ratio persistence on the SQLAlchemy Recording model."""

    def test_pixel_ratio_round_trips_through_model(self, temp_capture_dir):
        """A HiDPI pixel_ratio written to the model survives load."""
        import os
        import sqlite3
        import sys

        capture_path = str(Path(temp_capture_dir) / "capture")
        os.makedirs(capture_path, exist_ok=True)
        db_path = os.path.join(capture_path, "recording.db")
        engine, Session = create_db(db_path)
        session = Session()
        crud.insert_recording(
            session,
            {
                "timestamp": time.time(),
                "monitor_width": 3840,
                "monitor_height": 2160,
                "pixel_ratio": 2.0,
                "double_click_interval_seconds": 0.5,
                "double_click_distance_pixels": 5,
                "platform": sys.platform,
                "task_description": "HiDPI",
            },
        )
        session.close()
        engine.dispose()

        # The column is real: it is present in the on-disk schema.
        con = sqlite3.connect(db_path)
        cols = {row[1] for row in con.execute("PRAGMA table_info(recording)")}
        con.close()
        assert "pixel_ratio" in cols

        capture = Capture.load(capture_path)
        assert capture.pixel_ratio == 2.0
        capture.close()

    def test_old_recording_without_column_loads(self, temp_capture_dir):
        """A recording.db predating the pixel_ratio column still loads.

        The additive migration adds the column (NULL for existing rows) so
        the query does not fail, and pixel_ratio falls back to the config
        JSON, then to 1.0 when genuinely unknown.
        """
        import os
        import sqlite3
        import sys

        # config-JSON fallback preserved.
        capture_path = str(Path(temp_capture_dir) / "old_with_config")
        os.makedirs(capture_path, exist_ok=True)
        db_path = os.path.join(capture_path, "recording.db")
        engine, Session = create_db(db_path)
        session = Session()
        crud.insert_recording(
            session,
            {
                "timestamp": time.time(),
                "monitor_width": 2560,
                "monitor_height": 1440,
                "double_click_interval_seconds": 0.5,
                "double_click_distance_pixels": 5,
                "platform": sys.platform,
                "task_description": "old",
                "config": {"pixel_ratio": 1.5},
            },
        )
        session.close()
        engine.dispose()

        # Simulate an old DB that predates the column by dropping it.
        con = sqlite3.connect(db_path)
        con.execute("ALTER TABLE recording DROP COLUMN pixel_ratio")
        con.commit()
        con.close()

        capture = Capture.load(capture_path)
        assert capture.pixel_ratio == 1.5  # recovered from config JSON
        capture.close()

        # No column and no config -> genuinely unknown -> refuse.
        capture_path2 = str(Path(temp_capture_dir) / "old_no_config")
        os.makedirs(capture_path2, exist_ok=True)
        db_path2 = os.path.join(capture_path2, "recording.db")
        engine2, Session2 = create_db(db_path2)
        session2 = Session2()
        crud.insert_recording(
            session2,
            {
                "timestamp": time.time(),
                "monitor_width": 1920,
                "monitor_height": 1080,
                "double_click_interval_seconds": 0.5,
                "double_click_distance_pixels": 5,
                "platform": sys.platform,
                "task_description": "old2",
            },
        )
        session2.close()
        engine2.dispose()

        con = sqlite3.connect(db_path2)
        con.execute("ALTER TABLE recording DROP COLUMN pixel_ratio")
        con.commit()
        con.close()

        capture2 = Capture.load(capture_path2)
        with pytest.raises(DisplayMetricsUnavailable):
            _ = capture2.pixel_ratio
        capture2.close()

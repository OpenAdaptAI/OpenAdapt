"""Virtual-desktop coordinate and persistence contracts."""

from __future__ import annotations

import json
import queue
import threading
import time
from types import SimpleNamespace

import pytest
from PIL import Image

import openadapt_capture.recorder as recorder_module
from openadapt_capture import utils
from openadapt_capture.capture import CaptureSession
from openadapt_capture.db import create_db, crud
from openadapt_capture.desktop_capture import DesktopCaptureError, DesktopCaptureScope
from openadapt_capture.recorder import (
    NativeInputFrameBoundary,
    OrderedEventJournal,
    create_recording,
    read_screen_events,
    trigger_action_event,
)


def _two_monitor_scope() -> DesktopCaptureScope:
    return DesktopCaptureScope.from_monitors(
        [
            {"left": -1920, "top": 0, "width": 4480, "height": 1440},
            {"left": -1920, "top": 0, "width": 1920, "height": 1080},
            {"left": 0, "top": 0, "width": 2560, "height": 1440},
        ]
    )


def test_multiple_monitor_scope_translates_negative_global_origin() -> None:
    scope = _two_monitor_scope()

    assert scope.translate(-1920, 0) == (0, 0)
    assert scope.translate(-1, 100) == (1919, 100)
    assert scope.translate(0, 100) == (1920, 100)
    assert scope.translate(2559, 1439) == (4479, 1439)


def test_live_scope_rejects_same_size_origin_and_layout_change() -> None:
    original = [
        {"left": -1920, "top": 0, "width": 4480, "height": 1440},
        {"left": -1920, "top": 0, "width": 1920, "height": 1080},
        {"left": 0, "top": 0, "width": 2560, "height": 1440},
    ]
    current = list(original)
    scope = DesktopCaptureScope.from_monitors(
        original,
        topology_reader=lambda: current,
    )
    assert scope.translate(-100, 50) == (1820, 50)

    # The combined viewport keeps the same size. Only the global origin and
    # physical layout move. A video-frame dimension check cannot detect this.
    current = [
        {"left": 0, "top": 0, "width": 4480, "height": 1440},
        {"left": 0, "top": 0, "width": 1920, "height": 1080},
        {"left": 1920, "top": 0, "width": 2560, "height": 1440},
    ]

    with pytest.raises(DesktopCaptureError, match="topology changed"):
        scope.assert_current(force=True)


def test_multiple_monitor_snapshot_is_privacy_safe_geometry() -> None:
    snapshot = _two_monitor_scope().snapshot()
    assert snapshot == {
        "schema_version": "openadapt.capture.display-topology/v1",
        "coordinate_space": "virtual_desktop_pixels",
        "origin": [-1920, 0],
        "viewport": [4480, 1440],
        "monitor_count": 2,
        "monitors": [
            [-1920, 0, 1920, 1080],
            [0, 0, 2560, 1440],
        ],
        "topology_sha256": snapshot["topology_sha256"],
    }
    assert len(snapshot["topology_sha256"]) == 64


def test_desktop_scope_rejects_missing_physical_monitor() -> None:
    with pytest.raises(DesktopCaptureError, match="physical monitor"):
        DesktopCaptureScope.from_monitors([{"left": 0, "top": 0, "width": 1920, "height": 1080}])


@pytest.mark.parametrize("value", [True, 1.5, "1"])
def test_desktop_scope_rejects_coerced_geometry(value) -> None:
    with pytest.raises(DesktopCaptureError, match="must be an integer"):
        DesktopCaptureScope.from_monitors(
            [
                {"left": 0, "top": 0, "width": 1920, "height": 1080},
                {"left": value, "top": 0, "width": 1920, "height": 1080},
            ]
        )


def test_desktop_scope_rejects_inconsistent_combined_bounds() -> None:
    with pytest.raises(DesktopCaptureError, match="do not span"):
        DesktopCaptureScope.from_monitors(
            [
                {"left": 0, "top": 0, "width": 3840, "height": 1080},
                {"left": 0, "top": 0, "width": 1920, "height": 1080},
            ]
        )


def test_action_event_uses_virtual_desktop_pixels() -> None:
    import queue

    utils.set_start_time()
    events = queue.Queue()
    trigger_action_event(
        events,
        {"name": "click", "mouse_x": -100.0, "mouse_y": 50.0},
        _two_monitor_scope(),
    )

    event = events.get_nowait()
    assert event.data["mouse_x"] == 1820.0
    assert event.data["mouse_y"] == 50.0


def test_desktop_capture_metadata_round_trips(tmp_path) -> None:
    capture_dir = tmp_path / "capture"
    capture_dir.mkdir()
    engine, session_factory = create_db(str(capture_dir / "recording.db"))
    session = session_factory()
    snapshot = _two_monitor_scope().snapshot()
    crud.insert_recording(
        session,
        {
            "timestamp": 1.0,
            "monitor_width": 4480,
            "monitor_height": 1440,
            "double_click_interval_seconds": 0.5,
            "double_click_distance_pixels": 5,
            "platform": "test",
            "task_description": "two monitors",
            "config": json.loads(json.dumps({"capture_desktop": snapshot})),
        },
    )
    session.close()
    engine.dispose()

    with CaptureSession.load(capture_dir) as capture:
        assert capture.desktop_capture == snapshot
        assert capture.window_capture is None


def test_recording_rejects_ambiguous_coordinate_scopes(tmp_path) -> None:
    with pytest.raises(ValueError, match="both window and desktop"):
        create_recording(
            "ambiguous",
            str(tmp_path / "capture"),
            window_capture_info={"coordinate_space": "window_pixels"},
            desktop_capture_info={"coordinate_space": "virtual_desktop_pixels"},
        )


def test_desktop_screen_reader_discards_a_frame_crossed_by_native_input(
    monkeypatch,
) -> None:
    monkeypatch.setattr(recorder_module.config, "SCREEN_CAPTURE_FPS", 0)
    boundary = NativeInputFrameBoundary()
    clean_results = iter((False, True))
    completed_tokens: list[object] = []
    terminate = threading.Event()

    class FakeObserver:
        def begin_frame_capture(self) -> object:
            return object()

        def finish_frame_capture(self, _token: object) -> bool:
            return next(clean_results)

        def complete_frame_capture(self, token: object) -> None:
            completed_tokens.append(token)

    boundary.attach(FakeObserver())
    capture_calls = 0

    def take_screenshot() -> Image.Image:
        nonlocal capture_calls
        capture_calls += 1
        if capture_calls == 2:
            terminate.set()
        return Image.new("RGB", (4480, 1440), "black")

    monkeypatch.setattr(recorder_module.utils, "take_screenshot", take_screenshot)
    journal = OrderedEventJournal()
    read_screen_events(
        journal,
        terminate,
        SimpleNamespace(timestamp=time.time()),
        threading.Event(),
        desktop_scope=_two_monitor_scope(),
        input_frame_boundary=boundary,
    )

    assert capture_calls == 2
    assert len(completed_tokens) == 2
    frame = journal.get_nowait()
    assert frame.type == "screen"
    assert frame.source_ordinal == 1
    with pytest.raises(queue.Empty):
        journal.get_nowait()


def test_startup_failure_wakes_a_screen_reader_waiting_for_observer_attach() -> None:
    boundary = NativeInputFrameBoundary()
    terminate = threading.Event()
    terminal_cancelled = threading.Event()
    journal = OrderedEventJournal()
    errors: list[BaseException] = []

    def run_reader() -> None:
        try:
            read_screen_events(
                journal,
                terminate,
                SimpleNamespace(timestamp=time.time()),
                threading.Event(),
                desktop_scope=_two_monitor_scope(),
                input_frame_boundary=boundary,
                terminal_frame_cancelled=terminal_cancelled,
            )
        except BaseException as exc:
            errors.append(exc)

    reader = threading.Thread(target=run_reader)
    reader.start()
    time.sleep(0.01)

    failure = RuntimeError("observer setup timed out")
    terminal_cancelled.set()
    boundary.fail(failure)
    terminate.set()
    reader.join(timeout=1)

    assert not reader.is_alive()
    assert errors == [failure]
    assert journal.empty()


def test_desktop_terminal_frame_seals_input_before_journal_commit(
    monkeypatch,
) -> None:
    terminate = threading.Event()
    terminate.set()
    input_finished = threading.Event()
    input_finished.set()
    terminal_finished = threading.Event()
    boundary = NativeInputFrameBoundary()
    sealed = False
    completed = False

    class FakeObserver:
        def begin_frame_capture(self) -> object:
            return object()

        def finish_frame_capture(self, _token: object) -> bool:
            return True

        def seal_frame_capture(self, _token: object) -> None:
            nonlocal sealed
            sealed = True

        def complete_frame_capture(self, _token: object) -> None:
            nonlocal completed
            completed = True

    boundary.attach(FakeObserver())
    monkeypatch.setattr(
        recorder_module.utils,
        "take_screenshot",
        lambda: Image.new("RGB", (4480, 1440), "black"),
    )
    journal = OrderedEventJournal()
    original_put = journal.put

    def checked_put(*args, **kwargs):
        assert sealed
        return original_put(*args, **kwargs)

    monkeypatch.setattr(journal, "put", checked_put)
    read_screen_events(
        journal,
        terminate,
        SimpleNamespace(timestamp=time.time()),
        threading.Event(),
        desktop_scope=_two_monitor_scope(),
        input_finished=input_finished,
        input_frame_boundary=boundary,
        terminal_frame_finished=terminal_finished,
    )

    assert terminal_finished.is_set()
    assert completed
    assert journal.get_nowait().type == "screen"

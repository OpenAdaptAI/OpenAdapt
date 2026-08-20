"""Virtual-desktop coordinate and persistence contracts."""

from __future__ import annotations

import json

import pytest

from openadapt_capture import utils
from openadapt_capture.capture import CaptureSession
from openadapt_capture.db import create_db, crud
from openadapt_capture.desktop_capture import DesktopCaptureError, DesktopCaptureScope
from openadapt_capture.recorder import create_recording, trigger_action_event


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
    assert _two_monitor_scope().snapshot() == {
        "coordinate_space": "virtual_desktop_pixels",
        "origin": [-1920, 0],
        "viewport": [4480, 1440],
        "monitor_count": 2,
        "monitors": [
            [-1920, 0, 1920, 1080],
            [0, 0, 2560, 1440],
        ],
    }


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

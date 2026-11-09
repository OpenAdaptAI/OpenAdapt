"""Full-screen video startup must size the stream without a framebuffer read.

OpenAdaptAI/OpenAdapt#1883: in a macOS VM guest, ``take_screenshot()`` inside
``video_pre_callback`` blocked for ~28 s and blew the writer startup budget.
Window-scoped recording already passes ``frame_size`` and avoids the path.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from openadapt_capture import platform as capture_platform
from openadapt_capture import utils
from openadapt_capture.recorder import video_pre_callback
from openadapt_capture import video as video_module


def _recording() -> SimpleNamespace:
    return SimpleNamespace(id=1, timestamp=1_700_000_000.0)


def test_video_pre_callback_uses_screen_dimensions_not_screenshot(monkeypatch, tmp_path):
    """Full-screen sizing reads platform metrics; it must not grab the screen."""
    calls: dict[str, object] = {"screenshot": 0, "dims": None, "writer_size": None}

    def boom_screenshot():
        calls["screenshot"] = int(calls["screenshot"]) + 1
        raise AssertionError("take_screenshot must not run on the happy path")

    def fake_dims():
        calls["dims"] = (3200, 1800)
        return 3200, 1800

    def fake_init(path, width, height, **kwargs):
        calls["writer_size"] = (width, height)
        return MagicMock(name="container"), MagicMock(name="stream"), 12.5

    monkeypatch.setattr(
        "openadapt_capture.recorder.utils.take_screenshot", boom_screenshot
    )
    monkeypatch.setattr(
        "openadapt_capture.recorder.platform.get_screen_dimensions", fake_dims
    )
    monkeypatch.setattr(
        "openadapt_capture.recorder.video.initialize_video_writer", fake_init
    )
    monkeypatch.setattr(
        "openadapt_capture.recorder.crud.update_video_start_time", lambda *a, **k: None
    )

    state = video_pre_callback(
        db=MagicMock(),
        recording=_recording(),
        video_dir=str(tmp_path),
        provision=video_module.FFmpegProvision(
            executable="ffmpeg-unused",
            codec="mpeg4",
            pixel_format="yuv420p",
            muxer="mp4",
            source="test",
        ),
        timeout_seconds=5.0,
    )

    assert calls["screenshot"] == 0
    assert calls["dims"] == (3200, 1800)
    assert calls["writer_size"] == (3200, 1800)
    assert state["video_start_timestamp"] == 12.5


def test_video_pre_callback_falls_back_to_screenshot_when_metrics_unavailable(
    monkeypatch, tmp_path
):
    """When metrics cannot be measured, keep the previous screenshot size path."""
    from PIL import Image

    calls: dict[str, object] = {"writer_size": None}

    monkeypatch.setattr(
        "openadapt_capture.recorder.platform.get_screen_dimensions",
        lambda: (_ for _ in ()).throw(
            capture_platform.DisplayMetricsUnavailable("probe failed")
        ),
    )
    monkeypatch.setattr(
        "openadapt_capture.recorder.utils.take_screenshot",
        lambda: Image.new("RGB", (640, 480)),
    )

    def fake_init(path, width, height, **kwargs):
        calls["writer_size"] = (width, height)
        return MagicMock(), MagicMock(), 1.0

    monkeypatch.setattr(
        "openadapt_capture.recorder.video.initialize_video_writer", fake_init
    )
    monkeypatch.setattr(
        "openadapt_capture.recorder.crud.update_video_start_time", lambda *a, **k: None
    )

    video_pre_callback(
        db=MagicMock(),
        recording=_recording(),
        video_dir=str(tmp_path),
        provision=video_module.FFmpegProvision(
            executable="ffmpeg-unused",
            codec="mpeg4",
            pixel_format="yuv420p",
            muxer="mp4",
            source="test",
        ),
        timeout_seconds=5.0,
    )
    assert calls["writer_size"] == (640, 480)


def test_explicit_frame_size_still_wins(monkeypatch, tmp_path):
    """Window-scoped callers keep supplying the window's pixel size."""
    calls: dict[str, object] = {"writer_size": None, "dims": 0, "screenshot": 0}

    monkeypatch.setattr(
        "openadapt_capture.recorder.platform.get_screen_dimensions",
        lambda: calls.__setitem__("dims", int(calls["dims"]) + 1) or (1, 1),
    )
    monkeypatch.setattr(
        "openadapt_capture.recorder.utils.take_screenshot",
        lambda: calls.__setitem__("screenshot", int(calls["screenshot"]) + 1),
    )

    def fake_init(path, width, height, **kwargs):
        calls["writer_size"] = (width, height)
        return MagicMock(), MagicMock(), 0.0

    monkeypatch.setattr(
        "openadapt_capture.recorder.video.initialize_video_writer", fake_init
    )
    monkeypatch.setattr(
        "openadapt_capture.recorder.crud.update_video_start_time", lambda *a, **k: None
    )

    video_pre_callback(
        db=MagicMock(),
        recording=_recording(),
        video_dir=str(tmp_path),
        frame_size=(111, 222),
        provision=video_module.FFmpegProvision(
            executable="ffmpeg-unused",
            codec="mpeg4",
            pixel_format="yuv420p",
            muxer="mp4",
            source="test",
        ),
        timeout_seconds=5.0,
    )
    assert calls["writer_size"] == (111, 222)
    assert calls["dims"] == 0
    assert calls["screenshot"] == 0


@pytest.mark.skipif(sys.platform != "darwin", reason="Quartz main-display probe")
def test_darwin_screen_dimensions_match_screenshot_on_single_display():
    """Quartz pixel mode size must equal a real grab on a one-display host."""
    import mss

    with mss.mss() as sct:
        # monitors[0] is the virtual desktop; len==2 means one real display.
        if len(sct.monitors) != 2:
            pytest.skip("multi-monitor hosts need a virtual-desktop probe")

    dims = capture_platform.get_screen_dimensions()
    shot = utils.take_screenshot()
    assert dims == shot.size

    ratio = capture_platform.get_display_pixel_ratio()
    logical_w, _ = utils.get_monitor_dims()
    assert abs(logical_w * ratio - dims[0]) < 1.0

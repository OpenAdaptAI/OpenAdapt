"""Normal recorder startup sizes video from its retained initial source frame.

This is an orchestration component test, stopping at the video worker start.
Native MSS acquisition and task scheduling are synthetic; screenshot conversion,
desktop scope, journal, received video callback and writer size checks are real.
No desktop, input listener, encoder subprocess or completed recording is claimed.
"""

from __future__ import annotations

import queue
import threading
from types import SimpleNamespace

import pytest
from PIL import Image

from openadapt_capture import recorder, utils, video
from openadapt_capture.desktop_capture import DesktopCaptureScope


class ReachedVideoWriter(BaseException):
    """End the modeled startup after the genuine writer accepted the frame."""


@pytest.mark.parametrize(
    "monitors",
    [
        [
            {"left": 0, "top": 0, "width": 80, "height": 20},
            {"left": 0, "top": 0, "width": 40, "height": 20},
            {"left": 40, "top": 0, "width": 40, "height": 20},
        ],
        [
            {"left": -40, "top": 0, "width": 80, "height": 20},
            {"left": -40, "top": 0, "width": 40, "height": 20},
            {"left": 0, "top": 0, "width": 40, "height": 20},
        ],
        [
            {"left": 0, "top": -20, "width": 40, "height": 40},
            {"left": 0, "top": -20, "width": 40, "height": 20},
            {"left": 0, "top": 0, "width": 40, "height": 20},
        ],
        [
            {"left": 0, "top": 0, "width": 60, "height": 40},
            {"left": 0, "top": 0, "width": 20, "height": 40},
            {"left": 20, "top": 0, "width": 40, "height": 20},
        ],
        [
            {"left": 0, "top": 0, "width": 40, "height": 20},
            {"left": 0, "top": 0, "width": 40, "height": 20},
        ],
    ],
    ids=["horizontal", "negative-origin", "vertical", "unequal-extents", "single"],
)
def test_normal_startup_reuses_retained_desktop_frame(monkeypatch, tmp_path, monitors):
    expected_size = (monitors[0]["width"], monitors[0]["height"])
    primary_size = (monitors[1]["width"], monitors[1]["height"])
    counts = {"grabs": 0, "metrics": 0}
    scope = DesktopCaptureScope.from_monitors(monitors, topology_reader=lambda: monitors)
    journal = recorder.OrderedEventJournal()
    receipt = {}
    local_queues = []

    class SyntheticMSS:
        def __init__(self):
            self.monitors = monitors

        def grab(self, monitor):
            assert monitor is self.monitors[0]
            counts["grabs"] += 1
            assert counts["grabs"] == 1, "startup performed an extra framebuffer read"
            return SimpleNamespace(
                size=expected_size,
                bgra=b"\x00\x00\x00\xff" * (expected_size[0] * expected_size[1]),
            )

    def primary_metrics():
        counts["metrics"] += 1
        return primary_size

    class LocalQueue(queue.Queue):
        def __init__(self):
            super().__init__()
            self.closed = False
            local_queues.append(self)

        def close(self):
            self.closed = True

        def join_thread(self):
            assert self.closed

        def cancel_join_thread(self):
            raise AssertionError("synthetic local queue has no feeder to abandon")

    class InertTask:
        def __init__(self, *, target=None, args=(), daemon=False):
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self):
            # Real queue-release code can join these owned no-thread fixtures.
            if getattr(self.target, "__name__", None) == "join_thread":
                self.target()

        def is_alive(self):
            return False

        def join(self, timeout=None):
            pass

    class ModeledProcess(InertTask):
        def start(self):
            if not self.args or self.args[0] != "screen/video":
                return
            callback = self.args[9]
            state = callback(None, self.args[5])
            stage = state["video_container"]
            encoded = []
            monkeypatch.setattr(
                stage, "_enqueue_frames", lambda frame, count: encoded.append(count)
            )
            first = journal.get_nowait()
            assert first.type == "screen"
            assert first.data.size == expected_size
            stage.stage_frame(
                first.data,
                0,
                capture_timestamp=first.timestamp,
                source_ordinal=first.source_ordinal,
            )
            receipt["writer_size"] = (stage.stream.width, stage.stream.height)
            receipt["encoded"] = encoded
            # A subsequent extent change still refuses; no padding or resizing.
            changed = Image.new("RGB", (expected_size[0] + 2, expected_size[1]))
            with pytest.raises(video.FFmpegEncodingError, match="does not match"):
                stage.stage_frame(changed, 1, capture_timestamp=2.0, source_ordinal=2)
            raise ReachedVideoWriter()

    for name, value in {
        "RECORD_BROWSER_EVENTS": False,
        "RECORD_VIDEO": True,
        "RECORD_IMAGES": True,
        "RECORD_AUDIO": False,
        "RECORD_WINDOW_DATA": False,
        "RECORD_WINDOW_OWNER": "",
        "RECORD_WINDOW_TITLE": "",
        "PLOT_PERFORMANCE": False,
    }.items():
        monkeypatch.setattr(recorder.config, name, value)
    provision = video.FFmpegProvision(
        executable="synthetic-unused",
        codec="mpeg4",
        pixel_format="yuv420p",
        muxer="mp4",
        source="synthetic-startup-contract",
    )
    monkeypatch.setattr(video, "require_video_encoder", lambda **kwargs: provision)
    monkeypatch.setattr(recorder, "build_window_scope", lambda *args, **kwargs: None)
    monkeypatch.setattr(recorder.DesktopCaptureScope, "current", lambda: scope)
    monkeypatch.setattr(recorder, "OrderedEventJournal", lambda: journal)
    monkeypatch.setattr(recorder, "create_structural_observer", lambda **kwargs: None)
    monkeypatch.setattr(
        recorder,
        "create_recording",
        lambda *args, **kwargs: (SimpleNamespace(id=1, timestamp=1.0), str(tmp_path / "unused.db")),
    )
    monkeypatch.setattr(recorder.crud, "update_video_start_time", lambda *args: None)
    monkeypatch.setattr(utils, "get_timestamp", lambda: 1.0)
    monkeypatch.setattr(utils, "get_process_local_sct", lambda: SyntheticMSS())
    monkeypatch.setattr(recorder.platform, "get_screen_dimensions", primary_metrics)
    monkeypatch.setattr(recorder.sq, "SynchronizedQueue", LocalQueue)
    monkeypatch.setattr(recorder.threading, "Thread", InertTask)
    monkeypatch.setattr(recorder.multiprocessing, "Process", ModeledProcess)
    monkeypatch.setattr(recorder.multiprocessing, "SimpleQueue", queue.SimpleQueue)
    with pytest.raises(ReachedVideoWriter):
        recorder.record(
            "synthetic stream geometry",
            capture_dir=str(tmp_path),
            terminate_processing=threading.Event(),
            log_memory=False,
            num_action_events=SimpleNamespace(value=0),
            num_screen_events=SimpleNamespace(value=0),
            num_window_events=SimpleNamespace(value=0),
            num_browser_events=SimpleNamespace(value=0),
            num_video_events=SimpleNamespace(value=0),
        )
    assert counts == {"grabs": 1, "metrics": 0}
    assert receipt["writer_size"] == expected_size
    assert receipt["encoded"] == [1]
    assert local_queues and all(pending.closed for pending in local_queues)

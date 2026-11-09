"""The recorder publishes retained events to an opt-in live tap, never frames."""

from __future__ import annotations

import inspect
import multiprocessing
import queue
import threading
from types import SimpleNamespace

from PIL import Image

import openadapt_capture.recorder as recorder_module
from openadapt_capture.live import LiveEventTap
from openadapt_capture.recorder import Event


def _run(tap):
    from openadapt_capture.config import RecordingConfig, config_override

    images = [Image.new("RGB", (4, 3), color) for color in ("red", "green", "blue")]
    journal = queue.Queue()
    journal.put(Event(1.0, "screen", images[0], 1))
    journal.put(Event(2.0, "screen", images[1], 2))
    journal.put(Event(3.0, "action", {"name": "mouse.down"}, 3))
    journal.put(Event(4.0, "screen", images[2], 4))
    queues = [queue.Queue() for _ in range(6)]
    counters = [multiprocessing.Value("i", 0) for _ in range(5)]
    producers_finished = threading.Event()
    producers_finished.set()
    with config_override(
        RecordingConfig(
            capture_video=True,
            capture_images=False,
            capture_full_video=False,
            capture_window_data=False,
        )
    ):
        recorder_module.process_events(
            journal,
            *queues,
            SimpleNamespace(timestamp=0.0),
            threading.Event(),
            threading.Event(),
            *counters,
            producers_finished,
            live_event_tap=tap,
        )
    return queues


def test_retained_actions_reach_the_tap_and_frames_never_do():
    seen = []
    tap = LiveEventTap(seen.append).start()
    queues = _run(tap)
    tap.close()
    assert [(e.type, e.timestamp) for e in seen] == [("action", 3.0)]
    assert seen[0].data["name"] == "mouse.down"
    # The recording itself is unchanged.
    assert queues[1].get_nowait().data["name"] == "mouse.down"


def test_without_a_tap_nothing_is_published():
    queues = _run(None)
    assert queues[1].get_nowait().data["name"] == "mouse.down"


def test_recorder_entry_points_accept_a_tap_defaulting_to_none():
    for fn in (recorder_module.record, recorder_module.Recorder.__init__):
        param = inspect.signature(fn).parameters["live_event_tap"]
        assert param.default is None

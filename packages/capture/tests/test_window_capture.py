"""Tests for window-scoped recording (openadapt_capture/window_capture.py).

Unit tests run everywhere with NO display: the platform resolver/capturer are
injected fakes, so coordinate translation, bounds-timeline tracking, config
plumbing, and persistence are all exercised headless.

The live smoke test (TestWindowCaptureLive) captures a REAL window and is
gated like the input-injection tests in tests/test_performance.py: marked
'slow', skipped on unsupported platforms and when
OPENADAPT_CI_NO_INPUT_INJECTION=1 (hosted-runner session limitation). Run it
on an interactive macOS/Windows desktop (e.g. the Parallels rig):

    OPENADAPT_WINDOW_SMOKE_OWNER=Parallels pytest tests/test_window_capture.py -m slow
"""

import multiprocessing
import os
import queue
import sys
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from PIL import Image

import openadapt_capture.recorder as recorder_module
import openadapt_capture.window_capture as window_capture_module
from openadapt_capture.capture import CaptureSession
from openadapt_capture.db import create_db, crud
from openadapt_capture.desktop_capture import DesktopCaptureScope
from openadapt_capture.events import WindowCaptureStateV2, window_geometry_epoch_sha256
from openadapt_capture.input_observer import ObservedMouseButton, ThreadedInputObserver
from openadapt_capture.recorder import (
    Event,
    NativeInputFrameBoundary,
    OrderedEventJournal,
    Recorder,
    WindowScopedFrame,
    read_input_events,
    read_screen_events,
)
from openadapt_capture.window_capture import (
    TargetWindow,
    WindowCaptureAmbiguousError,
    WindowCaptureError,
    WindowCapturePermissionError,
    WindowCaptureScope,
    WindowTarget,
    build_window_scope,
    translate_point,
)


def test_record_closes_window_scope_when_startup_fails(monkeypatch, tmp_path):
    """A preflight stream must not survive a later recorder setup failure."""

    class FakeWindowScope:
        def __init__(self):
            self.close_calls = 0

        def bind_display_topology(self, _snapshot, _guard):
            return None

        def capture_frame(self, *, publish=False):
            assert publish is False
            return Image.new("RGB", (100, 80), "white"), True

        def snapshot(self):
            return {
                "window_id": 42,
                "capture_source": "test-exact-window-stream",
                "visibility_independent": True,
            }

        def close(self):
            self.close_calls += 1

    fake_scope = FakeWindowScope()
    fake_display_scope = SimpleNamespace(
        snapshot=lambda: {"topology_sha256": "test-topology"},
        assert_current=lambda **_kwargs: None,
    )
    monkeypatch.setattr(recorder_module.config, "RECORD_BROWSER_EVENTS", False)
    monkeypatch.setattr(recorder_module.config, "RECORD_VIDEO", False)
    monkeypatch.setattr(recorder_module.config, "RECORD_IMAGES", True)
    monkeypatch.setattr(
        recorder_module,
        "build_window_scope",
        lambda *_args, **_kwargs: fake_scope,
    )
    monkeypatch.setattr(
        recorder_module.DesktopCaptureScope,
        "current",
        lambda: fake_display_scope,
    )
    monkeypatch.setattr(
        recorder_module,
        "create_recording",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("setup failed")),
    )

    with pytest.raises(RuntimeError, match="setup failed"):
        recorder_module.record(
            "startup failure cleanup",
            capture_dir=str(tmp_path),
            structural_observer=object(),
        )

    assert fake_scope.close_calls == 1


# ---------------------------------------------------------------------------
# translate_point: exact inverse of flow's replay mapping
# ---------------------------------------------------------------------------


def test_journal_orders_concurrent_observations_by_reservation_not_timestamp():
    journal = OrderedEventJournal()
    later_clock = journal.reserve(20.0)
    earlier_clock = journal.reserve(10.0)

    earlier_clock.complete(Event(10.0, "action", {}))
    later_clock.complete(Event(20.0, "action", {}))

    first = journal.get_nowait()
    second = journal.get_nowait()
    assert (first.timestamp, first.source_ordinal) == (20.0, 1)
    assert (second.timestamp, second.source_ordinal) == (10.0, 2)


def test_action_reservation_cannot_bind_a_later_frame_generation(scope, fake, monkeypatch):
    scope.capture_frame()
    journal = OrderedEventJournal()
    translation_entered = threading.Event()
    allow_translation = threading.Event()
    original_translate = scope.translate_with_generation

    def blocked_translate(x, y):
        binding = original_translate(x, y)
        translation_entered.set()
        assert allow_translation.wait(timeout=5)
        return binding

    monkeypatch.setattr(scope, "translate_with_generation", blocked_translate)
    action_result = {}

    def reserve_action():
        reservation, binding = journal.reserve_window_action(1.0, scope, 310.0, 170.0)
        action_result["binding"] = binding
        reservation.complete(Event(1.0, "action", {"window_geometry_generation": binding[2]}))

    action_thread = threading.Thread(target=reserve_action)
    action_thread.start()
    assert translation_entered.wait(timeout=5)

    fake.bounds = (500.0, 250.0, 800.0, 600.0)
    image, _ = scope.capture_frame(publish=False)
    generation = scope.current_generation()
    frame_thread = threading.Thread(
        target=lambda: journal.commit_window_frame(
            Event(
                2.0,
                "screen",
                WindowScopedFrame(
                    image=image,
                    window_event_data=scope.window_event_data(),
                    geometry_generation=generation,
                ),
            ),
            scope,
            generation,
        )
    )
    frame_thread.start()
    allow_translation.set()
    action_thread.join(timeout=5)
    frame_thread.join(timeout=5)

    assert action_result["binding"][2] == 1
    action = journal.get_nowait()
    frame = journal.get_nowait()
    assert (action.source_ordinal, frame.source_ordinal) == (1, 2)
    assert frame.data.geometry_generation == 2


def test_native_receipt_snapshot_does_not_call_window_or_topology_apis(
    scope,
    monkeypatch,
):
    image, _ = scope.capture_frame(publish=False)
    generation = scope.current_generation()
    journal = OrderedEventJournal()
    journal.commit_window_frame(
        Event(
            1.0,
            "screen",
            WindowScopedFrame(
                image=image,
                window_event_data=scope.window_event_data(),
                geometry_generation=generation,
            ),
        ),
        scope,
        generation,
    )

    def forbidden_io(*_args, **_kwargs):
        pytest.fail("native receipt reservation performed live window or topology I/O")

    monkeypatch.setattr(scope, "resolve", forbidden_io)
    monkeypatch.setattr(scope, "_assert_display_topology", forbidden_io)

    receipt = journal.reserve_window_action_receipt(2.0, scope)
    assert receipt.source_ordinal == 2
    receipt.fail(RuntimeError("test cleanup"))


def test_screen_reader_discards_a_frame_with_concurrent_native_input(
    scope,
    monkeypatch,
):
    boundary = NativeInputFrameBoundary()
    clean_results = iter((False, True))
    completed_tokens: list[object] = []

    class CheckedTerminate(threading.Event):
        def wait(self, timeout=None):
            if timeout is not None:
                assert completed_tokens
            return super().wait(timeout)

    terminate = CheckedTerminate()

    class FakeObserver:
        def begin_frame_capture(self) -> object:
            return object()

        def finish_frame_capture(self, _token: object) -> bool:
            return next(clean_results)

        def complete_frame_capture(self, token: object) -> None:
            completed_tokens.append(token)

    boundary.attach(FakeObserver())
    capture_calls = 0
    original_capture = scope.capture_frame

    def counted_capture(*, publish=True):
        nonlocal capture_calls
        capture_calls += 1
        result = original_capture(publish=publish)
        if capture_calls == 2:
            terminate.set()
        return result

    monkeypatch.setattr(scope, "capture_frame", counted_capture)
    journal = OrderedEventJournal()
    read_screen_events(
        journal,
        terminate,
        SimpleNamespace(timestamp=time.time()),
        threading.Event(),
        window_scope=scope,
        input_frame_boundary=boundary,
    )

    assert capture_calls == 2
    assert len(completed_tokens) == 2
    frame = journal.get_nowait()
    assert frame.type == "screen"
    assert frame.source_ordinal == 1
    with pytest.raises(queue.Empty):
        journal.get_nowait()


def test_terminal_frame_seals_native_input_before_commit(scope, monkeypatch):
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
    journal = OrderedEventJournal()
    original_commit = journal.commit_window_frame

    def checked_commit(*args, **kwargs):
        assert sealed
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(journal, "commit_window_frame", checked_commit)
    read_screen_events(
        journal,
        terminate,
        SimpleNamespace(timestamp=time.time()),
        threading.Event(),
        window_scope=scope,
        input_finished=input_finished,
        input_frame_boundary=boundary,
        terminal_frame_finished=terminal_finished,
    )

    assert terminal_finished.is_set()
    assert completed
    assert journal.get_nowait().type == "screen"


def test_terminal_frame_retries_an_input_dirty_capture_before_signaling(
    scope,
    monkeypatch,
):
    monkeypatch.setattr(recorder_module.config, "SCREEN_CAPTURE_FPS", 0)
    terminate = threading.Event()
    terminate.set()
    input_finished = threading.Event()
    input_finished.set()
    terminal_finished = threading.Event()
    boundary = NativeInputFrameBoundary()
    clean_results = iter((False, True))
    seals = 0
    completes = 0

    class FakeObserver:
        def begin_frame_capture(self) -> object:
            return object()

        def finish_frame_capture(self, _token: object) -> bool:
            return next(clean_results)

        def seal_frame_capture(self, _token: object) -> None:
            nonlocal seals
            seals += 1

        def complete_frame_capture(self, _token: object) -> None:
            nonlocal completes
            completes += 1

    boundary.attach(FakeObserver())
    journal = OrderedEventJournal()
    read_screen_events(
        journal,
        terminate,
        SimpleNamespace(timestamp=time.time()),
        threading.Event(),
        window_scope=scope,
        input_finished=input_finished,
        input_frame_boundary=boundary,
        terminal_frame_finished=terminal_finished,
    )

    assert terminal_finished.is_set()
    assert seals == 1
    assert completes == 2
    assert journal.get_nowait().type == "screen"


def test_terminal_frame_deadline_does_not_signal_without_a_clean_cut(
    scope,
    monkeypatch,
):
    monkeypatch.setattr(recorder_module.config, "SCREEN_CAPTURE_FPS", 0)
    monkeypatch.setattr(recorder_module, "TERMINAL_FRAME_SEAL_TIMEOUT_SECONDS", 0.0)
    terminate = threading.Event()
    terminate.set()
    terminal_finished = threading.Event()
    boundary = NativeInputFrameBoundary()
    seals = 0
    completes = 0

    class DirtyObserver:
        def begin_frame_capture(self) -> object:
            return object()

        def finish_frame_capture(self, _token: object) -> bool:
            return False

        def seal_frame_capture(self, _token: object) -> None:
            nonlocal seals
            seals += 1

        def complete_frame_capture(self, _token: object) -> None:
            nonlocal completes
            completes += 1

    boundary.attach(DirtyObserver())
    journal = OrderedEventJournal()

    with pytest.raises(WindowCaptureError, match="terminal-frame deadline"):
        read_screen_events(
            journal,
            terminate,
            SimpleNamespace(timestamp=time.time()),
            threading.Event(),
            window_scope=scope,
            input_frame_boundary=boundary,
            terminal_frame_finished=terminal_finished,
        )

    assert not terminal_finished.is_set()
    assert seals == 0
    assert completes == 1
    with pytest.raises(queue.Empty):
        journal.get_nowait()


def test_processor_binds_first_later_frame_as_the_exact_action_after(scope):
    journal = queue.Queue()
    frames = []
    for timestamp, ordinal in ((1.0, 1), (3.0, 3)):
        image, _ = scope.capture_frame(publish=False)
        frames.append(
            Event(
                timestamp,
                "screen",
                WindowScopedFrame(
                    image=image,
                    window_event_data=scope.window_event_data(),
                    geometry_generation=scope.current_generation(),
                ),
                ordinal,
            )
        )
    journal.put(frames[0])
    journal.put(
        Event(
            2.0,
            "action",
            {
                "name": "click",
                "mouse_x": 1.0,
                "mouse_y": 2.0,
                "mouse_button_name": "left",
                "mouse_pressed": True,
                "window_geometry_generation": 1,
            },
            2,
        )
    )
    journal.put(frames[1])
    queues = [queue.Queue() for _ in range(6)]
    counters = [multiprocessing.Value("i", 0) for _ in range(5)]
    terminate = threading.Event()
    terminate.set()

    recorder_module.process_events(
        journal,
        queues[0],
        queues[1],
        queues[2],
        queues[3],
        queues[4],
        queues[5],
        SimpleNamespace(timestamp=0.0),
        terminate,
        threading.Event(),
        *counters,
    )

    action = queues[1].get_nowait()
    assert action.data["screenshot_source_ordinal"] == 1
    assert action.data["after_screenshot_source_ordinal"] == 3
    assert action.data["after_window_event_source_ordinal"] == 3
    assert action.data["after_window_geometry_generation"] == 1


def test_processor_retains_initial_desktop_frame_without_an_action():
    from openadapt_capture.config import RecordingConfig, config_override

    image = Image.new("RGB", (4, 3), "blue")
    journal = queue.Queue()
    journal.put(Event(1.0, "screen", image, 1))
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
            queues[0],
            queues[1],
            queues[2],
            queues[3],
            queues[4],
            queues[5],
            SimpleNamespace(timestamp=0.0),
            threading.Event(),
            threading.Event(),
            *counters,
            producers_finished,
        )

    retained = queues[0].get_nowait()
    video = queues[4].get_nowait()
    assert retained.source_ordinal == 1
    assert retained.data is None
    assert video.source_ordinal == 1
    assert video.data is image
    assert counters[0].value == 1
    assert counters[4].value == 1


def test_processor_retains_the_contiguous_desktop_action_journal():
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
            queues[0],
            queues[1],
            queues[2],
            queues[3],
            queues[4],
            queues[5],
            SimpleNamespace(timestamp=0.0),
            threading.Event(),
            threading.Event(),
            *counters,
            producers_finished,
        )

    retained_ordinals = [queues[0].get_nowait().source_ordinal for _ in range(3)]
    video_ordinals = [queues[4].get_nowait().source_ordinal for _ in range(3)]
    action = queues[1].get_nowait()
    assert retained_ordinals == [1, 2, 4]
    assert video_ordinals == retained_ordinals
    assert action.source_ordinal == 3
    assert action.data["screenshot_source_ordinal"] == 2
    assert action.data["after_screenshot_source_ordinal"] == 4
    assert counters[0].value == 3
    assert counters[1].value == 1
    assert counters[4].value == 3


def test_processor_retains_desktop_window_events_without_an_action():
    from openadapt_capture.config import RecordingConfig, config_override

    image = Image.new("RGB", (4, 3), "blue")
    journal = queue.Queue()
    journal.put(Event(1.0, "screen", image, 1))
    journal.put(Event(2.0, "window", {"title": "Fixture"}, 2))
    journal.put(Event(3.0, "screen", image, 3))
    queues = [queue.Queue() for _ in range(6)]
    counters = [multiprocessing.Value("i", 0) for _ in range(5)]
    producers_finished = threading.Event()
    producers_finished.set()

    with config_override(
        RecordingConfig(
            capture_video=False,
            capture_images=True,
            capture_window_data=True,
        )
    ):
        recorder_module.process_events(
            journal,
            queues[0],
            queues[1],
            queues[2],
            queues[3],
            queues[4],
            queues[5],
            SimpleNamespace(timestamp=0.0),
            threading.Event(),
            threading.Event(),
            *counters,
            producers_finished,
        )

    retained_ordinals = [queues[0].get_nowait().source_ordinal for _ in range(2)]
    window = queues[2].get_nowait()
    assert retained_ordinals == [1, 3]
    assert window.source_ordinal == 2
    assert counters[0].value == 2
    assert counters[2].value == 1


def test_processor_can_abort_without_claiming_that_producers_finished():
    queues = [queue.Queue() for _ in range(7)]
    counters = [multiprocessing.Value("i", 0) for _ in range(5)]
    producers_finished = threading.Event()
    processing_aborted = threading.Event()
    processing_aborted.set()

    recorder_module.process_events(
        queues[0],
        queues[1],
        queues[2],
        queues[3],
        queues[4],
        queues[5],
        queues[6],
        SimpleNamespace(timestamp=0.0),
        threading.Event(),
        threading.Event(),
        *counters,
        producers_finished,
        processing_aborted,
    )

    assert not producers_finished.is_set()
    assert all(counter.value == 0 for counter in counters)


def test_processor_fails_loud_when_an_action_precedes_configured_window_evidence():
    from openadapt_capture.config import RecordingConfig, config_override

    journal = queue.Queue()
    journal.put(Event(1.0, "screen", Image.new("RGB", (4, 3), "blue"), 1))
    journal.put(Event(2.0, "action", {"name": "mouse.down"}, 2))
    queues = [queue.Queue() for _ in range(6)]
    counters = [multiprocessing.Value("i", 0) for _ in range(5)]
    producers_finished = threading.Event()
    producers_finished.set()

    with config_override(
        RecordingConfig(
            capture_video=False,
            capture_images=True,
            capture_window_data=True,
        )
    ), pytest.raises(WindowCaptureError, match="configured window evidence"):
        recorder_module.process_events(
            journal,
            queues[0],
            queues[1],
            queues[2],
            queues[3],
            queues[4],
            queues[5],
            SimpleNamespace(timestamp=0.0),
            threading.Event(),
            threading.Event(),
            *counters,
            producers_finished,
        )


def test_processor_refuses_a_native_action_without_a_terminal_after_frame(scope):
    image, _ = scope.capture_frame(publish=False)
    journal = queue.Queue()
    journal.put(
        Event(
            1.0,
            "screen",
            WindowScopedFrame(
                image=image,
                window_event_data=scope.window_event_data(),
                geometry_generation=scope.current_generation(),
            ),
            1,
        )
    )
    journal.put(
        Event(
            2.0,
            "action",
            {"name": "press", "key_char": "a", "window_geometry_generation": 1},
            2,
        )
    )
    queues = [queue.Queue() for _ in range(6)]
    counters = [multiprocessing.Value("i", 0) for _ in range(5)]
    terminate = threading.Event()
    terminate.set()

    with pytest.raises(WindowCaptureError, match="pending actions received an after frame"):
        recorder_module.process_events(
            journal,
            queues[0],
            queues[1],
            queues[2],
            queues[3],
            queues[4],
            queues[5],
            SimpleNamespace(timestamp=0.0),
            terminate,
            threading.Event(),
            *counters,
        )


def test_input_observation_precedes_an_in_flight_window_frame(fake):
    capture_entered = threading.Event()
    release_capture = threading.Event()
    terminate = threading.Event()
    block_capture = threading.Event()

    def capturer(window):
        if block_capture.is_set():
            capture_entered.set()
            assert release_capture.wait(timeout=5)
            terminate.set()
        return fake.capturer(window)

    scope = WindowCaptureScope(
        WindowTarget(owner="FakeApp"),
        resolver=fake.resolver,
        capturer=capturer,
    )
    scope.bind_display_topology(
        {
            "schema_version": "openadapt.capture.display-topology/v1",
            "topology_sha256": "a" * 64,
        },
        lambda **_kwargs: None,
    )
    journal = OrderedEventJournal()
    image, _ = scope.capture_frame(publish=False)
    generation = scope.current_generation()
    journal.commit_window_frame(
        Event(
            time.time(),
            "screen",
            WindowScopedFrame(
                image=image,
                window_event_data=scope.window_event_data(),
                geometry_generation=generation,
            ),
        ),
        scope,
        generation,
    )

    block_capture.set()
    screen_reader = threading.Thread(
        target=read_screen_events,
        args=(
            journal,
            terminate,
            SimpleNamespace(timestamp=time.time()),
            threading.Event(),
        ),
        kwargs={"window_scope": scope},
    )
    screen_reader.start()
    assert capture_entered.wait(timeout=5)

    action_finished = threading.Event()
    action_binding: dict[str, int] = {}

    def reserve_action():
        action_timestamp = time.time()
        reservation, binding = journal.reserve_window_action(
            action_timestamp,
            scope,
            310.0,
            170.0,
        )
        action_binding["generation"] = binding[2]
        reservation.complete(
            Event(
                action_timestamp,
                "action",
                {"window_geometry_generation": binding[2]},
            )
        )
        action_finished.set()

    action_reader = threading.Thread(target=reserve_action)
    action_reader.start()
    assert action_finished.wait(timeout=5)

    # The in-flight frame can include the action's result. It must remain
    # unpublished until after the action has bound the previous exact frame.
    initial = journal.get_nowait()
    action = journal.get_nowait()
    assert [initial.type, action.type] == ["screen", "action"]
    assert action_binding["generation"] == initial.data.geometry_generation
    assert journal.empty()

    release_capture.set()
    screen_reader.join(timeout=5)
    action_reader.join(timeout=5)

    assert not screen_reader.is_alive()
    assert not action_reader.is_alive()
    assert journal.get_nowait().type == "screen"


def test_native_receipt_reserves_before_async_input_delivery(scope, monkeypatch):
    """A delayed observer callback cannot bind a post-input frame."""

    class ReadyObserver(ThreadedInputObserver):
        def __init__(self, callback):
            super().__init__(
                callback,
                observe_keyboard=False,
                observe_mouse=True,
                capture_mouse_moves=True,
                shutdown_timeout=1.0,
            )
            self.release_loop = threading.Event()

        def _setup(self):
            return

        def _run_loop(self):
            self.release_loop.wait()

        def _teardown(self):
            return

        def _wake(self):
            self.release_loop.set()

    journal = OrderedEventJournal()
    first_image, _ = scope.capture_frame(publish=False)
    first_generation = scope.current_generation()
    journal.commit_window_frame(
        Event(
            1.0,
            "screen",
            WindowScopedFrame(
                image=first_image,
                window_event_data=scope.window_event_data(),
                geometry_generation=first_generation,
            ),
        ),
        scope,
        first_generation,
    )

    observation_entered = threading.Event()
    release_observation = threading.Event()

    class BlockingStructuralObserver:
        def open_current_thread(self):
            return

        def close_current_thread(self):
            return

        def observe(self, _request):
            observation_entered.set()
            assert release_observation.wait(timeout=5)
            return None

    observers = []

    def create_observer(callback, **_kwargs):
        observer = ReadyObserver(callback)
        observers.append(observer)
        return observer

    monkeypatch.setattr(recorder_module, "create_input_observer", create_observer)
    terminate = threading.Event()
    started = threading.Event()
    input_reader = threading.Thread(
        target=read_input_events,
        args=(
            journal,
            terminate,
            SimpleNamespace(timestamp=time.time()),
            started,
        ),
        kwargs={
            "coordinate_scope": scope,
            "structural_observer": BlockingStructuralObserver(),
        },
    )
    input_reader.start()
    assert started.wait(timeout=5)
    observer = observers[0]
    native_event = ObservedMouseButton(
        x=310.0,
        y=170.0,
        button="left",
        pressed=True,
        timestamp=1.5,
    )
    receipt = observer._reserve_receipt(native_event.timestamp)
    observer._emit(
        native_event,
        receipt=receipt,
    )
    assert observation_entered.wait(timeout=5)

    later_image, _ = scope.capture_frame(publish=False)
    later_generation = scope.current_generation()
    journal.commit_window_frame(
        Event(
            2.0,
            "screen",
            WindowScopedFrame(
                image=later_image,
                window_event_data=scope.window_event_data(),
                geometry_generation=later_generation,
            ),
        ),
        scope,
        later_generation,
    )
    release_observation.set()
    deadline = time.monotonic() + 5
    while not bool(getattr(receipt, "finished", False)):
        if time.monotonic() >= deadline:
            pytest.fail("native receipt was not completed")
        time.sleep(0.001)
    terminate.set()
    input_reader.join(timeout=5)
    assert not input_reader.is_alive()

    initial = journal.get_nowait()
    action = journal.get_nowait()
    later = journal.get_nowait()
    assert [(initial.type, initial.source_ordinal), (action.type, action.source_ordinal)] == [
        ("screen", 1),
        ("action", 2),
    ]
    assert (later.type, later.source_ordinal) == ("screen", 3)
    assert action.data["window_geometry_generation"] == first_generation


def test_window_capture_state_rejects_scales_not_derived_from_content(scope):
    scope.capture_frame()
    state = scope.window_event_data()["state"]
    state["scale"] = 999.0
    state["scale_x"] = 999.0
    state["scale_y"] = 999.0
    state["geometry_epoch_sha256"] = window_geometry_epoch_sha256(
        {key: value for key, value in state.items() if key != "geometry_epoch_sha256"}
    )

    with pytest.raises(ValueError, match="axis scales"):
        WindowCaptureStateV2.model_validate(state)


def test_window_capture_state_accepts_proven_offscreen_sck_frame(scope):
    scope.capture_frame()
    state = scope.window_event_data()["state"]
    state.update(
        {
            "on_screen": False,
            "visibility_independent": True,
            "capture_source": "macos-screencapturekit-stream",
            "frame_status": "complete",
            "frame_display_time": 100,
            "pixel_display_time": 100,
            "stream_generation": 1,
            "stream_sequence": 1,
        }
    )
    state["capture_evidence_sha256"] = (
        window_capture_module.window_capture_evidence_sha256(state)
    )

    parsed = WindowCaptureStateV2.model_validate(state)

    assert parsed.on_screen is False
    assert parsed.visibility_independent is True


def test_window_capture_state_rejects_unproven_offscreen_frame(scope):
    scope.capture_frame()
    state = scope.window_event_data()["state"]
    state.update(
        {
            "on_screen": False,
            "visibility_independent": True,
            "capture_source": "macos-quartz-window-image",
        }
    )

    with pytest.raises(ValueError, match="proven exact-window capture source"):
        WindowCaptureStateV2.model_validate(state)


def test_macos_resolver_ignores_a_larger_hidden_matching_window(monkeypatch):
    monkeypatch.setattr(
        window_capture_module,
        "_macos_visibility_independent_capture_available",
        lambda: False,
    )
    hidden = {
        "kCGWindowOwnerName": "FakeApp",
        "kCGWindowName": "Document",
        "kCGWindowLayer": 0,
        "kCGWindowIsOnscreen": False,
        "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 2000, "Height": 1200},
        "kCGWindowOwnerPID": 100,
        "kCGWindowNumber": 1,
    }
    visible = {
        "kCGWindowOwnerName": "FakeApp",
        "kCGWindowName": "Document",
        "kCGWindowLayer": 0,
        "kCGWindowIsOnscreen": True,
        "kCGWindowBounds": {"X": 10, "Y": 10, "Width": 800, "Height": 600},
        "kCGWindowOwnerPID": 100,
        "kCGWindowNumber": 2,
    }
    quartz = SimpleNamespace(
        kCGWindowListOptionAll=1,
        kCGNullWindowID=0,
        CGWindowListCopyWindowInfo=lambda *_args: [hidden, visible],
    )
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    monkeypatch.setattr(window_capture_module, "_process_start_time", lambda _pid: 123.0)

    resolved = window_capture_module._resolve_window_macos(WindowTarget(owner="FakeApp"))

    assert resolved is not None
    assert resolved.window_id == 2
    assert resolved.on_screen is True


def test_macos_resolver_accepts_offspace_window_with_screencapturekit(monkeypatch):
    hidden = {
        "kCGWindowOwnerName": "FakeApp",
        "kCGWindowName": "Document",
        "kCGWindowLayer": 0,
        "kCGWindowIsOnscreen": False,
        "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 1512, "Height": 944},
        "kCGWindowOwnerPID": 100,
        "kCGWindowNumber": 19373,
    }
    quartz = SimpleNamespace(
        kCGWindowListOptionAll=1,
        kCGNullWindowID=0,
        CGWindowListCopyWindowInfo=lambda *_args: [hidden],
    )
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    monkeypatch.setattr(window_capture_module, "_process_start_time", lambda _pid: 123.0)
    monkeypatch.setattr(window_capture_module, "_screen_capture_kit_available", lambda: True)

    resolved = window_capture_module._resolve_window_macos(
        WindowTarget(owner="FakeApp", title="Document")
    )

    assert resolved is not None
    assert resolved.window_id == 19373
    assert resolved.on_screen is False
    assert resolved.visibility_independent is True
    assert resolved.capture_source == "macos-exact-window-provider-chain"


def test_macos_resolver_accepts_offspace_window_with_exact_utility(monkeypatch):
    hidden = {
        "kCGWindowOwnerName": "FakeApp",
        "kCGWindowName": "Document",
        "kCGWindowLayer": 0,
        "kCGWindowIsOnscreen": False,
        "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 1512, "Height": 944},
        "kCGWindowOwnerPID": 100,
        "kCGWindowNumber": 19373,
    }
    quartz = SimpleNamespace(
        kCGWindowListOptionAll=1,
        kCGNullWindowID=0,
        CGWindowListCopyWindowInfo=lambda *_args: [hidden],
    )
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    monkeypatch.setattr(window_capture_module, "_process_start_time", lambda _pid: 123.0)
    monkeypatch.setattr(window_capture_module, "_screen_capture_kit_available", lambda: False)
    monkeypatch.setattr(window_capture_module.os.path, "isfile", lambda _path: True)
    monkeypatch.setattr(window_capture_module.os, "access", lambda *_args: True)

    resolved = window_capture_module._resolve_window_macos(
        WindowTarget(owner="FakeApp", title="Document")
    )

    assert resolved is not None
    assert resolved.on_screen is False
    assert resolved.visibility_independent is True


def test_macos_resolver_refuses_ambiguous_owner_only_match(monkeypatch):
    windows = [
        {
            "kCGWindowOwnerName": "Google Chrome",
            "kCGWindowName": title,
            "kCGWindowLayer": 0,
            "kCGWindowIsOnscreen": True,
            "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 1512, "Height": 944},
            "kCGWindowOwnerPID": 100,
            "kCGWindowNumber": window_id,
        }
        for window_id, title in ((95, "Profile picker"), (19373, "Amex"))
    ]
    quartz = SimpleNamespace(
        kCGWindowListOptionAll=1,
        kCGNullWindowID=0,
        CGWindowListCopyWindowInfo=lambda *_args: windows,
    )
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    monkeypatch.setattr(window_capture_module, "_process_start_time", lambda _pid: 123.0)
    monkeypatch.setattr(window_capture_module, "_screen_capture_kit_available", lambda: True)

    with pytest.raises(WindowCaptureAmbiguousError, match="complete window title"):
        window_capture_module._resolve_window_macos(WindowTarget(owner="Google Chrome"))


def test_macos_capture_uses_exact_utility_after_sck_and_quartz_fail(monkeypatch):
    window = TargetWindow(
        window_id=19373,
        owner="FakeApp",
        title="Document",
        pid=100,
        bounds=(0.0, 0.0, 1512.0, 944.0),
        on_screen=False,
        process_start_time=123.0,
        coordinate_source="quartz-screen-points",
        capture_source="macos-exact-window-provider-chain",
        visibility_independent=True,
    )
    quartz = SimpleNamespace()
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    stream_calls = []

    class DeniedStream:
        def __init__(self, *, frame_rate=None):
            assert frame_rate is None

        def capture(self, _window, **_kwargs):
            stream_calls.append("capture")
            raise WindowCapturePermissionError("denied")

        def close(self, **_kwargs):
            stream_calls.append("close")

    monkeypatch.setattr(window_capture_module, "_screen_capture_kit_available", lambda: True)
    monkeypatch.setattr(
        window_capture_module,
        "_macos_window_minimized",
        lambda _window, **_kwargs: False,
    )
    monkeypatch.setattr(
        window_capture_module,
        "_MacOSScreenCaptureKitStream",
        DeniedStream,
    )
    expected = Image.new("RGB", (3024, 1888), "white")
    expected.info["openadapt_capture_source"] = "macos-screencapture-utility"
    monkeypatch.setattr(
        window_capture_module,
        "_capture_window_macos_utility",
        lambda _window, **_kwargs: expected,
    )

    captured = window_capture_module._capture_window_macos(window)

    assert captured.size == (3024, 1888)
    assert captured.info["openadapt_capture_source"] == "macos-screencapture-utility"
    assert stream_calls == ["capture", "close", "close"]


def test_macos_provider_disables_failed_stream_for_the_session(monkeypatch):
    window = TargetWindow(
        window_id=19373,
        owner="FakeApp",
        title="Document",
        pid=100,
        bounds=(0.0, 0.0, 1512.0, 944.0),
        on_screen=False,
        process_start_time=123.0,
        coordinate_source="quartz-screen-points",
        visibility_independent=True,
    )
    stream_calls = []

    class FailedStream:
        def __init__(self, *, frame_rate=None):
            assert frame_rate is None

        def capture(self, _window, **_kwargs):
            stream_calls.append("capture")
            raise WindowCapturePermissionError("denied")

        def close(self, **_kwargs):
            stream_calls.append("close")

    monkeypatch.setattr(window_capture_module, "_screen_capture_kit_available", lambda: True)
    monkeypatch.setattr(
        window_capture_module,
        "_macos_window_minimized",
        lambda _window, **_kwargs: False,
    )
    monkeypatch.setattr(
        window_capture_module,
        "_MacOSScreenCaptureKitStream",
        FailedStream,
    )
    utility_calls = []

    def utility(_window, **_kwargs):
        utility_calls.append("capture")
        return Image.new("RGB", (3024, 1888), "white")

    monkeypatch.setattr(window_capture_module, "_capture_window_macos_utility", utility)
    provider = window_capture_module._MacOSWindowCaptureProvider()

    provider.capture(window)
    provider.capture(window)

    assert stream_calls == ["capture", "close"]
    assert utility_calls == ["capture", "capture"]


def test_macos_provider_disables_failed_quartz_for_the_session(monkeypatch):
    window = TargetWindow(
        window_id=19373,
        owner="FakeApp",
        title="Document",
        pid=100,
        bounds=(0.0, 0.0, 1512.0, 944.0),
        on_screen=True,
        process_start_time=123.0,
        coordinate_source="quartz-screen-points",
        visibility_independent=True,
    )
    quartz_calls = []
    quartz = SimpleNamespace(
        CGRectNull=None,
        kCGWindowListOptionIncludingWindow=1,
        kCGWindowImageBoundsIgnoreFraming=2,
        CGWindowListCreateImage=lambda *_args: quartz_calls.append("capture"),
    )
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    monkeypatch.setattr(window_capture_module, "_screen_capture_kit_available", lambda: False)
    utility_calls = []

    def utility(_window, **_kwargs):
        utility_calls.append("capture")
        return Image.new("RGB", (3024, 1888), "white")

    monkeypatch.setattr(window_capture_module, "_capture_window_macos_utility", utility)
    provider = window_capture_module._MacOSWindowCaptureProvider()

    provider.capture(window)
    provider.capture(window)

    assert quartz_calls == ["capture"]
    assert utility_calls == ["capture", "capture"]


def test_screencapturekit_enumerates_other_spaces_and_selects_exact_id(monkeypatch):
    calls = []

    class FakeWindow:
        def __init__(self, window_id):
            self._window_id = window_id

        def windowID(self):
            return self._window_id

    expected = FakeWindow(19373)
    content = SimpleNamespace(windows=lambda: [FakeWindow(95), expected])

    class FakeShareableContent:
        @staticmethod
        def getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
            exclude_desktop, on_screen_only, callback
        ):
            calls.append((exclude_desktop, on_screen_only))
            callback(content, None)

    fake_sck = SimpleNamespace(SCShareableContent=FakeShareableContent)
    monkeypatch.setitem(sys.modules, "ScreenCaptureKit", fake_sck)

    resolved = window_capture_module._screen_capture_kit_window(19373)

    assert resolved is expected
    assert calls == [(True, False)]


@pytest.mark.skipif(sys.platform != "darwin", reason="PyObjC protocol test")
def test_screencapturekit_output_class_has_native_protocol_signatures():
    if not window_capture_module._screen_capture_kit_available():
        pytest.skip("ScreenCaptureKit bindings are not installed")

    output_class = window_capture_module._screen_capture_kit_output_class()

    assert output_class is not None


def _fake_screencapturekit_modules(monkeypatch, attachment):
    fake_sck = SimpleNamespace(
        SCStreamOutputTypeScreen=0,
        SCStreamFrameInfoStatus="status",
        SCStreamFrameInfoDisplayTime="display_time",
        SCFrameStatusComplete=0,
        SCFrameStatusIdle=1,
        SCFrameStatusStarted=2,
    )
    fake_core_media = SimpleNamespace(
        CMSampleBufferGetSampleAttachmentsArray=lambda *_args: [attachment],
    )
    monkeypatch.setitem(sys.modules, "ScreenCaptureKit", fake_sck)
    monkeypatch.setitem(sys.modules, "CoreMedia", fake_core_media)


def test_screencapturekit_stream_retains_complete_and_idle_evidence(monkeypatch):
    _fake_screencapturekit_modules(
        monkeypatch,
        {"status": 0, "display_time": 100},
    )
    first = Image.new("RGB", (20, 10), "blue")
    monkeypatch.setattr(
        window_capture_module,
        "_pil_image_from_sample_buffer",
        lambda _sample: first,
    )
    stream = window_capture_module._MacOSScreenCaptureKitStream(frame_rate=2.0)
    stream._generation = 1

    stream._receive_sample(1, object(), 0)

    assert stream._last_complete_image is first
    assert stream._last_complete_display_time == 100
    assert stream._last_status == 0

    sys.modules["CoreMedia"].CMSampleBufferGetSampleAttachmentsArray = (
        lambda *_args: [{"status": 1, "display_time": 120}]
    )
    stream._receive_sample(1, object(), 0)
    assert stream._last_complete_image is first
    assert stream._last_complete_display_time == 100
    assert stream._last_display_time == 120
    assert stream._last_status == 1


def test_screencapturekit_stream_rejects_missing_frame_metadata(monkeypatch):
    _fake_screencapturekit_modules(monkeypatch, {})
    stream = window_capture_module._MacOSScreenCaptureKitStream()
    stream._generation = 1

    stream._receive_sample(1, object(), 0)

    assert isinstance(stream._error, WindowCaptureError)
    assert "explicit status" in str(stream._error)


def test_screencapturekit_stream_ignores_late_generation(monkeypatch):
    _fake_screencapturekit_modules(
        monkeypatch,
        {"status": 0, "display_time": 100},
    )
    monkeypatch.setattr(
        window_capture_module,
        "_pil_image_from_sample_buffer",
        lambda _sample: Image.new("RGB", (20, 10), "blue"),
    )
    stream = window_capture_module._MacOSScreenCaptureKitStream()
    stream._generation = 2

    stream._receive_sample(1, object(), 0)

    assert stream._sequence == 0
    assert stream._last_complete_image is None


def test_screencapturekit_close_wakes_waiting_capture(monkeypatch):
    _fake_screencapturekit_modules(monkeypatch, {})

    class FakeNativeStream:
        @staticmethod
        def stopCaptureWithCompletionHandler_(callback):
            callback(None)

    window = TargetWindow(
        window_id=19373,
        owner="FakeApp",
        title="Document",
        pid=100,
        bounds=(0.0, 0.0, 20.0, 10.0),
        process_start_time=123.0,
    )
    stream = window_capture_module._MacOSScreenCaptureKitStream()
    stream._stream = FakeNativeStream()
    stream._window_id = window.window_id
    stream._bounds_size = (window.bounds[2], window.bounds[3])
    stream._generation = 1
    errors = []

    def wait_for_frame():
        try:
            stream.capture(window, deadline=time.monotonic() + 5)
        except WindowCaptureError as exc:
            errors.append(exc)

    capture_thread = threading.Thread(target=wait_for_frame)
    capture_thread.start()
    time.sleep(0.02)
    stream.close(timeout_seconds=0.5)
    capture_thread.join(timeout=1)

    assert not capture_thread.is_alive()
    assert len(errors) == 1
    assert "closed" in str(errors[0])


def test_screencapturekit_close_budget_includes_capture_lock_wait():
    stream = window_capture_module._MacOSScreenCaptureKitStream()
    lock_held = threading.Event()
    release_lock = threading.Event()

    def hold_capture_lock():
        with stream._capture_lock:
            lock_held.set()
            assert release_lock.wait(timeout=1)

    holder = threading.Thread(target=hold_capture_lock)
    holder.start()
    assert lock_held.wait(timeout=1)
    started = time.monotonic()
    stream.close(timeout_seconds=0.02)
    elapsed = time.monotonic() - started
    release_lock.set()
    holder.join(timeout=1)

    assert elapsed < 0.2
    assert stream._closed is True
    assert not holder.is_alive()


def test_screencapturekit_capture_owner_stops_stream_after_close_timeout(monkeypatch):
    _fake_screencapturekit_modules(monkeypatch, {})
    stop_called = threading.Event()
    allow_stop = threading.Event()
    stop_calls = 0

    class FakeNativeStream:
        @staticmethod
        def stopCaptureWithCompletionHandler_(callback):
            nonlocal stop_calls
            stop_calls += 1
            stop_called.set()
            assert allow_stop.wait(timeout=1)
            callback(None)

    window = TargetWindow(
        window_id=19373,
        owner="FakeApp",
        title="Document",
        pid=100,
        bounds=(0.0, 0.0, 20.0, 10.0),
        process_start_time=123.0,
    )
    stream = window_capture_module._MacOSScreenCaptureKitStream()
    stream._stream = FakeNativeStream()
    stream._window_id = window.window_id
    stream._bounds_size = (window.bounds[2], window.bounds[3])
    stream._generation = 1
    errors = []

    def wait_for_frame():
        try:
            stream.capture(window, deadline=time.monotonic() + 5)
        except WindowCaptureError as exc:
            errors.append(exc)

    capture_thread = threading.Thread(target=wait_for_frame)
    capture_thread.start()
    time.sleep(0.02)
    started = time.monotonic()
    stream.close(timeout_seconds=0.02)
    elapsed = time.monotonic() - started
    assert stop_called.wait(timeout=1)
    allow_stop.set()
    capture_thread.join(timeout=1)

    assert elapsed < 0.2
    assert not capture_thread.is_alive()
    assert len(errors) == 1
    assert stream._stream is None
    assert stop_calls == 1


def test_macos_provider_preserves_all_provider_permission_denial(monkeypatch):
    window = TargetWindow(
        window_id=19373,
        owner="FakeApp",
        title="Document",
        pid=100,
        bounds=(0.0, 0.0, 1512.0, 944.0),
        on_screen=False,
        process_start_time=123.0,
        coordinate_source="quartz-screen-points",
        visibility_independent=True,
    )

    class DeniedStream:
        def __init__(self, **_kwargs):
            pass

        def capture(self, _window, **_kwargs):
            raise WindowCapturePermissionError("denied")

        def close(self, **_kwargs):
            pass

    monkeypatch.setattr(window_capture_module, "_screen_capture_kit_available", lambda: True)
    monkeypatch.setattr(
        window_capture_module,
        "_macos_window_minimized",
        lambda _window, **_kwargs: False,
    )
    monkeypatch.setattr(
        window_capture_module,
        "_MacOSScreenCaptureKitStream",
        DeniedStream,
    )
    monkeypatch.setattr(
        window_capture_module,
        "_capture_window_macos_utility",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            WindowCapturePermissionError("denied")
        ),
    )

    with pytest.raises(WindowCapturePermissionError, match="all exact-window"):
        window_capture_module._MacOSWindowCaptureProvider().capture(window)


def test_macos_provider_refuses_minimized_window_before_capture(monkeypatch):
    window = TargetWindow(
        window_id=19373,
        owner="FakeApp",
        title="Document",
        pid=100,
        bounds=(0.0, 0.0, 1512.0, 944.0),
        on_screen=False,
        process_start_time=123.0,
        coordinate_source="quartz-screen-points",
        visibility_independent=True,
    )
    monkeypatch.setattr(window_capture_module, "_screen_capture_kit_available", lambda: False)
    monkeypatch.setattr(
        window_capture_module,
        "_macos_window_minimized",
        lambda _window, **_kwargs: True,
    )
    utility_calls = []
    monkeypatch.setattr(
        window_capture_module,
        "_capture_window_macos_utility",
        lambda *_args, **_kwargs: utility_calls.append("capture"),
    )

    with pytest.raises(WindowCaptureError, match="minimized"):
        window_capture_module._MacOSWindowCaptureProvider().capture(window)

    assert utility_calls == []


def test_macos_provider_refuses_unproven_offscreen_capture(monkeypatch):
    window = TargetWindow(
        window_id=19373,
        owner="FakeApp",
        title="Document",
        pid=100,
        bounds=(0.0, 0.0, 1512.0, 944.0),
        on_screen=False,
        process_start_time=123.0,
        coordinate_source="quartz-screen-points",
        visibility_independent=True,
    )
    monkeypatch.setattr(window_capture_module, "_screen_capture_kit_available", lambda: False)
    monkeypatch.setattr(
        window_capture_module,
        "_macos_window_minimized",
        lambda _window, **_kwargs: None,
    )
    utility_calls = []
    monkeypatch.setattr(
        window_capture_module,
        "_capture_window_macos_utility",
        lambda *_args, **_kwargs: utility_calls.append("capture"),
    )

    with pytest.raises(WindowCaptureError, match="proven non-minimized"):
        window_capture_module._MacOSWindowCaptureProvider().capture(window)

    assert utility_calls == []


def test_macos_provider_refuses_capture_after_close(monkeypatch):
    monkeypatch.setattr(window_capture_module, "_screen_capture_kit_available", lambda: False)
    provider = window_capture_module._MacOSWindowCaptureProvider()
    provider.close()
    utility_calls = []
    monkeypatch.setattr(
        window_capture_module,
        "_capture_window_macos_utility",
        lambda *_args, **_kwargs: utility_calls.append("capture"),
    )
    window = TargetWindow(
        window_id=19373,
        owner="FakeApp",
        title="Document",
        pid=100,
        bounds=(0.0, 0.0, 1512.0, 944.0),
        on_screen=True,
        process_start_time=123.0,
    )

    with pytest.raises(WindowCaptureError, match="provider is closed"):
        provider.capture(window)

    assert utility_calls == []


def test_macos_provider_close_during_stream_capture_cannot_fallback(monkeypatch):
    capture_started = threading.Event()
    release_capture = threading.Event()
    utility_calls = []

    class BlockingStream:
        def __init__(self, **_kwargs):
            pass

        def capture(self, _window, **_kwargs):
            capture_started.set()
            assert release_capture.wait(timeout=1)
            return Image.new("RGB", (20, 10), "white")

        def close(self, **_kwargs):
            release_capture.set()

    monkeypatch.setattr(window_capture_module, "_screen_capture_kit_available", lambda: True)
    monkeypatch.setattr(
        window_capture_module,
        "_MacOSScreenCaptureKitStream",
        BlockingStream,
    )
    monkeypatch.setattr(
        window_capture_module,
        "_capture_window_macos_utility",
        lambda *_args, **_kwargs: utility_calls.append("capture"),
    )
    provider = window_capture_module._MacOSWindowCaptureProvider()
    window = TargetWindow(
        window_id=19373,
        owner="FakeApp",
        title="Document",
        pid=100,
        bounds=(0.0, 0.0, 20.0, 10.0),
        on_screen=True,
        process_start_time=123.0,
    )
    errors = []

    def capture():
        try:
            provider.capture(window)
        except WindowCaptureError as exc:
            errors.append(exc)

    capture_thread = threading.Thread(target=capture)
    capture_thread.start()
    assert capture_started.wait(timeout=1)
    provider.close()
    capture_thread.join(timeout=1)

    assert not capture_thread.is_alive()
    assert len(errors) == 1
    assert "provider is closed" in str(errors[0])
    assert utility_calls == []


def test_macos_provider_rechecks_minimized_state_before_utility(monkeypatch):
    window = TargetWindow(
        window_id=19373,
        owner="FakeApp",
        title="Document",
        pid=100,
        bounds=(0.0, 0.0, 1512.0, 944.0),
        on_screen=False,
        process_start_time=123.0,
        coordinate_source="quartz-screen-points",
        visibility_independent=True,
    )
    states = iter((False, True))
    monkeypatch.setattr(window_capture_module, "_screen_capture_kit_available", lambda: False)
    monkeypatch.setattr(
        window_capture_module,
        "_macos_window_minimized",
        lambda _window, **_kwargs: next(states),
    )
    utility_calls = []
    monkeypatch.setattr(
        window_capture_module,
        "_capture_window_macos_utility",
        lambda *_args, **_kwargs: utility_calls.append("capture"),
    )

    with pytest.raises(WindowCaptureError, match="all exact-window") as exc_info:
        window_capture_module._MacOSWindowCaptureProvider().capture(window)

    assert utility_calls == []
    assert "minimized" in str(exc_info.value)


def test_macos_provider_refuses_utility_frame_if_window_becomes_minimized(
    monkeypatch,
):
    window = TargetWindow(
        window_id=19373,
        owner="FakeApp",
        title="Document",
        pid=100,
        bounds=(0.0, 0.0, 1512.0, 944.0),
        on_screen=False,
        process_start_time=123.0,
        coordinate_source="quartz-screen-points",
        visibility_independent=True,
    )
    states = iter((False, False, True))
    monkeypatch.setattr(window_capture_module, "_screen_capture_kit_available", lambda: False)
    monkeypatch.setattr(
        window_capture_module,
        "_macos_window_minimized",
        lambda _window, **_kwargs: next(states),
    )
    utility_calls = []

    def utility(*_args, **_kwargs):
        utility_calls.append("capture")
        return Image.new("RGB", (3024, 1888), "white")

    monkeypatch.setattr(window_capture_module, "_capture_window_macos_utility", utility)

    with pytest.raises(WindowCaptureError, match="all exact-window") as exc_info:
        window_capture_module._MacOSWindowCaptureProvider().capture(window)

    assert utility_calls == ["capture"]
    assert "minimized" in str(exc_info.value)


def test_macos_minimized_state_matches_exact_ax_window_number(monkeypatch):
    exact = object()
    other = object()
    attributes = {
        (exact, "AXWindowNumber"): 19373,
        (exact, "AXTitle"): "Document",
        (exact, "AXMinimized"): False,
        (other, "AXWindowNumber"): 99,
        (other, "AXTitle"): "Document",
    }

    def copy_attribute(element, name, _value):
        if element == "application" and name == "AXWindows":
            return 0, [other, exact]
        value = attributes.get((element, name))
        return (0, value) if value is not None else (1, None)

    application_services = SimpleNamespace(
        kAXErrorSuccess=0,
        AXUIElementCreateApplication=lambda _pid: "application",
        AXUIElementSetMessagingTimeout=lambda _element, _timeout: 0,
        AXUIElementCopyAttributeValue=copy_attribute,
    )
    monkeypatch.setitem(sys.modules, "ApplicationServices", application_services)
    window = TargetWindow(
        window_id=19373,
        owner="FakeApp",
        title="Document",
        pid=100,
        bounds=(0.0, 0.0, 1512.0, 944.0),
        on_screen=False,
        process_start_time=123.0,
    )

    assert window_capture_module._macos_window_minimized(window) is False


def test_macos_minimized_title_fallback_requires_matching_geometry(monkeypatch):
    candidate = object()
    attributes = {
        (candidate, "AXTitle"): "Document",
        (candidate, "AXPosition"): SimpleNamespace(x=50.0, y=50.0),
        (candidate, "AXSize"): SimpleNamespace(width=1512.0, height=944.0),
        (candidate, "AXMinimized"): False,
    }

    def copy_attribute(element, name, _value):
        if element == "application" and name == "AXWindows":
            return 0, [candidate]
        value = attributes.get((element, name))
        return (0, value) if value is not None else (1, None)

    application_services = SimpleNamespace(
        kAXErrorSuccess=0,
        kAXValueCGPointType=1,
        kAXValueCGSizeType=2,
        AXUIElementCreateApplication=lambda _pid: "application",
        AXUIElementSetMessagingTimeout=lambda _element, _timeout: 0,
        AXUIElementCopyAttributeValue=copy_attribute,
        AXValueGetValue=lambda value, _type, _output: (True, value),
    )
    monkeypatch.setitem(sys.modules, "ApplicationServices", application_services)
    window = TargetWindow(
        window_id=19373,
        owner="FakeApp",
        title="Document",
        pid=100,
        bounds=(0.0, 0.0, 1512.0, 944.0),
        on_screen=False,
        process_start_time=123.0,
    )

    assert window_capture_module._macos_window_minimized(window) is None


def test_macos_minimized_title_fallback_rejects_known_different_window_id(
    monkeypatch,
):
    candidate = object()
    attributes = {
        (candidate, "AXWindowNumber"): 99,
        (candidate, "AXTitle"): "Document",
        (candidate, "AXPosition"): SimpleNamespace(x=0.0, y=0.0),
        (candidate, "AXSize"): SimpleNamespace(width=1512.0, height=944.0),
        (candidate, "AXMinimized"): False,
    }

    def copy_attribute(element, name, _value):
        if element == "application" and name == "AXWindows":
            return 0, [candidate]
        value = attributes.get((element, name))
        return (0, value) if value is not None else (1, None)

    application_services = SimpleNamespace(
        kAXErrorSuccess=0,
        kAXValueCGPointType=1,
        kAXValueCGSizeType=2,
        AXUIElementCreateApplication=lambda _pid: "application",
        AXUIElementSetMessagingTimeout=lambda _element, _timeout: 0,
        AXUIElementCopyAttributeValue=copy_attribute,
        AXValueGetValue=lambda value, _type, _output: (True, value),
    )
    monkeypatch.setitem(sys.modules, "ApplicationServices", application_services)
    window = TargetWindow(
        window_id=19373,
        owner="FakeApp",
        title="Document",
        pid=100,
        bounds=(0.0, 0.0, 1512.0, 944.0),
        on_screen=False,
        process_start_time=123.0,
    )

    assert window_capture_module._macos_window_minimized(window) is None


def test_macos_minimized_lookup_sets_bounded_ax_timeout(monkeypatch):
    timeouts = []

    def set_timeout(_element, timeout):
        timeouts.append(timeout)
        return 0

    application_services = SimpleNamespace(
        kAXErrorSuccess=0,
        AXUIElementCreateApplication=lambda _pid: "application",
        AXUIElementSetMessagingTimeout=set_timeout,
        AXUIElementCopyAttributeValue=lambda *_args: (1, None),
    )
    monkeypatch.setitem(sys.modules, "ApplicationServices", application_services)
    window = TargetWindow(
        window_id=19373,
        owner="FakeApp",
        title="Document",
        pid=100,
        bounds=(0.0, 0.0, 1512.0, 944.0),
        on_screen=False,
        process_start_time=123.0,
    )
    monkeypatch.setattr(window_capture_module.time, "monotonic", lambda: 100.0)
    deadline = 100.05

    assert window_capture_module._macos_window_minimized(
        window,
        deadline=deadline,
    ) is None
    assert len(timeouts) == 1
    assert 0 < timeouts[0] <= 0.05


def test_macos_minimized_lookup_refuses_rejected_ax_timeout(monkeypatch):
    reads = []
    application_services = SimpleNamespace(
        kAXErrorSuccess=0,
        AXUIElementCreateApplication=lambda _pid: "application",
        AXUIElementSetMessagingTimeout=lambda _element, _timeout: 1,
        AXUIElementCopyAttributeValue=lambda *_args: reads.append("read"),
    )
    monkeypatch.setitem(sys.modules, "ApplicationServices", application_services)
    window = TargetWindow(
        window_id=19373,
        owner="FakeApp",
        title="Document",
        pid=100,
        bounds=(0.0, 0.0, 1512.0, 944.0),
        on_screen=False,
        process_start_time=123.0,
    )

    assert window_capture_module._macos_window_minimized(window) is None
    assert reads == []


class TestTranslatePoint:
    """Coordinate translation: global screen points -> window pixels."""

    def test_identity_at_origin_scale_1(self):
        assert translate_point(10.0, 20.0, (0.0, 0.0, 100.0, 100.0), 1.0) == (
            10.0,
            20.0,
        )

    def test_offset_window(self):
        # Window at (300, 150): a global point inside it becomes relative.
        assert translate_point(310.0, 170.0, (300.0, 150.0, 800.0, 600.0), 1.0) == (
            10.0,
            20.0,
        )

    def test_retina_scale(self):
        # 2x backing scale: window points map to twice the pixels.
        assert translate_point(310.0, 170.0, (300.0, 150.0, 800.0, 600.0), 2.0) == (
            20.0,
            40.0,
        )

    def test_inverse_of_flow_replay_mapping(self):
        """Round-trip through flow's ``_to_screen``: screen = origin + px/scale."""
        bounds = (37.0, 59.0, 1024.0, 768.0)
        scale = 2.0
        for sx, sy in [(37.0, 59.0), (549.0, 443.0), (1061.0, 827.0)]:
            px, py = translate_point(sx, sy, bounds, scale)
            # flow replay maps the pixel back to a screen point:
            rx, ry = bounds[0] + px / scale, bounds[1] + py / scale
            assert (rx, ry) == (sx, sy)

    def test_out_of_window_points_not_clamped(self):
        """Input outside the window records out-of-range (not clamped) pixels."""
        px, py = translate_point(100.0, 100.0, (300.0, 150.0, 800.0, 600.0), 2.0)
        assert px == -400.0 and py == -100.0


# ---------------------------------------------------------------------------
# WindowTarget spec parsing
# ---------------------------------------------------------------------------


class TestWindowTarget:
    """WindowTarget.from_spec validation (the Recorder(window=...) shape)."""

    def test_none_spec_is_none(self):
        assert WindowTarget.from_spec(None) is None

    def test_dict_spec(self):
        target = WindowTarget.from_spec({"owner": "Parallels", "title": None})
        assert target == WindowTarget(owner="Parallels", title=None)

    def test_title_only(self):
        target = WindowTarget.from_spec({"title": "Accuro"})
        assert target.owner is None and target.title == "Accuro"

    def test_passthrough(self):
        target = WindowTarget(owner="Citrix")
        assert WindowTarget.from_spec(target) is target

    def test_unknown_keys_rejected(self):
        with pytest.raises(ValueError, match="unknown window spec keys"):
            WindowTarget.from_spec({"owner": "Parallels", "app": "x"})

    def test_empty_spec_rejected(self):
        with pytest.raises(ValueError, match="owner.*title"):
            WindowTarget.from_spec({"owner": None, "title": None})

    def test_wrong_type_rejected(self):
        with pytest.raises(TypeError):
            WindowTarget.from_spec("Parallels")

    def test_build_window_scope_none_when_unconfigured(self):
        assert build_window_scope(None, None) is None

    def test_build_window_scope_with_owner(self):
        scope = build_window_scope("Parallels", None)
        assert isinstance(scope, WindowCaptureScope)
        assert scope.target.owner == "Parallels"


# ---------------------------------------------------------------------------
# WindowCaptureScope with injected fakes (no display)
# ---------------------------------------------------------------------------


class FakePlatform:
    """Injectable resolver/capturer simulating a movable Retina window."""

    def __init__(self, bounds=(300.0, 150.0, 800.0, 600.0), scale=2.0):
        self.bounds = bounds
        self.scale = scale
        self.window_id = 42
        self.title = "Fake Window"
        self.missing = False
        self.on_screen = True
        self.visibility_independent = False
        self.process_start_time = 123.5

    def resolver(self, target: WindowTarget):
        if self.missing:
            return None
        return TargetWindow(
            window_id=self.window_id,
            owner="FakeApp",
            title=self.title,
            pid=1234,
            bounds=self.bounds,
            on_screen=self.on_screen,
            visibility_independent=self.visibility_independent,
            process_start_time=self.process_start_time,
            coordinate_source="test-screen-points",
        )

    def capturer(self, window: TargetWindow) -> Image.Image:
        w = int(window.bounds[2] * self.scale)
        h = int(window.bounds[3] * self.scale)
        return Image.new("RGB", (w, h), color=(1, 2, 3))


@pytest.fixture
def fake():
    return FakePlatform()


@pytest.fixture
def scope(fake):
    result = WindowCaptureScope(
        WindowTarget(owner="FakeApp"),
        resolver=fake.resolver,
        capturer=fake.capturer,
    )
    result.bind_display_topology(
        {
            "schema_version": "openadapt.capture.display-topology/v1",
            "topology_sha256": "a" * 64,
        },
        lambda **_kwargs: None,
    )
    return result


class TestWindowCaptureScope:
    """Bounds tracking, scale computation, and translation via fakes."""

    def test_capture_frame_returns_window_pixels(self, scope):
        image, changed = scope.capture_frame()
        assert changed is True  # first frame always establishes the timeline
        assert image.size == (1600, 1200)  # 800x600 points at 2x

    def test_visible_window_cannot_become_offscreen_during_capture(self, fake):
        fake.visibility_independent = True

        def capturer(window):
            image = fake.capturer(window)
            fake.on_screen = False
            return image

        capture_scope = WindowCaptureScope(
            WindowTarget(owner="FakeApp"),
            resolver=fake.resolver,
            capturer=capturer,
        )
        capture_scope.bind_display_topology(
            {
                "schema_version": "openadapt.capture.display-topology/v1",
                "topology_sha256": "a" * 64,
            },
            lambda **_kwargs: None,
        )

        with pytest.raises(WindowCaptureError, match="stopped being visible"):
            capture_scope.capture_frame()

    def test_scale_computed_from_frame_and_bounds(self, scope):
        scope.capture_frame()
        assert scope.snapshot()["scale"] == 2.0

    def test_unchanged_window_not_reannounced(self, scope):
        scope.capture_frame()
        _, changed = scope.capture_frame()
        assert changed is False

    def test_moved_window_flagged_changed(self, scope, fake):
        scope.capture_frame()
        fake.bounds = (500.0, 250.0, 800.0, 600.0)  # window moved
        _, changed = scope.capture_frame()
        assert changed is True

    def test_resized_window_flagged_changed(self, scope, fake):
        scope.capture_frame()
        fake.bounds = (300.0, 150.0, 900.0, 700.0)
        image, changed = scope.capture_frame()
        assert changed is True
        assert image.size == (1600, 1200)
        state = scope.window_event_data()["state"]
        assert state["viewport"] == [1600, 1200]
        assert state["source_viewport"] == [1800, 1400]

    def test_resize_letterboxes_and_translates_into_fixed_viewport(self, scope, fake):
        scope.capture_frame()
        fake.bounds = (300.0, 150.0, 400.0, 600.0)

        image, changed = scope.capture_frame()

        assert changed is True
        assert image.size == (1600, 1200)
        state = scope.window_event_data()["state"]
        assert state["source_viewport"] == [800, 1200]
        assert state["content_rect"] == [400, 0, 800, 1200]
        assert state["fit_scale"] == 1.0
        assert scope.translate(300.0, 150.0) == (400.0, 0.0)
        assert scope.translate(500.0, 450.0) == (800.0, 600.0)

    def test_resize_uses_exact_axis_scales_after_integer_rounding(self, fake):
        fake.bounds = (0.0, 0.0, 3.0, 3.0)
        images = iter(
            [
                Image.new("RGB", (5, 5)),
                Image.new("RGB", (4, 3)),
            ]
        )
        scope = WindowCaptureScope(
            WindowTarget(owner="FakeApp"),
            resolver=fake.resolver,
            capturer=lambda _window: next(images),
        )
        scope.bind_display_topology(
            {
                "schema_version": "openadapt.capture.display-topology/v1",
                "topology_sha256": "a" * 64,
            },
            lambda **_kwargs: None,
        )
        scope.capture_frame()
        scope.capture_frame()

        state = scope.window_event_data()["state"]
        assert state["content_rect"] == [0, 0, 5, 4]
        assert state["scale_x"] == pytest.approx(5 / 3)
        assert state["scale_y"] == pytest.approx(4 / 3)
        assert scope.translate(1.5, 1.5) == pytest.approx((2.5, 2.0))

    def test_translate_before_first_frame_raises(self, scope):
        with pytest.raises(WindowCaptureError, match="before the first"):
            scope.translate(400.0, 300.0)

    def test_translate_uses_latest_bounds(self, scope, fake):
        scope.capture_frame()
        assert scope.translate(310.0, 170.0) == (20.0, 40.0)
        # Window moves; after the next frame the SAME global point maps
        # relative to the new origin.
        fake.bounds = (100.0, 50.0, 800.0, 600.0)
        scope.capture_frame()
        assert scope.translate(310.0, 170.0) == (420.0, 240.0)

    def test_resolve_does_not_mix_new_bounds_with_previous_frame(self, scope, fake):
        scope.capture_frame()
        assert scope.translate(310.0, 170.0) == (20.0, 40.0)

        fake.bounds = (100.0, 50.0, 800.0, 600.0)
        scope.resolve()

        # A resolver poll alone cannot commit geometry. Input stops until a
        # frame with the new bounds is captured and published.
        with pytest.raises(WindowCaptureError, match="moved or resized"):
            scope.translate(310.0, 170.0)
        scope.capture_frame()
        assert scope.translate(310.0, 170.0) == (420.0, 240.0)

    def test_window_identity_change_terminates_scope(self, scope, fake):
        scope.capture_frame()
        fake.window_id = 99

        with pytest.raises(WindowCaptureError, match="changed window identity"):
            scope.capture_frame()

    def test_missing_window_raises_loudly(self, scope, fake):
        fake.missing = True
        with pytest.raises(WindowCaptureError, match="no window matching"):
            scope.capture_frame()

    def test_screen_reader_propagates_capture_failure_without_retry(self, scope, fake):
        fake.missing = True

        with pytest.raises(WindowCaptureError, match="no window matching"):
            read_screen_events(
                queue.Queue(),
                threading.Event(),
                SimpleNamespace(timestamp=time.time()),
                threading.Event(),
                window_scope=scope,
            )

    def test_screen_reader_retains_terminal_frame_after_input_finishes(self, scope):
        journal = OrderedEventJournal()
        terminate = threading.Event()
        terminate.set()
        input_finished = threading.Event()
        input_finished.set()

        read_screen_events(
            journal,
            terminate,
            SimpleNamespace(timestamp=time.time()),
            threading.Event(),
            window_scope=scope,
            input_finished=input_finished,
        )

        event = journal.get_nowait()
        assert event.source_ordinal == 1
        assert isinstance(event.data, WindowScopedFrame)
        assert event.data.geometry_generation == 1
        assert journal.empty()

    def test_window_event_data_matches_window_event_columns(self, scope):
        scope.capture_frame()
        data = scope.window_event_data()
        # Keys are exactly the WindowEvent insert payload.
        assert set(data) == {
            "title",
            "left",
            "top",
            "width",
            "height",
            "window_id",
            "state",
        }
        assert data["left"] == 300 and data["top"] == 150
        assert data["width"] == 800 and data["height"] == 600
        assert data["window_id"] == "42"
        state = data["state"]
        assert state["window_capture"] is True
        assert state["scale"] == 2.0
        assert state["bounds"] == [300.0, 150.0, 800.0, 600.0]
        assert state["viewport"] == [1600, 1200]
        assert state["source_viewport"] == [1600, 1200]
        assert state["content_rect"] == [0, 0, 1600, 1200]
        assert state["fit_scale"] == 1.0

    def test_window_event_data_before_frame_raises(self, scope):
        with pytest.raises(WindowCaptureError):
            scope.window_event_data()

    def test_snapshot_shape(self, scope):
        scope.capture_frame()
        snap = scope.snapshot()
        assert snap["coordinate_space"] == "window_pixels"
        assert snap["target"] == {"owner": "FakeApp", "title": None}
        assert snap["window_id"] == 42
        assert snap["initial_bounds"] == [300.0, 150.0, 800.0, 600.0]
        assert snap["viewport"] == [1600, 1200]
        assert snap["source_viewport"] == [1600, 1200]
        assert snap["content_rect"] == [0, 0, 1600, 1200]

    def test_snapshot_before_frame_has_target_only(self, scope):
        snap = scope.snapshot()
        assert snap["coordinate_space"] == "window_pixels"
        assert "window_id" not in snap


# ---------------------------------------------------------------------------
# Config plumbing (Recorder(window=...) -> RecordingConfig -> Settings)
# ---------------------------------------------------------------------------


class TestWindowConfigPlumbing:
    """The window spec flows through config the same way other options do."""

    def test_config_override_window_fields(self):
        from openadapt_capture.config import (
            RecordingConfig,
            config,
            config_override,
        )

        assert config.RECORD_WINDOW_OWNER is None  # full-screen by default
        rc = RecordingConfig(window_owner="Parallels", window_title="Accuro")
        with config_override(rc):
            assert config.RECORD_WINDOW_OWNER == "Parallels"
            assert config.RECORD_WINDOW_TITLE == "Accuro"
        assert config.RECORD_WINDOW_OWNER is None
        assert config.RECORD_WINDOW_TITLE is None

    def test_recorder_accepts_window_param(self):
        rec = Recorder(
            "/tmp/test_never_created",
            task_description="test",
            window={"owner": "Parallels", "title": None},
        )
        assert rec._recording_config.window_owner == "Parallels"
        assert rec._recording_config.window_title is None

    def test_recorder_without_window_param_records_fullscreen(self):
        rec = Recorder("/tmp/test_never_created")
        assert rec._recording_config.window_owner is None
        assert rec._recording_config.window_title is None

    def test_recorder_rejects_bad_window_spec(self):
        with pytest.raises(ValueError):
            Recorder("/tmp/test_never_created", window={"app": "Parallels"})


# ---------------------------------------------------------------------------
# Coordinate translation through the recorder's action path (no display)
# ---------------------------------------------------------------------------


class TestActionTranslation:
    """trigger_action_event translates coordinates in window mode."""

    def _drain(self, q):
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        return events

    def test_mouse_action_translated(self, scope):
        import queue

        from openadapt_capture import utils
        from openadapt_capture.recorder import trigger_action_event

        utils.set_start_time()
        scope.capture_frame()
        q = queue.Queue()
        trigger_action_event(q, {"name": "click", "mouse_x": 310.0, "mouse_y": 170.0}, scope)
        (event,) = self._drain(q)
        assert event.data["mouse_x"] == 20.0
        assert event.data["mouse_y"] == 40.0

    def test_mouse_action_before_first_frame_fails_session(self, scope):
        import queue

        from openadapt_capture import utils
        from openadapt_capture.recorder import trigger_action_event

        utils.set_start_time()
        q = queue.Queue()
        with pytest.raises(WindowCaptureError, match="before the first"):
            trigger_action_event(
                q,
                {"name": "click", "mouse_x": 310.0, "mouse_y": 170.0},
                scope,
            )
        assert self._drain(q) == []

    def test_key_action_unaffected(self, scope):
        import queue

        from openadapt_capture import utils
        from openadapt_capture.recorder import trigger_action_event

        utils.set_start_time()
        scope.capture_frame()
        q = queue.Queue()
        trigger_action_event(q, {"name": "press", "key_char": "a"}, scope)
        (event,) = self._drain(q)
        assert event.data["key_char"] == "a"

    def test_no_scope_leaves_globals(self):
        import queue

        from openadapt_capture import utils
        from openadapt_capture.recorder import trigger_action_event

        utils.set_start_time()
        q = queue.Queue()
        trigger_action_event(q, {"name": "click", "mouse_x": 310.0, "mouse_y": 170.0})
        (event,) = self._drain(q)
        assert event.data["mouse_x"] == 310.0


# ---------------------------------------------------------------------------
# Persistence: capture_window config JSON round-trips through CaptureSession
# ---------------------------------------------------------------------------


class TestWindowCapturePersistence:
    """Recording.config['capture_window'] is exposed to converters."""

    def _insert_recording(self, capture_path, config_json=None):
        os.makedirs(capture_path, exist_ok=True)
        db_path = os.path.join(capture_path, "recording.db")
        engine, Session = create_db(db_path)
        session = Session()
        recording_data = {
            "timestamp": time.time(),
            "monitor_width": 1920,
            "monitor_height": 1080,
            "double_click_interval_seconds": 0.5,
            "double_click_distance_pixels": 5,
            "platform": sys.platform,
            "task_description": "window test",
        }
        if config_json is not None:
            recording_data["config"] = config_json
        crud.insert_recording(session, recording_data)
        session.close()
        engine.dispose()
        return capture_path

    def test_window_capture_round_trips(self, tmp_path):
        scope_info = {
            "target": {"owner": "Parallels", "title": None},
            "coordinate_space": "window_pixels",
            "window_id": 42,
            "initial_bounds": [300.0, 150.0, 800.0, 600.0],
            "scale": 2.0,
            "viewport": [1600, 1200],
        }
        capture_path = self._insert_recording(str(tmp_path / "cap"), {"capture_window": scope_info})
        with CaptureSession.load(capture_path) as capture:
            assert capture.window_capture == scope_info
            assert capture.window_capture["coordinate_space"] == "window_pixels"

    def test_fullscreen_recording_has_no_window_capture(self, tmp_path):
        capture_path = self._insert_recording(str(tmp_path / "cap"))
        with CaptureSession.load(capture_path) as capture:
            assert capture.window_capture is None


# ---------------------------------------------------------------------------
# Live smoke test: capture a REAL window (interactive desktop only)
# ---------------------------------------------------------------------------

_ON_SUPPORTED_PLATFORM = sys.platform in ("darwin", "win32")
_PLATFORM_SKIP_REASON = (
    "window-scoped capture supports macOS (CGWindowListCreateImage) and "
    "Windows (Win32 + mss region grab) only; no Linux implementation"
)
# Same gate as the input-injection tests in tests/test_performance.py: on
# hosted CI runners the job executes in a non-interactive session, so there
# is no guarantee a resolvable/capturable application window exists. Run on
# an interactive desktop (developer machine or the Parallels rig).
_NO_INPUT_INJECTION = os.environ.get("OPENADAPT_CI_NO_INPUT_INJECTION") == "1"
_PRODUCTION_QUALIFICATION = os.environ.get("OPENADAPT_CAPTURE_PRODUCTION_QUALIFICATION") == "1"
_SESSION_SKIP_REASON = (
    "OPENADAPT_CI_NO_INPUT_INJECTION=1: non-interactive hosted-runner session "
    "has no guaranteed capturable application window (hosted CI limitation, "
    "not a window-capture bug); run on an interactive macOS/Windows desktop"
)
# Default smoke target: a window that exists on any logged-in desktop.
# Override on the rig: OPENADAPT_WINDOW_SMOKE_OWNER=Parallels
_SMOKE_OWNER = os.environ.get(
    "OPENADAPT_WINDOW_SMOKE_OWNER",
    "Finder" if sys.platform == "darwin" else "explorer",
)
_SMOKE_TITLE = os.environ.get("OPENADAPT_WINDOW_SMOKE_TITLE") or None


def _geometry_changed(
    current: tuple[float, float, float, float],
    original: tuple[float, float, float, float],
) -> bool:
    """Return true only after both the position and the size change."""
    moved = abs(current[0] - original[0]) >= 1 or abs(current[1] - original[1]) >= 1
    resized = abs(current[2] - original[2]) >= 1 or abs(current[3] - original[3]) >= 1
    return moved and resized


def _capture_until_bounds(
    scope: WindowCaptureScope,
    predicate,
    *,
    timeout: float = 10.0,
):
    """Capture until the live target has bounds accepted by ``predicate``."""
    deadline = time.monotonic() + timeout
    last_bounds = None
    saw_changed = False
    while time.monotonic() < deadline:
        image, changed = scope.capture_frame()
        saw_changed = saw_changed or changed
        data = scope.window_event_data()
        last_bounds = tuple(data["state"]["bounds"])
        if predicate(last_bounds):
            return image, saw_changed, data
        time.sleep(0.1)
    raise AssertionError(
        f"window bounds did not reach the required state within {timeout}s; "
        f"last bounds were {last_bounds!r}"
    )


@contextmanager
def _temporary_windows_geometry(window: TargetWindow):
    """Move and resize a normal Win32 window, then restore its exact rectangle."""
    import ctypes
    import ctypes.wintypes as wintypes

    user32 = ctypes.windll.user32
    hwnd = wintypes.HWND(window.window_id)
    assert user32.IsWindow(hwnd), f"Win32 window {window.window_id} no longer exists"
    assert not user32.IsIconic(hwnd), "qualification target must not be minimized"
    assert not user32.IsZoomed(hwnd), "qualification target must not be maximized"

    original = wintypes.RECT()
    assert user32.GetWindowRect(hwnd, ctypes.byref(original)), (
        f"GetWindowRect failed for Win32 window {window.window_id}"
    )
    original_width = original.right - original.left
    original_height = original.bottom - original.top
    target_width = max(320, original_width - 137)
    target_height = max(240, original_height - 83)
    if target_width == original_width:
        target_width += 137
    if target_height == original_height:
        target_height += 83

    mutated = False
    try:
        mutated = bool(
            user32.MoveWindow(
                hwnd,
                original.left + 37,
                original.top + 29,
                target_width,
                target_height,
                True,
            )
        )
        assert mutated, f"MoveWindow failed for Win32 window {window.window_id}"
        yield
    finally:
        if mutated:
            restored = user32.MoveWindow(
                hwnd,
                original.left,
                original.top,
                original_width,
                original_height,
                True,
            )
            assert restored, f"could not restore Win32 window {window.window_id} geometry"


def _macos_ax_attribute(application_services, element, name: str):
    """Read one AX attribute and return ``None`` when it is unavailable."""
    error, value = application_services.AXUIElementCopyAttributeValue(
        element,
        name,
        None,
    )
    if error != application_services.kAXErrorSuccess:
        return None
    return value


def _macos_ax_geometry(application_services, value, value_type):
    """Read a CGPoint or CGSize from an AXValue."""
    success, geometry = application_services.AXValueGetValue(
        value,
        value_type,
        None,
    )
    assert success, "could not decode macOS accessibility geometry"
    return geometry


@contextmanager
def _temporary_macos_geometry(window: TargetWindow):
    """Move and resize one AX window, then restore its exact AX geometry."""
    import ApplicationServices

    app = ApplicationServices.AXUIElementCreateApplication(window.pid)
    ax_windows = _macos_ax_attribute(ApplicationServices, app, "AXWindows") or []
    id_matches = []
    title_matches = []
    for candidate in ax_windows:
        candidate_number = _macos_ax_attribute(
            ApplicationServices,
            candidate,
            "AXWindowNumber",
        )
        if candidate_number is not None and int(candidate_number) == window.window_id:
            id_matches.append(candidate)
        candidate_title = _macos_ax_attribute(
            ApplicationServices,
            candidate,
            "AXTitle",
        )
        if candidate_title is not None and str(candidate_title) == window.title:
            title_matches.append(candidate)

    if id_matches:
        ax_window = id_matches[0]
    else:
        assert len(title_matches) == 1, (
            "the macOS qualification target must expose a unique Accessibility "
            f"window title; found {len(title_matches)} matches for {window.title!r}"
        )
        ax_window = title_matches[0]

    fullscreen = _macos_ax_attribute(
        ApplicationServices,
        ax_window,
        "AXFullScreen",
    )
    movable = _macos_ax_attribute(ApplicationServices, ax_window, "AXMovable")
    resizable = _macos_ax_attribute(ApplicationServices, ax_window, "AXResizable")
    assert not fullscreen, "qualification target must not be full screen"
    assert movable is not False, "qualification target must be movable"
    assert resizable is not False, "qualification target must be resizable"

    original_position = _macos_ax_attribute(
        ApplicationServices,
        ax_window,
        "AXPosition",
    )
    original_size = _macos_ax_attribute(ApplicationServices, ax_window, "AXSize")
    assert original_position is not None and original_size is not None, (
        "qualification target does not expose mutable Accessibility geometry"
    )
    point = _macos_ax_geometry(
        ApplicationServices,
        original_position,
        ApplicationServices.kAXValueCGPointType,
    )
    size = _macos_ax_geometry(
        ApplicationServices,
        original_size,
        ApplicationServices.kAXValueCGSizeType,
    )
    target_width = max(320.0, float(size.width) - 137.0)
    target_height = max(240.0, float(size.height) - 83.0)
    if target_width == float(size.width):
        target_width += 137.0
    if target_height == float(size.height):
        target_height += 83.0

    target_position_value = ApplicationServices.AXValueCreate(
        ApplicationServices.kAXValueCGPointType,
        (float(point.x) + 37.0, float(point.y) + 29.0),
    )
    target_size_value = ApplicationServices.AXValueCreate(
        ApplicationServices.kAXValueCGSizeType,
        (target_width, target_height),
    )

    mutated = False
    try:
        size_error = ApplicationServices.AXUIElementSetAttributeValue(
            ax_window,
            "AXSize",
            target_size_value,
        )
        mutated = size_error == ApplicationServices.kAXErrorSuccess
        assert mutated, f"could not resize macOS window (AX error {size_error})"
        position_error = ApplicationServices.AXUIElementSetAttributeValue(
            ax_window,
            "AXPosition",
            target_position_value,
        )
        assert position_error == ApplicationServices.kAXErrorSuccess, (
            f"could not move macOS window (AX error {position_error})"
        )
        yield
    finally:
        if mutated:
            size_error = ApplicationServices.AXUIElementSetAttributeValue(
                ax_window,
                "AXSize",
                original_size,
            )
            position_error = ApplicationServices.AXUIElementSetAttributeValue(
                ax_window,
                "AXPosition",
                original_position,
            )
            assert size_error == ApplicationServices.kAXErrorSuccess, (
                f"could not restore macOS window size (AX error {size_error})"
            )
            assert position_error == ApplicationServices.kAXErrorSuccess, (
                f"could not restore macOS window position (AX error {position_error})"
            )


@contextmanager
def _temporary_window_geometry(window: TargetWindow):
    """Dispatch a reversible live geometry change to the current platform."""
    if sys.platform == "win32":
        with _temporary_windows_geometry(window):
            yield
        return
    if sys.platform == "darwin":
        with _temporary_macos_geometry(window):
            yield
        return
    raise AssertionError(f"no live window geometry controller for {sys.platform}")


@pytest.mark.slow
@pytest.mark.skipif(not _ON_SUPPORTED_PLATFORM, reason=_PLATFORM_SKIP_REASON)
@pytest.mark.skipif(_NO_INPUT_INJECTION, reason=_SESSION_SKIP_REASON)
class TestWindowCaptureLive:
    """Capture a real window end to end (resolve -> frame -> translate)."""

    def _scope(self, *, require_on_screen: bool = False) -> WindowCaptureScope:
        scope = WindowCaptureScope(WindowTarget(owner=_SMOKE_OWNER, title=_SMOKE_TITLE))
        try:
            resolved = scope.resolve()
        except WindowCaptureError as exc:
            if _PRODUCTION_QUALIFICATION:
                raise AssertionError(
                    f"production qualification requires an exact window "
                    f"matching owner {_SMOKE_OWNER!r} title {_SMOKE_TITLE!r}"
                ) from exc
            pytest.skip(
                f"no exact window matching owner {_SMOKE_OWNER!r} "
                f"title {_SMOKE_TITLE!r} on this desktop; open one (or set "
                "OPENADAPT_WINDOW_SMOKE_OWNER) to run the live smoke test"
            )
        if require_on_screen and not resolved.on_screen:
            if _PRODUCTION_QUALIFICATION:
                raise AssertionError(
                    "the live geometry qualification window is not on screen"
                )
            pytest.skip("the matching live geometry-test window is not on screen")
        desktop = DesktopCaptureScope.current()
        scope.bind_display_topology(desktop.snapshot(), desktop.assert_current)
        return scope

    def test_live_window_frame_and_translation(self):
        scope = self._scope()
        image, changed = scope.capture_frame()
        assert changed is True
        assert image.width > 0 and image.height > 0

        snap = scope.snapshot()
        assert snap["coordinate_space"] == "window_pixels"
        assert snap["scale"] > 0
        x, y, w, h = snap["initial_bounds"]
        assert w > 0 and h > 0

        # The window's center (global points) must translate to the center
        # of the captured frame (pixels), within a pixel of rounding.
        cx, cy = scope.translate(x + w / 2, y + h / 2)
        assert abs(cx - image.width / 2) <= max(2.0, snap["scale"])
        assert abs(cy - image.height / 2) <= max(2.0, snap["scale"])

        # Bounds-timeline payload is writable as a WindowEvent.
        data = scope.window_event_data()
        assert data["state"]["viewport"] == [image.width, image.height]
        scope.close()

    @pytest.mark.skipif(
        not _PRODUCTION_QUALIFICATION,
        reason=(
            "live window move/resize changes are reserved for explicit "
            "OPENADAPT_CAPTURE_PRODUCTION_QUALIFICATION=1 runs"
        ),
    )
    def test_live_move_resize_preserves_fixed_viewport_and_restores_window(self):
        """Prove live move/resize normalization without changing final app state."""
        discovery_scope = self._scope(require_on_screen=True)
        target = discovery_scope.resolve()
        assert target.title.strip(), (
            "production qualification requires a target with a stable window title"
        )

        # Bind this test to the exact resolved application/title. This prevents
        # an owner-only selector from switching to another large window after
        # the target changes size.
        scope = WindowCaptureScope(WindowTarget(owner=target.owner, title=target.title))
        desktop = DesktopCaptureScope.current()
        scope.bind_display_topology(desktop.snapshot(), desktop.assert_current)
        initial_image, initial_changed = scope.capture_frame()
        assert initial_changed is True
        initial_data = scope.window_event_data()
        initial_state = initial_data["state"]
        initial_bounds = tuple(initial_state["bounds"])
        initial_viewport = initial_state["viewport"]
        initial_source_viewport = initial_state["source_viewport"]

        with _temporary_window_geometry(target):
            moved_image, moved_changed, moved_data = _capture_until_bounds(
                scope,
                lambda bounds: _geometry_changed(bounds, initial_bounds),
            )

            assert moved_changed is True
            assert moved_data["window_id"] == str(target.window_id)
            moved_state = moved_data["state"]
            assert moved_image.size == tuple(initial_viewport)
            assert moved_state["viewport"] == initial_viewport
            assert moved_state["source_viewport"] != initial_source_viewport

            # A changed aspect ratio must be represented by a content rectangle
            # inside the fixed output viewport, not by a dropped or stretched
            # frame.
            content_x, content_y, content_width, content_height = moved_state["content_rect"]
            assert 0 <= content_x < initial_viewport[0]
            assert 0 <= content_y < initial_viewport[1]
            assert 0 < content_width <= initial_viewport[0]
            assert 0 < content_height <= initial_viewport[1]
            assert [content_x, content_y, content_width, content_height] != [
                0,
                0,
                *initial_viewport,
            ]

            # Input at the live window center must map to the center of the
            # non-letterboxed content, even after the move and resize.
            x, y, width, height = moved_state["bounds"]
            px, py = scope.translate(x + width / 2, y + height / 2)
            tolerance = max(3.0, float(moved_state["scale"]))
            assert px == pytest.approx(content_x + content_width / 2, abs=tolerance)
            assert py == pytest.approx(content_y + content_height / 2, abs=tolerance)

        restored_image, restored_changed, restored_data = _capture_until_bounds(
            scope,
            lambda bounds: all(
                abs(current - original) <= 4 for current, original in zip(bounds, initial_bounds)
            ),
        )
        assert restored_changed is True
        assert restored_image.size == tuple(initial_viewport)
        assert restored_data["window_id"] == str(target.window_id)
        scope.close()
        discovery_scope.close()

    def test_live_missing_window_fails_loud(self):
        scope = WindowCaptureScope(WindowTarget(owner="no-such-app-obviously-not-running-xyz"))
        scope.bind_display_topology(
            {
                "schema_version": "openadapt.capture.display-topology/v1",
                "topology_sha256": "a" * 64,
            },
            lambda **_kwargs: None,
        )
        with pytest.raises(WindowCaptureError, match="no window matching"):
            scope.capture_frame()

"""Platform-neutral and Linux contracts for native input observation."""

from __future__ import annotations

import queue
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from openadapt_capture import input as input_module
from openadapt_capture import recorder as recorder_module
from openadapt_capture.capture import _convert_action_event
from openadapt_capture.events import MouseClickEvent, MouseDownEvent, MouseUpEvent
from openadapt_capture.input_observer import (
    InputObserverError,
    InputObserverUnavailableError,
    ObservedKey,
    ObservedMouseButton,
    ObservedMouseScroll,
    ThreadedInputObserver,
    add_exception_note,
    create_input_observer,
)
from openadapt_capture.input_observer.linux import (
    LinuxXInputObserver,
    normalize_xinput_button_event,
    normalize_xinput_key_event,
)
from openadapt_capture.processing import merge_consecutive_mouse_click_events


class _CooperativeNeverReadyObserver(ThreadedInputObserver):
    def __init__(self) -> None:
        super().__init__(
            lambda event: None,
            observe_keyboard=True,
            observe_mouse=False,
            capture_mouse_moves=False,
            startup_timeout=0.02,
            shutdown_timeout=0.2,
        )
        self.resource_open = False
        self.torn_down = threading.Event()

    def _setup(self) -> None:
        self.resource_open = True
        self._stop_requested.wait()

    def _run_loop(self) -> None:
        return

    def _teardown(self) -> None:
        self.resource_open = False
        self.torn_down.set()


class _StubbornNeverReadyObserver(_CooperativeNeverReadyObserver):
    def __init__(self) -> None:
        super().__init__()
        self.shutdown_timeout = 0.01
        self.release_setup = threading.Event()

    def _setup(self) -> None:
        self.resource_open = True
        self.release_setup.wait()


class _FailureAfterReadinessTimeoutObserver(_CooperativeNeverReadyObserver):
    """Report a concrete setup failure only after start begins its abort."""

    def _setup(self) -> None:
        self.resource_open = True
        self._stop_requested.wait()
        raise RuntimeError("setup failed while readiness timed out")


class _ReadyObserver(ThreadedInputObserver):
    def __init__(self, callback=lambda _event: None) -> None:
        super().__init__(
            callback,
            observe_keyboard=True,
            observe_mouse=False,
            capture_mouse_moves=False,
            startup_timeout=0.2,
            shutdown_timeout=0.02,
        )
        self.release_loop = threading.Event()

    def _setup(self) -> None:
        return

    def _run_loop(self) -> None:
        self.release_loop.wait()

    def _teardown(self) -> None:
        return

    def _wake(self) -> None:
        self.release_loop.set()


class _SetupEmittingObserver(_ReadyObserver):
    def __init__(
        self,
        callback,
        *,
        fail_setup: bool = False,
        delivery_queue_size: int = 4096,
    ) -> None:
        self.callback_invoked = threading.Event()

        def observe_delivery(event) -> None:
            self.callback_invoked.set()
            callback(event)

        super().__init__(observe_delivery)
        self.delivery_queue_size = delivery_queue_size
        self.fail_setup = fail_setup
        self.health_checked = False
        self.callback_observed_during_setup = False
        self.events = [
            ObservedKey(pressed=True, key_char="a", timestamp=1.0),
            ObservedKey(pressed=False, key_char="a", timestamp=2.0),
        ]

    def _setup(self) -> None:
        for event in self.events:
            self._emit(event)
        self.callback_observed_during_setup = self.callback_invoked.wait(timeout=0.05)
        if self.fail_setup:
            raise RuntimeError("setup failed after receiving input")

    def check_health(self) -> None:
        self.health_checked = True
        super().check_health()


def test_setup_events_are_delivered_in_order_only_after_start_commits() -> None:
    delivered = []
    observer = _SetupEmittingObserver(delivered.append)

    observer.start()
    deadline = time.monotonic() + 1
    while len(delivered) < len(observer.events):
        if time.monotonic() >= deadline:
            pytest.fail("committed setup events were not delivered")
        time.sleep(0.001)
    observer.stop()

    assert observer.health_checked
    assert not observer.callback_observed_during_setup
    assert delivered == observer.events


def test_delivery_thread_balances_callback_lifecycle_hooks() -> None:
    lifecycle: list[tuple[str, int]] = []
    delivered = threading.Event()

    class Callback:
        def _openadapt_delivery_thread_start(self) -> None:
            lifecycle.append(("start", threading.get_ident()))

        def __call__(self, _event) -> None:
            lifecycle.append(("event", threading.get_ident()))
            delivered.set()

        def _openadapt_delivery_thread_stop(self) -> None:
            lifecycle.append(("stop", threading.get_ident()))

    observer = _ReadyObserver(Callback())
    observer.start()
    observer._emit(ObservedKey(pressed=True, key_char="a", timestamp=1.0))
    assert delivered.wait(timeout=1)
    observer.stop()

    assert [name for name, _thread_id in lifecycle] == ["start", "event", "stop"]
    assert len({thread_id for _name, thread_id in lifecycle}) == 1


def test_delivery_setup_can_use_a_larger_bound_than_native_setup() -> None:
    """A bounded cold service start must not weaken native-hook readiness."""
    delivery_started = threading.Event()

    class Callback:
        def _openadapt_delivery_thread_start(self) -> None:
            time.sleep(0.05)
            delivery_started.set()

        def __call__(self, _event) -> None:
            return

    observer = _ReadyObserver(Callback())
    observer.startup_timeout = 0.02
    observer.delivery_startup_timeout = 0.2

    observer.start()
    observer.stop()

    assert delivery_started.is_set()


def test_setup_events_are_discarded_when_start_fails() -> None:
    delivered = []
    observer = _SetupEmittingObserver(delivered.append, fail_setup=True)

    with pytest.raises(InputObserverError, match="setup failed after receiving input"):
        observer.start()

    assert observer.health_checked
    assert delivered == []
    assert observer._delivery_queue.empty()
    assert observer._thread is None
    assert observer._delivery_thread is None


def test_setup_delivery_queue_overflow_still_fails_loud() -> None:
    delivered = []
    observer = _SetupEmittingObserver(
        delivered.append,
        delivery_queue_size=1,
    )

    with pytest.raises(InputObserverError, match="delivery queue overflowed"):
        observer.start()

    assert delivered == []
    assert observer._delivery_queue.empty()
    assert observer._thread is None
    assert observer._delivery_thread is None


def test_failed_startup_joins_and_tears_down_before_raising() -> None:
    observer = _CooperativeNeverReadyObserver()

    with pytest.raises(InputObserverError, match="did not become ready"):
        observer.start()

    assert observer._thread is None
    assert observer._delivery_thread is None
    assert observer.torn_down.is_set()
    assert not observer.resource_open


def test_readiness_timeout_surfaces_setup_failure_observed_during_abort() -> None:
    observer = _FailureAfterReadinessTimeoutObserver()

    with pytest.raises(
        InputObserverError,
        match="setup failed while readiness timed out",
    ):
        observer.start()

    assert observer._thread is None
    assert observer._delivery_thread is None
    assert observer.torn_down.is_set()
    assert not observer.resource_open


def test_lingering_failed_startup_cannot_be_reused_as_success() -> None:
    observer = _StubbornNeverReadyObserver()
    try:
        with pytest.raises(InputObserverError, match="did not become ready"):
            observer.start()
        assert observer._thread is not None
        assert observer._thread.is_alive()
        assert observer._delivery_thread is None

        with pytest.raises(InputObserverError, match="live thread from a failed startup"):
            observer.start()
    finally:
        observer.release_setup.set()
        deadline = time.monotonic() + 1
        while observer._thread is not None and observer._thread.is_alive():
            if time.monotonic() >= deadline:
                pytest.fail("stubborn observer thread did not finish after release")
            time.sleep(0.01)
        observer.stop()
    assert observer.torn_down.is_set()
    assert not observer.resource_open


def test_event_thread_start_failure_cleans_delivery_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer = _ReadyObserver()
    original_start = threading.Thread.start

    def fail_event_thread(thread: threading.Thread) -> None:
        if thread.name.endswith("-event-loop"):
            raise RuntimeError("event thread start failed")
        original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_event_thread)

    with pytest.raises(RuntimeError, match="event thread start failed"):
        observer.start()

    assert observer._thread is None
    assert observer._delivery_thread is None


def test_delivery_thread_start_failure_is_transactional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer = _ReadyObserver()

    def fail_start(_thread: threading.Thread) -> None:
        raise RuntimeError("delivery thread start failed")

    monkeypatch.setattr(threading.Thread, "start", fail_start)

    with pytest.raises(RuntimeError, match="delivery thread start failed"):
        observer.start()

    assert observer._thread is None
    assert observer._delivery_thread is None


def test_first_failure_is_preserved_when_wake_also_fails() -> None:
    observer = _ReadyObserver()
    primary = InputObserverError("primary observer failure")

    def fail_wake() -> None:
        raise RuntimeError("wake failed")

    observer._wake = fail_wake  # type: ignore[method-assign]
    observer._fail(primary)

    with pytest.raises(InputObserverError, match="primary observer failure") as caught:
        observer.check_health()
    assert caught.value is primary
    notes = getattr(primary, "__notes__", [])
    if hasattr(primary, "add_note"):
        assert any("wake failed" in note for note in notes)
    else:
        assert notes == []


def test_idempotent_start_surfaces_live_delivery_consumer_failure() -> None:
    callback_entered = threading.Event()

    def fail_consumer(_event) -> None:
        callback_entered.set()
        raise RuntimeError("consumer failed")

    observer = _ReadyObserver(fail_consumer)
    observer.start()
    observer._emit(ObservedKey(pressed=True, key_char="a"))
    assert callback_entered.wait(timeout=1)

    with pytest.raises(InputObserverError, match="consumer failed"):
        observer.start()
    with pytest.raises(InputObserverError, match="consumer failed"):
        observer.stop()


def test_event_shutdown_timeout_still_cleans_delivery_thread() -> None:
    class StubbornLoopObserver(_ReadyObserver):
        def _wake(self) -> None:
            return

    observer = StubbornLoopObserver()
    observer.start()

    with pytest.raises(InputObserverError, match="did not stop"):
        observer.stop()

    assert observer._thread is not None
    assert observer._thread.is_alive()
    assert observer._delivery_thread is None
    observer.release_loop.set()
    observer._thread.join(timeout=1)
    observer.stop()


def test_repeated_stop_retries_lingering_delivery_cleanup() -> None:
    callback_entered = threading.Event()
    release_callback = threading.Event()

    def block_consumer(_event) -> None:
        callback_entered.set()
        release_callback.wait()

    observer = _ReadyObserver(block_consumer)
    observer.start()
    observer._emit(ObservedKey(pressed=True, key_char="a"))
    assert callback_entered.wait(timeout=1)

    with pytest.raises(InputObserverError, match="delivery thread did not stop"):
        observer.stop()
    assert observer._delivery_thread is not None
    assert observer._delivery_thread.is_alive()

    release_callback.set()
    observer._delivery_thread.join(timeout=1)
    with pytest.raises(InputObserverError, match="delivery thread did not stop"):
        observer.stop()
    assert observer._delivery_thread is None


def test_async_delivery_preserves_native_receipt_timestamp_and_order() -> None:
    callback_entered = threading.Event()
    release_callback = threading.Event()
    public_events = []

    def consume(event) -> None:
        if not public_events:
            callback_entered.set()
            assert release_callback.wait(timeout=1)
        public_events.append(input_module._to_public_event(event))

    observer = _ReadyObserver(consume)
    observer.start()
    observer._emit(
        ObservedKey(
            pressed=True,
            key_char="a",
            canonical_key_char="a",
            timestamp=10.25,
        )
    )
    assert callback_entered.wait(timeout=1)
    observer._emit(
        ObservedKey(
            pressed=False,
            key_char="a",
            canonical_key_char="a",
            timestamp=10.5,
        )
    )
    release_callback.set()
    observer.stop()

    assert [event.timestamp for event in public_events] == [10.25, 10.5]


def test_stop_fails_a_native_receipt_that_never_reached_delivery() -> None:
    journal = recorder_module.OrderedEventJournal()

    def consume(_event) -> None:
        return

    setattr(consume, "_openadapt_input_receipt", journal.reserve)
    observer = _ReadyObserver(consume)
    observer.start()
    receipt = observer._reserve_receipt(1.0)

    with pytest.raises(InputObserverError, match="stopped before reserved native input"):
        observer.stop()

    assert receipt is not None
    assert receipt.finished
    with pytest.raises(recorder_module.EventJournalReservationError):
        journal.get_nowait()


def test_terminal_frame_seal_keeps_prior_receipts_and_drops_later_input() -> None:
    journal = recorder_module.OrderedEventJournal()

    def consume(_event) -> None:
        pytest.fail("reserved input must use the receipt consumer")

    def deliver(event, receipt) -> None:
        receipt.complete(
            recorder_module.Event(event.timestamp, "action", {"key": event.key_char})
        )

    setattr(consume, "_openadapt_input_receipt", journal.reserve)
    setattr(consume, "_openadapt_input_delivery", deliver)
    observer = _ReadyObserver(consume)
    observer.start()

    accepted = ObservedKey(pressed=True, key_char="a", timestamp=1.0)
    accepted_receipt = observer._reserve_receipt(accepted.timestamp)
    observer.seal_frame_capture(None)
    observer._emit_received(accepted, accepted_receipt)

    outside = ObservedKey(pressed=True, key_char="b", timestamp=2.0)
    outside_receipt = observer._reserve_receipt(outside.timestamp)
    observer._emit_received(outside, outside_receipt)

    deadline = time.monotonic() + 1
    while not bool(getattr(accepted_receipt, "finished", False)):
        if time.monotonic() >= deadline:
            pytest.fail("the pre-seal receipt did not finish")
        time.sleep(0.001)
    observer.stop()

    action = journal.get_nowait()
    assert (action.data["key"], action.source_ordinal) == ("a", 1)
    with pytest.raises(queue.Empty):
        journal.get_nowait()


def test_generic_frame_cut_rejects_dirty_pixels_and_orders_post_cut_input() -> None:
    journal = recorder_module.OrderedEventJournal()

    def consume(_event) -> None:
        pytest.fail("reserved input must use the receipt consumer")

    def deliver(event, receipt) -> None:
        receipt.complete(
            recorder_module.Event(event.timestamp, "action", {"key": event.key_char})
        )

    setattr(consume, "_openadapt_input_receipt", journal.reserve)
    setattr(consume, "_openadapt_input_delivery", deliver)
    observer = _ReadyObserver(consume)
    observer.start()

    dirty_cut = observer.begin_frame_capture()
    dirty_event = ObservedKey(pressed=True, key_char="a", timestamp=1.0)
    dirty_receipt = observer._reserve_receipt(dirty_event.timestamp)
    assert not observer.finish_frame_capture(dirty_cut)
    observer.complete_frame_capture(dirty_cut)
    observer._emit_received(dirty_event, dirty_receipt)

    clean_cut = observer.begin_frame_capture()
    assert observer.finish_frame_capture(clean_cut)
    reserve_started = threading.Event()
    post_cut: dict[str, object] = {}

    def reserve_post_cut_input() -> None:
        reserve_started.set()
        post_cut["receipt"] = observer._reserve_receipt(3.0)

    reserve_thread = threading.Thread(target=reserve_post_cut_input)
    reserve_thread.start()
    assert reserve_started.wait(timeout=1)
    time.sleep(0.01)
    assert "receipt" not in post_cut
    journal.put(recorder_module.Event(2.0, "screen", {}))
    observer.complete_frame_capture(clean_cut)
    reserve_thread.join(timeout=1)
    assert not reserve_thread.is_alive()

    post_cut_event = ObservedKey(pressed=True, key_char="b", timestamp=3.0)
    observer._emit_received(post_cut_event, post_cut["receipt"])
    deadline = time.monotonic() + 1
    while not bool(getattr(post_cut["receipt"], "finished", False)):
        if time.monotonic() >= deadline:
            pytest.fail("the post-cut receipt did not finish")
        time.sleep(0.001)
    observer.stop()

    assert [journal.get_nowait().type for _ in range(3)] == [
        "action",
        "screen",
        "action",
    ]


def test_consumer_failure_fails_later_queued_receipts() -> None:
    delivery_entered = threading.Event()
    release_delivery = threading.Event()
    receipts = []

    class Receipt:
        def __init__(self) -> None:
            self.finished = False

        def fail(self, _error) -> None:
            self.finished = True

    def consume(_event) -> None:
        raise AssertionError("reserved input must use the receipt consumer")

    def reserve(_timestamp):
        receipt = Receipt()
        receipts.append(receipt)
        return receipt

    def deliver(_event, _receipt) -> None:
        delivery_entered.set()
        assert release_delivery.wait(timeout=1)
        raise RuntimeError("consumer failed")

    setattr(consume, "_openadapt_input_receipt", reserve)
    setattr(consume, "_openadapt_input_delivery", deliver)
    observer = _ReadyObserver(consume)
    observer.start()
    first = ObservedKey(pressed=True, key_char="a", timestamp=1.0)
    observer._emit(first, receipt=observer._reserve_receipt(first.timestamp))
    assert delivery_entered.wait(timeout=1)
    second = ObservedKey(pressed=False, key_char="a", timestamp=2.0)
    observer._emit(second, receipt=observer._reserve_receipt(second.timestamp))
    release_delivery.set()

    deadline = time.monotonic() + 1
    while not all(receipt.finished for receipt in receipts):
        if time.monotonic() >= deadline:
            pytest.fail("queued native receipts were not failed")
        time.sleep(0.001)

    with pytest.raises(InputObserverError, match="consumer failed"):
        observer.stop()


def test_delivery_start_hook_failure_fails_an_already_queued_receipt() -> None:
    journal = recorder_module.OrderedEventJournal()
    hook_entered = threading.Event()
    release_hook = threading.Event()

    class Callback:
        def __call__(self, _event) -> None:
            pytest.fail("reserved input must use the receipt consumer")

        def _openadapt_input_receipt(self, timestamp):
            return journal.reserve(timestamp)

        def _openadapt_input_delivery(self, _event, _receipt) -> None:
            pytest.fail("the failed delivery hook must prevent event delivery")

        def _openadapt_delivery_thread_start(self) -> None:
            hook_entered.set()
            assert release_hook.wait(timeout=1)
            raise RuntimeError("delivery start hook failed")

    class SetupReceiptObserver(_ReadyObserver):
        def _setup(self) -> None:
            event = ObservedKey(pressed=True, key_char="a", timestamp=1.0)
            self.receipt = self._reserve_receipt(event.timestamp)
            self._emit_received(event, self.receipt)

    observer = SetupReceiptObserver(Callback())
    start_errors: list[BaseException] = []

    def start_observer() -> None:
        try:
            observer.start()
        except BaseException as exc:
            start_errors.append(exc)

    start_thread = threading.Thread(target=start_observer)
    start_thread.start()
    assert hook_entered.wait(timeout=1)
    assert start_thread.is_alive()
    release_hook.set()
    start_thread.join(timeout=1)
    assert not start_thread.is_alive()
    assert len(start_errors) == 1
    assert isinstance(start_errors[0], InputObserverError)
    assert "delivery start hook failed" in str(start_errors[0])

    deadline = time.monotonic() + 1
    while not observer.receipt.finished:
        if time.monotonic() >= deadline:
            pytest.fail("delivery start failure did not fail the queued receipt")
        time.sleep(0.001)

    assert observer._delivery_queue.empty()
    with pytest.raises(recorder_module.EventJournalReservationError):
        journal.get_nowait()


def test_stop_sequence_callback_can_stop_listener_from_delivery_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observers: list[_ReadyObserver] = []

    def create(callback, **_kwargs):
        observer = _ReadyObserver(callback)
        observers.append(observer)
        return observer

    monkeypatch.setattr(input_module, "create_input_observer", create)
    listener: input_module.KeyboardListener
    listener = input_module.KeyboardListener(
        lambda _event: None,
        stop_sequences=["q"],
        on_stop_sequence=lambda: listener.stop(),
    )
    listener.start()
    observer = observers[0]
    observer._emit(
        ObservedKey(
            pressed=True,
            key_char="q",
            canonical_key_char="q",
            timestamp=20.0,
        )
    )

    deadline = time.monotonic() + 1
    while observer._delivery_thread is not None and observer._delivery_thread.is_alive():
        if time.monotonic() >= deadline:
            pytest.fail("listener delivery thread did not exit after callback stop")
        time.sleep(0.001)

    assert not listener._running
    assert observer._failure is None


def test_recorder_uses_one_observer_and_preserves_cross_device_receipt_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminate = threading.Event()
    started = threading.Event()
    event_q: queue.Queue = queue.Queue()
    observed = [
        ObservedMouseButton(
            x=1,
            y=2,
            button="left",
            pressed=True,
            timestamp=100.1,
        ),
        ObservedKey(
            pressed=True,
            key_char="a",
            canonical_key_char="a",
            timestamp=100.2,
        ),
        ObservedMouseButton(
            x=1,
            y=2,
            button="left",
            pressed=False,
            timestamp=100.3,
        ),
    ]
    factory_calls = []
    structural_lifecycle = []

    class FakeStructuralObserver:
        def open_current_thread(self) -> None:
            structural_lifecycle.append("start")

        def close_current_thread(self) -> None:
            structural_lifecycle.append("stop")

        def observe(self, _request):
            return None

    structural_observer = FakeStructuralObserver()

    class FakeObserver:
        def __init__(self, callback) -> None:
            self.callback = callback

        def start(self) -> None:
            for event in observed:
                self.callback(event)
            terminate.set()

        def check_health(self) -> None:
            return

        def stop(self) -> None:
            return

    def create(callback, **kwargs):
        factory_calls.append(kwargs)
        callback._openadapt_delivery_thread_start()
        callback._openadapt_delivery_thread_stop()
        return FakeObserver(callback)

    monkeypatch.setattr(recorder_module, "create_input_observer", create)
    recorder_module.read_input_events(
        event_q,
        terminate,
        SimpleNamespace(timestamp=100.0),
        started,
        structural_observer=structural_observer,
    )

    persisted = [event_q.get_nowait() for _ in range(event_q.qsize())]
    assert factory_calls == [
        {
            "observe_keyboard": True,
            "observe_mouse": True,
            "capture_mouse_moves": True,
        }
    ]
    assert started.is_set()
    assert structural_lifecycle == ["start", "stop"]
    assert [event.timestamp for event in persisted] == [100.1, 100.2, 100.3]
    assert [event.data["name"] for event in persisted] == [
        "click",
        "press",
        "click",
    ]


def test_input_reader_skips_terminal_wait_after_startup_cancellation(
    monkeypatch,
) -> None:
    terminate = threading.Event()
    terminate.set()
    terminal_waiting = threading.Event()

    class TrackingEvent(threading.Event):
        def wait(self, timeout=None):
            terminal_waiting.set()
            return super().wait(timeout)

    terminal_finished = TrackingEvent()
    terminal_cancelled = threading.Event()
    boundary = recorder_module.NativeInputFrameBoundary()
    stopped = threading.Event()

    class FakeObserver:
        def start(self) -> None:
            return

        def stop(self) -> None:
            stopped.set()

    monkeypatch.setattr(
        recorder_module,
        "create_input_observer",
        lambda *_args, **_kwargs: FakeObserver(),
    )

    reader = threading.Thread(
        target=recorder_module.read_input_events,
        args=(
            queue.Queue(),
            terminate,
            SimpleNamespace(timestamp=100.0),
            threading.Event(),
        ),
        kwargs={
            "input_frame_boundary": boundary,
            "terminal_frame_finished": terminal_finished,
            "terminal_frame_cancelled": terminal_cancelled,
        },
    )
    reader.start()
    assert terminal_waiting.wait(timeout=1)
    terminal_cancelled.set()
    reader.join(timeout=1)

    assert not reader.is_alive()
    assert stopped.is_set()
    assert not terminal_finished.is_set()


@pytest.mark.parametrize(
    ("detail", "pressed", "expected"),
    [
        (
            1,
            True,
            ObservedMouseButton(
                x=12,
                y=34,
                button="left",
                pressed=True,
            ),
        ),
        (
            2,
            False,
            ObservedMouseButton(
                x=12,
                y=34,
                button="middle",
                pressed=False,
            ),
        ),
        (
            4,
            True,
            ObservedMouseScroll(x=12, y=34, dx=0, dy=1),
        ),
        (
            7,
            True,
            ObservedMouseScroll(x=12, y=34, dx=1, dy=0),
        ),
        (
            8,
            True,
            ObservedMouseButton(
                x=12,
                y=34,
                button="x1",
                pressed=True,
            ),
        ),
        (
            9,
            False,
            ObservedMouseButton(
                x=12,
                y=34,
                button="x2",
                pressed=False,
            ),
        ),
        (4, False, None),
        (0, True, None),
    ],
)
def test_xinput_button_normalization(
    detail: int,
    pressed: bool,
    expected: ObservedMouseButton | ObservedMouseScroll | None,
) -> None:
    assert (
        normalize_xinput_button_event(
            detail=detail,
            pressed=pressed,
            x=12,
            y=34,
        )
        == expected
    )


def test_xinput_key_normalization_preserves_physical_and_canonical_identity() -> None:
    letter = normalize_xinput_key_event(
        keycode=38,
        pressed=True,
        keysym_name="A",
    )
    assert letter == ObservedKey(
        pressed=True,
        key_char="A",
        key_vk="38",
        canonical_key_char="a",
        canonical_key_vk="38",
    )

    modifier = normalize_xinput_key_event(
        keycode=50,
        pressed=False,
        keysym_name="Shift_L",
        injected=True,
    )
    assert modifier.key_name == "shift"
    assert modifier.canonical_key_name == "shift"
    assert modifier.injected


@pytest.mark.parametrize("button", ["x1", "x2", "button4"])
def test_auxiliary_mouse_button_survives_storage_conversion_and_processing(
    button: str,
) -> None:
    down = _convert_action_event(
        SimpleNamespace(
            name="click",
            timestamp=1.0,
            mouse_x=12.5,
            mouse_y=34.5,
            mouse_button_name=button,
            mouse_pressed=True,
        )
    )
    up = _convert_action_event(
        SimpleNamespace(
            name="click",
            timestamp=1.1,
            mouse_x=12.5,
            mouse_y=34.5,
            mouse_button_name=button,
            mouse_pressed=False,
        )
    )

    assert isinstance(down, MouseDownEvent)
    assert isinstance(up, MouseUpEvent)
    assert down.button == button
    assert up.button == button
    processed = merge_consecutive_mouse_click_events([down, up])
    assert len(processed) == 1
    assert isinstance(processed[0], MouseClickEvent)
    assert processed[0].button == button


def test_wayland_refuses_instead_of_silently_observing_only_xwayland(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    observer = LinuxXInputObserver(
        lambda event: None,
        observe_keyboard=True,
        observe_mouse=True,
        capture_mouse_moves=True,
        environ={"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"},
    )

    with pytest.raises(InputObserverUnavailableError, match="silently omit"):
        observer.start()
    assert observer._thread is None


def test_factory_refuses_unknown_platform() -> None:
    with pytest.raises(InputObserverUnavailableError, match="not implemented"):
        create_input_observer(lambda event: None, platform_name="plan9")


def test_exception_note_helper_is_safe_without_python_311_api() -> None:
    class LegacyException:
        pass

    add_exception_note(LegacyException(), "cleanup context")  # type: ignore[arg-type]


def test_record_boundary_reraises_setup_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail_scope(*args, **kwargs):
        raise RuntimeError("native observer setup failed")

    monkeypatch.setattr(
        recorder_module.video,
        "require_video_encoder",
        lambda **_kwargs: recorder_module.video.FFmpegProvision(
            executable="test-ffmpeg",
            source="test",
        ),
    )
    monkeypatch.setattr(recorder_module, "build_window_scope", fail_scope)
    with pytest.raises(RuntimeError, match="native observer setup failed"):
        recorder_module.record("test", capture_dir=str(tmp_path))


def test_post_readiness_worker_failure_reaches_recorder_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    release_failure = threading.Event()

    def fail_after_ready(
        *,
        terminate_processing,
        terminate_recording,
        status_pipe,
        **kwargs,
    ) -> None:
        status_pipe.send({"type": "record.started"})
        assert release_failure.wait(timeout=1)
        raise RuntimeError("native observer failed after readiness")

    monkeypatch.setattr(recorder_module, "record", fail_after_ready)
    recorder = recorder_module.Recorder(str(tmp_path / "late-failure"))

    with pytest.raises(
        RuntimeError,
        match="native observer failed after readiness",
    ):
        with recorder:
            assert recorder.wait_for_ready(timeout=1)
            release_failure.set()

    with pytest.raises(
        RuntimeError,
        match="native observer failed after readiness",
    ):
        _ = recorder.capture


def test_recorder_worker_error_does_not_mask_outer_exception(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    release_failure = threading.Event()

    def fail_after_ready(*, status_pipe, **kwargs) -> None:
        status_pipe.send({"type": "record.started"})
        assert release_failure.wait(timeout=1)
        raise RuntimeError("secondary recorder failure")

    monkeypatch.setattr(recorder_module, "record", fail_after_ready)
    recorder = recorder_module.Recorder(str(tmp_path / "outer-failure"))

    with pytest.raises(ValueError, match="primary caller failure") as caught:
        with recorder:
            assert recorder.wait_for_ready(timeout=1)
            release_failure.set()
            raise ValueError("primary caller failure")

    notes = getattr(caught.value, "__notes__", [])
    if notes:
        assert any("secondary recorder failure" in note for note in notes)

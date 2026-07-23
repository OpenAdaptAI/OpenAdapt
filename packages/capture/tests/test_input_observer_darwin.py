"""Mocked macOS native-input observer contracts."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from openadapt_capture.input_observer.base import (
    InputObserverError,
    InputObserverPermissionError,
    InputObserverUnavailableError,
    ObservedKey,
    ObservedMouseButton,
    ObservedMouseMove,
    ObservedMouseScroll,
)
from openadapt_capture.input_observer.darwin import DarwinInputObserver


class FakeEvent:
    def __init__(
        self,
        *,
        x: float = 10,
        y: float = 20,
        fields: dict[int, int] | None = None,
        text: str = "",
        flags: int = 0,
    ) -> None:
        self.location = SimpleNamespace(x=x, y=y)
        self.fields = fields or {}
        self.text = text
        self.flags = flags


class FakeQuartz:
    kCGEventKeyDown = 1
    kCGEventKeyUp = 2
    kCGEventFlagsChanged = 3
    kCGEventLeftMouseDown = 4
    kCGEventLeftMouseUp = 5
    kCGEventRightMouseDown = 6
    kCGEventRightMouseUp = 7
    kCGEventOtherMouseDown = 8
    kCGEventOtherMouseUp = 9
    kCGEventScrollWheel = 10
    kCGEventMouseMoved = 11
    kCGEventLeftMouseDragged = 12
    kCGEventRightMouseDragged = 13
    kCGEventOtherMouseDragged = 14
    kCGEventTapDisabledByTimeout = 100
    kCGEventTapDisabledByUserInput = 101

    kCGKeyboardEventKeycode = 200
    kCGMouseEventButtonNumber = 201
    kCGScrollWheelEventDeltaAxis1 = 202
    kCGScrollWheelEventDeltaAxis2 = 203
    kCGEventSourceUnixProcessID = 204
    kCGEventFlagMaskAlphaShift = 1 << 0
    kCGEventFlagMaskShift = 1 << 1
    kCGEventFlagMaskControl = 1 << 2
    kCGEventFlagMaskAlternate = 1 << 3
    kCGEventFlagMaskCommand = 1 << 4
    kCGEventFlagMaskSecondaryFn = 1 << 5

    kCGHIDEventTap = 300
    kCGHeadInsertEventTap = 301
    kCGEventTapOptionListenOnly = 302
    kCFRunLoopDefaultMode = "default"

    def __init__(
        self,
        *,
        permission: bool = True,
        grant_on_request: bool = False,
        create_tap: bool = True,
    ) -> None:
        self.permission = permission
        self.grant_on_request = grant_on_request
        self.permission_requests = 0
        self.create_tap = create_tap
        self.tap = object()
        self.loop = object()
        self.source = object()
        self.callback = None
        self.event_mask = None
        self.tap_enabled: list[bool] = []
        self.added_sources = []
        self.removed_sources = []
        self.stopped_loops = []
        self.invalidated_sources = []
        self.invalidated_ports = []

    def CGPreflightListenEventAccess(self):
        return self.permission

    def CGRequestListenEventAccess(self):
        self.permission_requests += 1
        if self.grant_on_request:
            self.permission = True
        return self.permission

    @staticmethod
    def CGEventMaskBit(event_type):
        return 1 << event_type

    def CGEventTapCreate(
        self,
        tap_location,
        placement,
        options,
        event_mask,
        callback,
        refcon,
    ):
        assert tap_location == self.kCGHIDEventTap
        assert placement == self.kCGHeadInsertEventTap
        assert options == self.kCGEventTapOptionListenOnly
        assert refcon is None
        self.event_mask = event_mask
        self.callback = callback
        return self.tap if self.create_tap else None

    def CFRunLoopGetCurrent(self):
        return self.loop

    def CFMachPortCreateRunLoopSource(self, allocator, tap, order):
        assert allocator is None
        assert tap is self.tap
        assert order == 0
        return self.source

    def CFRunLoopAddSource(self, loop, source, mode):
        self.added_sources.append((loop, source, mode))

    @staticmethod
    def CFRunLoopRunInMode(_mode, seconds, _return_after_source):
        time.sleep(min(seconds, 0.002))
        return 0

    def CFRunLoopStop(self, loop):
        self.stopped_loops.append(loop)

    def CFRunLoopRemoveSource(self, loop, source, mode):
        self.removed_sources.append((loop, source, mode))

    def CFRunLoopSourceInvalidate(self, source):
        self.invalidated_sources.append(source)

    def CFMachPortInvalidate(self, port):
        self.invalidated_ports.append(port)

    def CGEventTapEnable(self, tap, enabled):
        assert tap is self.tap
        self.tap_enabled.append(enabled)

    @staticmethod
    def CGEventGetLocation(event):
        return event.location

    @staticmethod
    def CGEventGetIntegerValueField(event, field):
        return event.fields.get(field, 0)

    @staticmethod
    def CGEventGetFlags(event):
        return event.flags

    @staticmethod
    def CGEventKeyboardGetUnicodeString(event, max_length, _length, _buffer):
        text = event.text[:max_length]
        return len(text), text


class FakeApplicationServices:
    def __init__(self, *, trusted: bool = True) -> None:
        self.trusted = trusted

    def AXIsProcessTrusted(self):
        return self.trusted


def make_observer(
    quartz: FakeQuartz,
    callback,
    *,
    observe_keyboard: bool = True,
    observe_mouse: bool = True,
    capture_mouse_moves: bool = True,
    application_services=None,
) -> DarwinInputObserver:
    return DarwinInputObserver(
        callback,
        observe_keyboard=observe_keyboard,
        observe_mouse=observe_mouse,
        capture_mouse_moves=capture_mouse_moves,
        startup_timeout=0.5,
        shutdown_timeout=0.5,
        _quartz=quartz,
        _application_services=application_services or FakeApplicationServices(),
    )


def test_lifecycle_creates_listen_only_tap_and_releases_run_loop() -> None:
    quartz = FakeQuartz()
    observer = make_observer(quartz, lambda _event: None)

    observer.start()
    observer.check_health()
    observer.stop()

    assert quartz.callback is not None
    assert quartz.tap_enabled == [True, False]
    assert quartz.added_sources == [
        (quartz.loop, quartz.source, quartz.kCFRunLoopDefaultMode)
    ]
    assert quartz.removed_sources == quartz.added_sources
    assert quartz.invalidated_sources == [quartz.source]
    assert quartz.invalidated_ports == [quartz.tap]
    assert quartz.loop in quartz.stopped_loops
    for event_type in observer._observed_event_types():
        assert quartz.event_mask & quartz.CGEventMaskBit(event_type)


def test_permission_denial_fails_before_event_tap_creation() -> None:
    quartz = FakeQuartz(permission=False)
    observer = make_observer(quartz, lambda _event: None)

    with pytest.raises(InputObserverPermissionError, match="Input Monitoring"):
        observer.start()
    assert quartz.callback is None
    assert quartz.permission_requests == 1


def test_permission_request_can_complete_explicit_observer_start() -> None:
    quartz = FakeQuartz(permission=False, grant_on_request=True)
    observer = make_observer(quartz, lambda _event: None)

    observer.start()
    observer.stop()

    assert quartz.permission_requests == 1
    assert quartz.callback is not None


def test_accessibility_permission_is_fallback_on_older_macos() -> None:
    quartz = FakeQuartz()
    quartz.CGPreflightListenEventAccess = None
    observer = make_observer(
        quartz,
        lambda _event: None,
        application_services=FakeApplicationServices(trusted=False),
    )

    with pytest.raises(InputObserverPermissionError, match="Input Monitoring"):
        observer.start()
    assert quartz.callback is None


def test_event_tap_creation_failure_is_explicit() -> None:
    quartz = FakeQuartz(create_tap=False)
    observer = make_observer(quartz, lambda _event: None)

    with pytest.raises(
        InputObserverUnavailableError,
        match="listen-only Quartz event tap",
    ):
        observer.start()


def test_mouse_move_button_and_scroll_normalization() -> None:
    quartz = FakeQuartz()
    events = []
    observer = make_observer(quartz, events.append)
    physical = {quartz.kCGEventSourceUnixProcessID: 0}

    observer.start()
    observer._handle_event(
        quartz.kCGEventMouseMoved,
        FakeEvent(x=11.5, y=22.5, fields=physical),
    )
    observer._handle_event(
        quartz.kCGEventLeftMouseDown,
        FakeEvent(x=30, y=40, fields=physical),
    )
    observer._handle_event(
        quartz.kCGEventOtherMouseUp,
        FakeEvent(
            x=50,
            y=60,
            fields={
                **physical,
                quartz.kCGMouseEventButtonNumber: 2,
            },
        ),
    )
    observer._handle_event(
        quartz.kCGEventScrollWheel,
        FakeEvent(
            x=70,
            y=80,
            fields={
                **physical,
                quartz.kCGScrollWheelEventDeltaAxis1: 3,
                quartz.kCGScrollWheelEventDeltaAxis2: -2,
            },
        ),
    )
    observer.stop()

    assert events == [
        ObservedMouseMove(x=11.5, y=22.5),
        ObservedMouseButton(
            x=30,
            y=40,
            button="left",
            pressed=True,
        ),
        ObservedMouseButton(
            x=50,
            y=60,
            button="middle",
            pressed=False,
        ),
        ObservedMouseScroll(x=70, y=80, dx=-2, dy=3),
    ]


def test_move_filter_does_not_disable_buttons() -> None:
    quartz = FakeQuartz()
    events = []
    observer = make_observer(
        quartz,
        events.append,
        capture_mouse_moves=False,
    )

    observer.start()
    observer._handle_event(quartz.kCGEventMouseMoved, FakeEvent())
    observer._handle_event(quartz.kCGEventRightMouseUp, FakeEvent())
    observer.stop()

    assert events == [
        ObservedMouseButton(
            x=10,
            y=20,
            button="right",
            pressed=False,
        )
    ]
    assert quartz.kCGEventMouseMoved not in observer._observed_event_types()


def test_event_callback_stamps_native_receipt_time_before_async_delivery() -> None:
    quartz = FakeQuartz()
    events = []
    observer = make_observer(quartz, events.append)
    event = FakeEvent()

    observer.start()
    before = time.time()
    observer._event_callback(None, quartz.kCGEventMouseMoved, event, None)
    after = time.time()
    observer.stop()

    assert len(events) == 1
    assert events[0].timestamp is not None
    assert before <= events[0].timestamp <= after


def test_key_press_release_and_modifier_canonicalization() -> None:
    quartz = FakeQuartz()
    events = []
    observer = make_observer(quartz, events.append)

    observer.start()
    observer._handle_event(
        quartz.kCGEventKeyDown,
        FakeEvent(
            fields={
                quartz.kCGKeyboardEventKeycode: 0,
                quartz.kCGEventSourceUnixProcessID: 123,
            },
            text="A",
        ),
    )
    observer._handle_event(
        quartz.kCGEventKeyUp,
        FakeEvent(
            fields={quartz.kCGKeyboardEventKeycode: 0},
            text="a",
        ),
    )
    shift_press = FakeEvent(
        fields={quartz.kCGKeyboardEventKeycode: 56},
        flags=quartz.kCGEventFlagMaskShift,
    )
    shift_release = FakeEvent(
        fields={quartz.kCGKeyboardEventKeycode: 56},
    )
    observer._handle_event(quartz.kCGEventFlagsChanged, shift_press)
    observer._handle_event(quartz.kCGEventFlagsChanged, shift_release)
    observer.stop()

    assert events == [
        ObservedKey(
            pressed=True,
            key_char="A",
            key_vk="0",
            canonical_key_char="a",
            canonical_key_vk="0",
            injected=True,
        ),
        ObservedKey(
            pressed=False,
            key_char="a",
            key_vk="0",
            canonical_key_char="a",
            canonical_key_vk="0",
        ),
        ObservedKey(
            pressed=True,
            key_name="shift_l",
            key_vk="56",
            canonical_key_name="shift",
            canonical_key_vk="56",
        ),
        ObservedKey(
            pressed=False,
            key_name="shift_l",
            key_vk="56",
            canonical_key_name="shift",
            canonical_key_vk="56",
        ),
    ]


def test_first_observed_modifier_release_does_not_toggle_to_press() -> None:
    quartz = FakeQuartz()
    events = []
    observer = make_observer(quartz, events.append)

    observer.start()
    observer._handle_event(
        quartz.kCGEventFlagsChanged,
        FakeEvent(
            fields={quartz.kCGKeyboardEventKeycode: 56},
            flags=0,
        ),
    )
    observer.stop()

    assert events == [
        ObservedKey(
            pressed=False,
            key_name="shift_l",
            key_vk="56",
            canonical_key_name="shift",
            canonical_key_vk="56",
        )
    ]


def test_caps_lock_state_comes_from_event_flags() -> None:
    quartz = FakeQuartz()
    events = []
    observer = make_observer(quartz, events.append)

    observer.start()
    observer._handle_event(
        quartz.kCGEventFlagsChanged,
        FakeEvent(
            fields={quartz.kCGKeyboardEventKeycode: 57},
            flags=quartz.kCGEventFlagMaskAlphaShift,
        ),
    )
    observer._handle_event(
        quartz.kCGEventFlagsChanged,
        FakeEvent(
            fields={quartz.kCGKeyboardEventKeycode: 57},
            flags=0,
        ),
    )
    observer.stop()

    assert [(event.key_name, event.pressed) for event in events] == [
        ("caps_lock", True),
        ("caps_lock", False),
    ]


def test_releasing_one_shift_ignores_the_sibling_aggregate_flag() -> None:
    quartz = FakeQuartz()
    events = []
    observer = make_observer(quartz, events.append)

    observer.start()
    for keycode, flags in [
        (56, quartz.kCGEventFlagMaskShift),
        (60, quartz.kCGEventFlagMaskShift),
        (56, quartz.kCGEventFlagMaskShift),
        (60, 0),
    ]:
        observer._handle_event(
            quartz.kCGEventFlagsChanged,
            FakeEvent(
                fields={quartz.kCGKeyboardEventKeycode: keycode},
                flags=flags,
            ),
        )
    observer.stop()

    assert [(event.key_name, event.pressed) for event in events] == [
        ("shift_l", True),
        ("shift_r", True),
        ("shift_l", False),
        ("shift_r", False),
    ]


def test_disabled_tap_fails_loud_instead_of_hiding_incomplete_coverage() -> None:
    quartz = FakeQuartz()
    events = []
    observer = make_observer(quartz, events.append)
    event = FakeEvent()

    observer.start()
    assert (
        observer._event_callback(
            None,
            quartz.kCGEventTapDisabledByTimeout,
            event,
            None,
        )
        is event
    )
    with pytest.raises(InputObserverError, match="coverage is incomplete"):
        observer.check_health()
    with pytest.raises(InputObserverError, match="coverage is incomplete"):
        observer.stop()
    assert quartz.tap_enabled == [True, True, False]
    assert events == []


def test_callback_failure_becomes_health_failure() -> None:
    quartz = FakeQuartz()

    def fail(_event):
        raise ValueError("callback boom")

    observer = make_observer(quartz, fail)
    event = FakeEvent()
    observer._handle_event = (  # type: ignore[method-assign]
        lambda _type, _event, **_kwargs: observer._emit(ObservedMouseMove(1, 2))
    )

    observer.start()
    observer._event_callback(None, quartz.kCGEventMouseMoved, event, None)

    deadline = time.monotonic() + 1
    while True:
        try:
            observer.check_health()
        except InputObserverError as exc:
            assert "callback boom" in str(exc)
            break
        if time.monotonic() >= deadline:
            pytest.fail("asynchronous callback failure did not reach observer health")
        time.sleep(0.001)
    assert observer._stop_requested.is_set()
    with pytest.raises(InputObserverError, match="callback boom"):
        observer.stop()

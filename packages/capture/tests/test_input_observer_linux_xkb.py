"""Linux X RECORD ABI, event-time state, and fail-closed correlation tests."""

from __future__ import annotations

import ctypes
import sys
import time

import pytest

from openadapt_capture.input_observer import (
    InputObserverError,
    ObservedKey,
    ObservedMouseButton,
    ObservedMouseMove,
)
from openadapt_capture.input_observer import linux as linux_module
from openadapt_capture.input_observer.linux import (
    LinuxXInputObserver,
    _CoreWireEvent,
    _decode_core_event,
    _XRecordInterceptData,
    _XRecordRange,
)


class FakeX11:
    def __init__(
        self,
        *,
        keysym: int = 0x61,
        keysym_name: str = "a",
    ) -> None:
        self.keysym = keysym
        self.keysym_name = keysym_name
        self.lookup_states: list[int] = []

    def XkbLookupKeySym(
        self,
        _display,
        _keycode,
        state,
        _consumed_pointer,
        keysym_pointer,
    ) -> int:
        self.lookup_states.append(int(state))
        ctypes.cast(
            keysym_pointer,
            ctypes.POINTER(ctypes.c_ulong),
        ).contents.value = self.keysym
        return 1

    def XKeysymToString(self, _keysym):
        return self.keysym_name.encode()


class FakeXkbCommon:
    def __init__(self, text_by_keysym: dict[int, str]) -> None:
        self.text_by_keysym = text_by_keysym
        self.reset_count = 0

    def xkb_keysym_to_utf8(self, keysym, buffer, size) -> int:
        encoded = self.text_by_keysym.get(int(keysym), "").encode()
        if not encoded:
            return 0
        assert len(encoded) + 1 <= int(size)
        ctypes.memmove(buffer, encoded + b"\0", len(encoded) + 1)
        return len(encoded) + 1

    def xkb_compose_state_reset(self, _state) -> None:
        self.reset_count += 1


class FakeComposeXkbCommon(FakeXkbCommon):
    def __init__(self) -> None:
        super().__init__({0x65: "e", 0x78: "x"})
        self.statuses = iter(
            [
                linux_module._XKB_COMPOSE_COMPOSING,
                linux_module._XKB_COMPOSE_COMPOSED,
                0,
            ]
        )
        self.status = 0

    def xkb_compose_state_feed(self, _state, _keysym) -> int:
        self.status = next(self.statuses)
        return 1

    def xkb_compose_state_get_status(self, _state) -> int:
        return self.status

    def xkb_compose_state_get_utf8(self, _state, buffer, size) -> int:
        encoded = "é".encode()
        assert len(encoded) + 1 <= int(size)
        ctypes.memmove(buffer, encoded + b"\0", len(encoded) + 1)
        return len(encoded)


def make_observer() -> LinuxXInputObserver:
    return LinuxXInputObserver(
        lambda _event: None,
        observe_keyboard=True,
        observe_mouse=True,
        capture_mouse_moves=True,
        environ={"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"},
    )


def wire_event(
    *,
    event_type: int,
    detail: int,
    event_time: int,
    state: int = 0,
    root_x: int = 0,
    root_y: int = 0,
    swapped: bool = False,
) -> bytes:
    byteorder = (
        "big"
        if (sys.byteorder == "little") == swapped
        else "little"
    )
    data = bytearray(32)
    data[0] = event_type
    data[1] = detail
    data[4:8] = event_time.to_bytes(4, byteorder)
    data[8:12] = (0x1234).to_bytes(4, byteorder)
    data[20:22] = root_x.to_bytes(2, byteorder, signed=True)
    data[22:24] = root_y.to_bytes(2, byteorder, signed=True)
    data[28:30] = state.to_bytes(2, byteorder)
    return bytes(data)


def intercept_record(
    observer: LinuxXInputObserver,
    *,
    category: int,
    payload: bytes = b"",
    id_base: int = 0,
    client_swapped: bool = False,
) -> None:
    buffer = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
    recorded = _XRecordInterceptData(
        id_base=id_base,
        category=category,
        client_swapped=client_swapped,
        data=ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
        data_len=len(payload) // 4,
    )
    observer._record_intercept(None, ctypes.pointer(recorded))


def test_direct_setup_initializes_xlib_threads_before_loading_x11(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer = make_observer()
    monkeypatch.setattr(linux_module.sys, "platform", "linux")

    def reject_xlib_threading() -> None:
        raise RuntimeError("XInitThreads refused")

    monkeypatch.setattr(
        linux_module,
        "ensure_xlib_thread_support",
        reject_xlib_threading,
    )
    monkeypatch.setattr(
        linux_module.ctypes.util,
        "find_library",
        lambda _name: pytest.fail("loaded Xlib before XInitThreads succeeded"),
    )

    with pytest.raises(RuntimeError, match="XInitThreads refused"):
        observer._setup()

    assert observer._x11 is None
    assert observer._control_display is None


@pytest.mark.skipif(
    ctypes.sizeof(ctypes.c_void_p) != 8 or ctypes.sizeof(ctypes.c_ulong) != 8,
    reason="ABI offsets below describe X11's 64-bit LP64 data model",
)
def test_xrecord_structures_match_primary_header_lp64_abi() -> None:
    assert ctypes.sizeof(_XRecordRange) == 32
    assert _XRecordRange.delivered_events.offset == 16
    assert _XRecordRange.device_events.offset == 18
    assert _XRecordRange.client_started.offset == 24
    assert ctypes.sizeof(_XRecordInterceptData) == 48
    assert _XRecordInterceptData.category.offset == 24
    assert _XRecordInterceptData.data.offset == 32
    assert _XRecordInterceptData.data_len.offset == 40


@pytest.mark.parametrize("swapped", [False, True])
def test_core_wire_event_decodes_recorded_client_byte_order(swapped: bool) -> None:
    decoded = _decode_core_event(
        wire_event(
            event_type=linux_module._KEY_PRESS,
            detail=38,
            event_time=0x11223344,
            state=0x4282,
            root_x=-120,
            root_y=735,
            swapped=swapped,
        ),
        client_swapped=swapped,
    )

    assert decoded.event_type == linux_module._KEY_PRESS
    assert decoded.detail == 38
    assert decoded.time == 0x11223344
    assert decoded.state == 0x4282
    assert (decoded.root_x, decoded.root_y) == (-120, 735)


def test_control_reply_marker_discards_pre_boundary_input_then_arms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer = make_observer()
    observer._waiting_baseline_marker = True
    observer._control_id_base = 0x400000
    freed: list[int] = []

    class FakeXtst:
        def XRecordFreeData(self, pointer) -> None:
            freed.append(ctypes.addressof(pointer.contents))

    observer._xtst = FakeXtst()
    monkeypatch.setattr(linux_module.time, "time", lambda: 10.0)
    monkeypatch.setattr(linux_module.time, "monotonic", lambda: 20.0)

    def intercept(payload: bytes, *, id_base: int) -> None:
        buffer = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
        recorded = _XRecordInterceptData(
            id_base=id_base,
            category=linux_module._XRECORD_FROM_SERVER,
            data=ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
            data_len=len(payload) // 4,
        )
        observer._record_intercept(None, ctypes.pointer(recorded))

    key = wire_event(
        event_type=linux_module._KEY_PRESS,
        detail=38,
        event_time=7,
    )
    intercept(key, id_base=0)
    assert observer._pending is None
    assert not observer._accepting_events

    marker = bytearray(32)
    marker[0] = 1
    intercept(bytes(marker), id_base=0x400000)
    assert observer._baseline_marker_seen
    assert observer._accepting_events

    intercept(key, id_base=0)
    assert observer._pending is not None
    assert observer._pending.detail == 38
    assert len(freed) == 3


def test_xkb_lookup_uses_state_from_delivered_event() -> None:
    observer = make_observer()
    x11 = FakeX11(keysym=0x20AC, keysym_name="EuroSign")
    observer._control_display = object()
    observer._x11 = x11

    keysym, name = observer._lookup_keysym(26, 0x4282)

    assert (keysym, name) == (0x20AC, "EuroSign")
    assert x11.lookup_states == [0x4282]


def test_dead_key_and_resolving_key_are_text_unverifiable() -> None:
    observer = make_observer()
    observer._xkbcommon = FakeXkbCommon({0x65: "e", 0x78: "x"})

    assert (
        observer._resolved_character(
            keycode=48,
            keysym=0xFE51,
            keysym_name="dead_acute",
            pressed=True,
        )
        is None
    )
    assert (
        observer._resolved_character(
            keycode=26,
            keysym=0x65,
            keysym_name="e",
            pressed=True,
        )
        is None
    )
    assert (
        observer._resolved_character(
            keycode=26,
            keysym=0x65,
            keysym_name="e",
            pressed=False,
        )
        is None
    )
    assert (
        observer._resolved_character(
            keycode=53,
            keysym=0x78,
            keysym_name="x",
            pressed=True,
        )
        == "x"
    )


def test_multi_key_sequence_never_guesses_application_compose_text() -> None:
    observer = make_observer()
    observer._xkbcommon = FakeXkbCommon({0x61: "a", 0x62: "b", 0x78: "x"})

    for keycode, keysym, name in [
        (65, 0xFF20, "Multi_key"),
        (38, 0x61, "a"),
        (56, 0x62, "b"),
    ]:
        assert (
            observer._resolved_character(
                keycode=keycode,
                keysym=keysym,
                keysym_name=name,
                pressed=True,
            )
            is None
        )

    assert (
        observer._resolved_character(
            keycode=53,
            keysym=0x78,
            keysym_name="x",
            pressed=True,
        )
        == "x"
    )


def test_locale_compose_state_commits_exact_text_then_resumes_normal_text() -> None:
    observer = make_observer()
    xkbcommon = FakeComposeXkbCommon()
    observer._xkbcommon = xkbcommon
    observer._compose_state = object()

    assert (
        observer._resolved_character(
            keycode=48,
            keysym=0xFE51,
            keysym_name="dead_acute",
            pressed=True,
        )
        is None
    )
    assert (
        observer._resolved_character(
            keycode=26,
            keysym=0x65,
            keysym_name="e",
            pressed=True,
        )
        == "é"
    )
    assert xkbcommon.reset_count == 1
    assert (
        observer._resolved_character(
            keycode=53,
            keysym=0x78,
            keysym_name="x",
            pressed=True,
        )
        == "x"
    )


def test_device_and_delivered_key_correlate_with_event_time_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = []
    observer = make_observer()
    observer._emit = events.append  # type: ignore[method-assign]
    x11 = FakeX11(keysym=0x20AC, keysym_name="EuroSign")
    observer._x11 = x11
    observer._control_display = object()
    observer._xkbcommon = FakeXkbCommon({0x20AC: "€"})
    monkeypatch.setattr(linux_module.time, "time", lambda: 321.5)
    monkeypatch.setattr(linux_module.time, "monotonic", lambda: 100.0)

    observer._handle_device_event(
        _CoreWireEvent(
            linux_module._KEY_PRESS,
            26,
            False,
            900,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
        )
    )
    observer._handle_delivered_event(
        _CoreWireEvent(
            linux_module._KEY_PRESS,
            26,
            False,
            900,
            0,
            0,
            0,
            50,
            60,
            0,
            0,
            0x4282,
        ),
        id_base=0x400000,
    )
    observer._finalize_pending()

    assert events == [
        ObservedKey(
            pressed=True,
            key_char="€",
            key_vk="26",
            canonical_key_char="€",
            canonical_key_vk="26",
            timestamp=321.5,
        )
    ]
    assert x11.lookup_states == [0x4282]


def test_normative_device_then_duplicate_deliveries_then_next_device_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = []
    observer = make_observer()
    observer._emit = events.append  # type: ignore[method-assign]
    x11 = FakeX11(keysym=0x61, keysym_name="a")
    observer._x11 = x11
    observer._control_display = object()
    observer._xkbcommon = FakeXkbCommon({0x61: "a"})
    wall_times = iter([100.0, 101.0])
    monotonic_times = iter([200.0, 201.0])
    monkeypatch.setattr(linux_module.time, "time", lambda: next(wall_times))
    monkeypatch.setattr(
        linux_module.time,
        "monotonic",
        lambda: next(monotonic_times),
    )

    press = _CoreWireEvent(
        linux_module._KEY_PRESS,
        38,
        False,
        900,
        0,
        0,
        0,
        50,
        60,
        0,
        0,
        0x0001,
    )
    observer._handle_device_event(press)
    observer._handle_delivered_event(press, id_base=0x200000)
    observer._handle_delivered_event(press, id_base=0x400000)
    assert events == []

    release = _CoreWireEvent(
        linux_module._KEY_RELEASE,
        38,
        False,
        901,
        0,
        0,
        0,
        50,
        60,
        0,
        0,
        0x0001,
    )
    observer._handle_device_event(release)
    assert len(events) == 1
    assert events[0] == ObservedKey(
        pressed=True,
        key_char="a",
        key_vk="38",
        canonical_key_char="a",
        canonical_key_vk="38",
        timestamp=100.0,
    )

    observer._handle_delivered_event(release, id_base=0x200000)
    observer._finalize_pending()

    assert events == [
        ObservedKey(
            pressed=True,
            key_char="a",
            key_vk="38",
            canonical_key_char="a",
            canonical_key_vk="38",
            timestamp=100.0,
        ),
        ObservedKey(
            pressed=False,
            key_char="a",
            key_vk="38",
            canonical_key_char="a",
            canonical_key_vk="38",
            timestamp=101.0,
        ),
    ]
    assert x11.lookup_states == [0x0001, 0x0001]


def test_unmatched_key_keeps_physical_identity_and_suppresses_later_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = []
    observer = make_observer()
    observer._emit = events.append  # type: ignore[method-assign]
    observer._xkbcommon = FakeXkbCommon({0x61: "a"})
    observer._compose_state = object()
    monkeypatch.setattr(linux_module.time, "time", lambda: 10.5)
    monkeypatch.setattr(linux_module.time, "monotonic", lambda: 20.0)
    observer._handle_device_event(
        _CoreWireEvent(
            linux_module._KEY_PRESS,
            38,
            False,
            1,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
        )
    )

    observer._finalize_pending()

    assert events == [
        ObservedKey(
            pressed=True,
            key_name="keycode_38",
            key_vk="38",
            canonical_key_name="keycode_38",
            canonical_key_vk="38",
            timestamp=10.5,
        )
    ]
    assert observer._text_state_uncertain
    assert (
        observer._resolved_character(
            keycode=38,
            keysym=0x61,
            keysym_name="a",
            pressed=True,
        )
        is None
    )


def test_motion_uses_event_time_root_coordinates_without_pointer_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = []
    observer = make_observer()
    observer._emit = events.append  # type: ignore[method-assign]
    monkeypatch.setattr(linux_module.time, "time", lambda: 44.0)

    observer._handle_device_event(
        _CoreWireEvent(
            linux_module._MOTION_NOTIFY,
            0,
            False,
            2,
            0,
            0,
            0,
            -120,
            735,
            0,
            0,
            0,
        )
    )

    assert observer._last_pointer == (-120.0, 735.0)
    assert events == [
        ObservedMouseMove(x=-120.0, y=735.0, timestamp=44.0)
    ]


def test_unmatched_button_uses_last_exact_recorded_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = []
    observer = make_observer()
    observer._emit = events.append  # type: ignore[method-assign]
    observer._last_pointer = (12.0, 34.0)
    monkeypatch.setattr(linux_module.time, "time", lambda: 70.0)
    monkeypatch.setattr(linux_module.time, "monotonic", lambda: 80.0)
    observer._handle_device_event(
        _CoreWireEvent(
            linux_module._BUTTON_PRESS,
            1,
            False,
            3,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
        )
    )

    observer._finalize_pending()

    assert events == [
        ObservedMouseButton(
            x=12.0,
            y=34.0,
            button="left",
            pressed=True,
            timestamp=70.0,
        )
    ]


def test_stationary_first_click_uses_pre_ready_baseline_without_resampling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = []
    observer = make_observer()
    observer._emit = events.append  # type: ignore[method-assign]
    observer._control_display = object()
    observer._root = 99

    class BaselineX11:
        def __init__(self) -> None:
            self.query_count = 0

        def XQueryPointer(
            self,
            _display,
            _root,
            _root_return,
            _child_return,
            root_x_pointer,
            root_y_pointer,
            _window_x_pointer,
            _window_y_pointer,
            _mask_pointer,
        ) -> int:
            self.query_count += 1
            ctypes.cast(root_x_pointer, ctypes.POINTER(ctypes.c_int)).contents.value = 41
            ctypes.cast(root_y_pointer, ctypes.POINTER(ctypes.c_int)).contents.value = 73
            return 1

    x11 = BaselineX11()
    observer._x11 = x11
    observer._query_pointer_baseline()
    observer._accepting_events = True
    monkeypatch.setattr(linux_module.time, "time", lambda: 123.0)
    monkeypatch.setattr(linux_module.time, "monotonic", lambda: 80.0)
    observer._handle_device_event(
        _CoreWireEvent(
            linux_module._BUTTON_PRESS,
            1,
            False,
            3,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
        )
    )
    observer._finalize_pending()

    assert x11.query_count == 1
    assert events == [
        ObservedMouseButton(
            x=41.0,
            y=73.0,
            button="left",
            pressed=True,
            timestamp=123.0,
        )
    ]


def test_unmatched_button_without_exact_position_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer = make_observer()
    monkeypatch.setattr(linux_module.time, "monotonic", lambda: 80.0)
    observer._handle_device_event(
        _CoreWireEvent(
            linux_module._BUTTON_PRESS,
            1,
            False,
            3,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
        )
    )

    with pytest.raises(InputObserverError, match="without event-time coordinates"):
        observer._finalize_pending()


def test_conflicting_delivered_candidates_fail_instead_of_choosing_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer = make_observer()
    monkeypatch.setattr(linux_module.time, "monotonic", lambda: 1.0)
    observer._handle_device_event(
        _CoreWireEvent(
            linux_module._KEY_PRESS,
            38,
            False,
            5,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
        )
    )
    observer._handle_delivered_event(
        _CoreWireEvent(
            linux_module._KEY_PRESS,
            38,
            False,
            5,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            1,
        ),
        id_base=0x200000,
    )

    with pytest.raises(InputObserverError, match="conflicting event-time state"):
        observer._handle_delivered_event(
            _CoreWireEvent(
                linux_module._KEY_PRESS,
                38,
                False,
                5,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                2,
            ),
            id_base=0x400000,
        )


def test_new_device_finalizes_prior_event_before_it() -> None:
    events = []
    observer = make_observer()
    observer._emit = events.append  # type: ignore[method-assign]
    observer._last_pointer = (10.0, 20.0)
    observer._handle_device_event(
        _CoreWireEvent(
            linux_module._KEY_PRESS,
            38,
            False,
            10,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
        )
    )
    observer._handle_device_event(
        _CoreWireEvent(
            linux_module._MOTION_NOTIFY,
            0,
            False,
            11,
            0,
            0,
            0,
            30,
            40,
            0,
            0,
            0,
        )
    )

    assert isinstance(events[0], ObservedKey)
    assert isinstance(events[1], ObservedMouseMove)
    assert (events[1].x, events[1].y) == (30.0, 40.0)


def test_idle_timeout_finalizes_unmatched_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = []
    observer = make_observer()
    observer._emit = events.append  # type: ignore[method-assign]
    moments = iter([1.0, 1.05, 1.11])
    monkeypatch.setattr(linux_module.time, "monotonic", lambda: next(moments))
    observer._handle_device_event(
        _CoreWireEvent(
            linux_module._KEY_PRESS,
            38,
            False,
            12,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
        )
    )

    observer._finalize_expired_pending()
    assert events == []
    observer._finalize_expired_pending()
    assert len(events) == 1
    assert isinstance(events[0], ObservedKey)


def test_shutdown_drain_emits_buffered_tail_through_end_of_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = []
    observer = make_observer()
    observer._emit = events.append  # type: ignore[method-assign]
    observer._accepting_events = True
    observer._x11 = FakeX11(keysym=0x61, keysym_name="a")
    observer._control_display = object()
    observer._xkbcommon = FakeXkbCommon({0x61: "a"})
    monkeypatch.setattr(linux_module.time, "time", lambda: 51.0)

    class TailXtst:
        def __init__(self) -> None:
            self.process_count = 0
            self.free_count = 0

        def XRecordProcessReplies(self, _display) -> None:
            self.process_count += 1
            device = wire_event(
                event_type=linux_module._KEY_PRESS,
                detail=38,
                event_time=500,
            )
            delivered = wire_event(
                event_type=linux_module._KEY_PRESS,
                detail=38,
                event_time=500,
                state=1,
            )
            intercept_record(
                observer,
                category=linux_module._XRECORD_FROM_SERVER,
                payload=device,
            )
            intercept_record(
                observer,
                category=linux_module._XRECORD_FROM_SERVER,
                payload=delivered,
                id_base=0x400000,
            )
            intercept_record(
                observer,
                category=linux_module._XRECORD_END_OF_DATA,
            )

        def XRecordFreeData(self, _pointer) -> None:
            self.free_count += 1

    xtst = TailXtst()
    observer._xtst = xtst

    observer._drain_record_tail()

    assert observer._record_ended
    assert xtst.process_count == 1
    assert xtst.free_count == 3
    assert events == [
        ObservedKey(
            pressed=True,
            key_char="a",
            key_vk="38",
            canonical_key_char="a",
            canonical_key_vk="38",
            timestamp=51.0,
        )
    ]


def test_shutdown_drain_times_out_without_end_of_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer = make_observer()
    observer.shutdown_timeout = 0.05
    process_count = 0

    class NeverEndsXtst:
        def XRecordProcessReplies(self, _display) -> None:
            nonlocal process_count
            process_count += 1

    observer._xtst = NeverEndsXtst()
    moments = iter([10.0, 10.1])
    monkeypatch.setattr(linux_module.time, "monotonic", lambda: next(moments))

    with pytest.raises(InputObserverError, match="did not deliver EndOfData"):
        observer._drain_record_tail()

    assert process_count == 1


def test_teardown_drains_then_releases_every_native_resource() -> None:
    observer = make_observer()
    observer.shutdown_timeout = 0.1
    control_display = object()
    data_display = object()
    record_range = object()
    compose_state = object()
    compose_table = object()
    compose_context = object()
    observer._record_enabled = True
    observer._record_context = 7
    observer._control_display = control_display
    observer._data_display = data_display
    observer._record_range = record_range
    observer._compose_state = compose_state
    observer._compose_table = compose_table
    observer._compose_context = compose_context

    class TeardownXtst:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.free_data_count = 0

        def XRecordDisableContext(self, display, context) -> int:
            assert display is control_display
            assert context == 7
            self.calls.append("disable")
            return 1

        def XRecordProcessReplies(self, display) -> None:
            assert display is data_display
            self.calls.append("process")
            intercept_record(
                observer,
                category=linux_module._XRECORD_END_OF_DATA,
            )

        def XRecordFreeData(self, _pointer) -> None:
            self.free_data_count += 1

        def XRecordFreeContext(self, display, context) -> int:
            assert display is control_display
            assert context == 7
            self.calls.append("free_context")
            return 1

    class TeardownX11:
        def __init__(self) -> None:
            self.freed = []
            self.closed = []

        def XFree(self, value) -> int:
            self.freed.append(value)
            return 1

        def XCloseDisplay(self, value) -> int:
            self.closed.append(value)
            return 0

    class TeardownXkb:
        def __init__(self) -> None:
            self.released = []

        def xkb_compose_state_unref(self, value) -> None:
            self.released.append(("state", value))

        def xkb_compose_table_unref(self, value) -> None:
            self.released.append(("table", value))

        def xkb_context_unref(self, value) -> None:
            self.released.append(("context", value))

    xtst = TeardownXtst()
    x11 = TeardownX11()
    xkb = TeardownXkb()
    observer._xtst = xtst
    observer._x11 = x11
    observer._xkbcommon = xkb

    observer._teardown()

    assert xtst.calls == ["disable", "process", "free_context"]
    assert xtst.free_data_count == 1
    assert x11.freed == [record_range]
    assert x11.closed == [data_display, control_display]
    assert xkb.released == [
        ("state", compose_state),
        ("table", compose_table),
        ("context", compose_context),
    ]
    assert observer._record_context == 0
    assert observer._data_display is None
    assert observer._control_display is None


def test_setup_failure_without_enabled_context_skips_shutdown_drain() -> None:
    observer = make_observer()
    observer._record_enabled = False
    observer._record_context = 7
    observer._control_display = object()
    observer._data_display = object()

    class SetupFailureXtst:
        def __init__(self) -> None:
            self.process_count = 0
            self.free_context_count = 0

        def XRecordProcessReplies(self, _display) -> None:
            self.process_count += 1

        def XRecordFreeContext(self, _display, _context) -> int:
            self.free_context_count += 1
            return 1

    class SetupFailureX11:
        def XCloseDisplay(self, _display) -> int:
            return 0

    xtst = SetupFailureXtst()
    observer._xtst = xtst
    observer._x11 = SetupFailureX11()
    observer._xkbcommon = None

    observer._teardown()

    assert xtst.process_count == 0
    assert xtst.free_context_count == 1


def test_failed_start_drain_never_arms_or_emits_buffered_input() -> None:
    events = []
    observer = make_observer()
    observer._emit = events.append  # type: ignore[method-assign]
    observer._record_enabled = True
    observer._record_context = 7
    observer._control_display = object()
    observer._data_display = object()
    observer._control_id_base = 0x400000
    observer._waiting_baseline_marker = True
    observer._accepting_events = False
    observer._setup_complete = False

    class FailedStartXtst:
        def __init__(self) -> None:
            self.free_data_count = 0

        def XRecordDisableContext(self, _display, _context) -> int:
            return 1

        def XRecordProcessReplies(self, _display) -> None:
            marker = bytearray(32)
            marker[0] = 1
            device = wire_event(
                event_type=linux_module._KEY_PRESS,
                detail=38,
                event_time=500,
            )
            delivered = wire_event(
                event_type=linux_module._KEY_PRESS,
                detail=38,
                event_time=500,
                state=1,
            )
            intercept_record(
                observer,
                category=linux_module._XRECORD_FROM_SERVER,
                payload=bytes(marker),
                id_base=observer._control_id_base,
            )
            intercept_record(
                observer,
                category=linux_module._XRECORD_FROM_SERVER,
                payload=device,
            )
            intercept_record(
                observer,
                category=linux_module._XRECORD_FROM_SERVER,
                payload=delivered,
                id_base=0x800000,
            )
            intercept_record(
                observer,
                category=linux_module._XRECORD_END_OF_DATA,
            )

        def XRecordFreeData(self, _pointer) -> None:
            self.free_data_count += 1

        def XRecordFreeContext(self, _display, _context) -> int:
            return 1

    class FailedStartX11:
        def XCloseDisplay(self, _display) -> int:
            return 0

    xtst = FailedStartXtst()
    observer._xtst = xtst
    observer._x11 = FailedStartX11()
    observer._xkbcommon = None

    observer._teardown()

    assert events == []
    assert xtst.free_data_count == 4
    assert not observer._setup_complete
    assert not observer._baseline_marker_seen
    assert not observer._waiting_baseline_marker
    assert not observer._accepting_events


def exercise_cancelled_start_lifecycle(
    *,
    include_buffered_tail: bool,
    enter_marker_wait_after_cancel: bool = False,
    complete_setup_before_cancel: bool = False,
) -> tuple[LinuxXInputObserver, list, object, float]:
    events = []

    class LifecycleX11:
        def __init__(self) -> None:
            self.close_count = 0

        def XCloseDisplay(self, _display) -> int:
            self.close_count += 1
            return 0

    class LifecycleXtst:
        def __init__(self) -> None:
            self.observer: LinuxXInputObserver | None = None
            self.disabled = False
            self.tail_sent = False
            self.process_before_disable = 0
            self.free_data_count = 0
            self.free_context_count = 0

        def XRecordDisableContext(self, _display, _context) -> int:
            self.disabled = True
            return 1

        def XRecordProcessReplies(self, _display) -> None:
            if not self.disabled:
                self.process_before_disable += 1
                return
            if self.tail_sent:
                return
            self.tail_sent = True
            assert self.observer is not None
            if include_buffered_tail:
                marker = bytearray(32)
                marker[0] = 1
                intercept_record(
                    self.observer,
                    category=linux_module._XRECORD_FROM_SERVER,
                    payload=bytes(marker),
                    id_base=self.observer._control_id_base,
                )
                intercept_record(
                    self.observer,
                    category=linux_module._XRECORD_FROM_SERVER,
                    payload=wire_event(
                        event_type=linux_module._KEY_PRESS,
                        detail=38,
                        event_time=700,
                    ),
                )
                intercept_record(
                    self.observer,
                    category=linux_module._XRECORD_FROM_SERVER,
                    payload=wire_event(
                        event_type=linux_module._KEY_PRESS,
                        detail=38,
                        event_time=700,
                        state=1,
                    ),
                    id_base=0x800000,
                )
            intercept_record(
                self.observer,
                category=linux_module._XRECORD_END_OF_DATA,
            )

        def XRecordFreeData(self, _pointer) -> None:
            self.free_data_count += 1

        def XRecordFreeContext(self, _display, _context) -> int:
            self.free_context_count += 1
            return 1

    x11 = LifecycleX11()
    xtst = LifecycleXtst()

    class ControlledLifecycleObserver(LinuxXInputObserver):
        def _setup(self) -> None:
            self._x11 = x11
            self._xtst = xtst
            self._xkbcommon = None
            self._control_display = object()
            self._data_display = object()
            self._record_context = 11
            self._record_enabled = True
            self._record_started = True
            self._control_id_base = 0x400000
            self._waiting_baseline_marker = True
            if complete_setup_before_cancel:
                self._waiting_baseline_marker = False
                self._baseline_marker_seen = True
                self._accepting_events = True
                self._setup_complete = True
                assert self._stop_requested.wait(self.shutdown_timeout)
                return
            if enter_marker_wait_after_cancel:
                assert self._stop_requested.wait(self.shutdown_timeout)
            self._wait_for_baseline_marker(timeout=self.shutdown_timeout * 2)
            self._raise_if_setup_cancelled()
            if not self._accepting_events:
                raise InputObserverError(
                    "test boundary was observed without arming input"
                )
            self._setup_complete = True

    observer = ControlledLifecycleObserver(
        events.append,
        observe_keyboard=True,
        observe_mouse=True,
        capture_mouse_moves=True,
        startup_timeout=0.02,
        shutdown_timeout=0.2,
        environ={"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"},
    )
    xtst.observer = observer
    started_at = time.monotonic()
    with pytest.raises(InputObserverError, match="did not become ready"):
        observer.start()
    elapsed = time.monotonic() - started_at

    assert x11.close_count == 2
    return observer, events, xtst, elapsed


def test_start_timeout_discards_delayed_marker_and_buffered_input_tail() -> None:
    observer, events, xtst, elapsed = exercise_cancelled_start_lifecycle(
        include_buffered_tail=True,
    )

    assert events == []
    assert xtst.tail_sent
    assert xtst.free_data_count == 4
    assert xtst.free_context_count == 1
    assert observer._thread is None
    assert observer._delivery_thread is None
    assert elapsed < observer.startup_timeout + observer.shutdown_timeout + 0.1


def test_start_cancellation_before_marker_wait_returns_without_polling() -> None:
    observer, events, xtst, elapsed = exercise_cancelled_start_lifecycle(
        include_buffered_tail=False,
        enter_marker_wait_after_cancel=True,
    )

    assert events == []
    assert xtst.tail_sent
    assert xtst.process_before_disable == 0
    assert xtst.free_data_count == 1
    assert xtst.free_context_count == 1
    assert observer._thread is None
    assert elapsed < observer.startup_timeout + observer.shutdown_timeout + 0.1


def test_timeout_after_setup_before_ready_still_suppresses_tail() -> None:
    observer, events, xtst, elapsed = exercise_cancelled_start_lifecycle(
        include_buffered_tail=True,
        complete_setup_before_cancel=True,
    )

    assert events == []
    assert xtst.tail_sent
    assert xtst.free_data_count == 4
    assert xtst.free_context_count == 1
    assert observer._thread is None
    assert elapsed < observer.startup_timeout + observer.shutdown_timeout + 0.1


def test_outer_timeout_cancels_already_armed_record_batch_transactionally() -> None:
    events = []

    class LifecycleX11:
        def __init__(self) -> None:
            self.close_count = 0

        def XCloseDisplay(self, _display) -> int:
            self.close_count += 1
            return 0

    class CancelledBatchObserver(LinuxXInputObserver):
        def __init__(self) -> None:
            super().__init__(
                events.append,
                observe_keyboard=True,
                observe_mouse=True,
                capture_mouse_moves=True,
                startup_timeout=0.02,
                shutdown_timeout=0.2,
                environ={"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"},
            )
            self.device_records_processed = 0
            self.delivered_records_processed = 0

        def _handle_device_event(self, event: _CoreWireEvent) -> None:
            self.device_records_processed += 1
            super()._handle_device_event(event)

        def _handle_delivered_event(
            self,
            event: _CoreWireEvent,
            *,
            id_base: int,
        ) -> None:
            self.delivered_records_processed += 1
            super()._handle_delivered_event(event, id_base=id_base)

        def _setup(self) -> None:
            self._x11 = x11
            self._xtst = xtst
            self._xkbcommon = None
            self._control_display = object()
            self._data_display = object()
            self._record_context = 11
            self._record_enabled = True
            self._record_started = True
            self._control_id_base = 0x400000
            self._waiting_baseline_marker = True
            self._wait_for_baseline_marker(timeout=self.shutdown_timeout * 2)
            self._raise_if_setup_cancelled()
            self._setup_complete = True

    class LifecycleXtst:
        def __init__(self) -> None:
            self.observer: CancelledBatchObserver | None = None
            self.disabled = False
            self.pre_disable_batch_sent = False
            self.end_sent = False
            self.free_data_count = 0
            self.free_context_count = 0

        def XRecordDisableContext(self, _display, _context) -> int:
            self.disabled = True
            return 1

        def XRecordProcessReplies(self, _display) -> None:
            assert self.observer is not None
            if not self.disabled and not self.pre_disable_batch_sent:
                self.pre_disable_batch_sent = True
                marker = bytearray(32)
                marker[0] = 1
                intercept_record(
                    self.observer,
                    category=linux_module._XRECORD_FROM_SERVER,
                    payload=bytes(marker),
                    id_base=self.observer._control_id_base,
                )
                assert self.observer._accepting_events
                # Model a native batch already copied by ProcessReplies while
                # the parent start() deadline expires between its marker and
                # remaining records.
                assert self.observer._stop_requested.wait(timeout=1)
                intercept_record(
                    self.observer,
                    category=linux_module._XRECORD_FROM_SERVER,
                    payload=wire_event(
                        event_type=linux_module._KEY_PRESS,
                        detail=38,
                        event_time=800,
                    ),
                )
                intercept_record(
                    self.observer,
                    category=linux_module._XRECORD_FROM_SERVER,
                    payload=wire_event(
                        event_type=linux_module._KEY_PRESS,
                        detail=38,
                        event_time=800,
                        state=1,
                    ),
                    id_base=0x800000,
                )
                intercept_record(
                    self.observer,
                    category=linux_module._XRECORD_FROM_SERVER,
                    payload=wire_event(
                        event_type=linux_module._MOTION_NOTIFY,
                        detail=0,
                        event_time=801,
                        root_x=30,
                        root_y=40,
                    ),
                )
                assert self.observer.device_records_processed == 0
                assert self.observer.delivered_records_processed == 0
                return
            if self.disabled and not self.end_sent:
                self.end_sent = True
                intercept_record(
                    self.observer,
                    category=linux_module._XRECORD_END_OF_DATA,
                )

        def XRecordFreeData(self, _pointer) -> None:
            self.free_data_count += 1

        def XRecordFreeContext(self, _display, _context) -> int:
            self.free_context_count += 1
            return 1

    x11 = LifecycleX11()
    xtst = LifecycleXtst()
    observer = CancelledBatchObserver()
    xtst.observer = observer

    with pytest.raises(InputObserverError, match="did not become ready"):
        observer.start()

    assert events == []
    assert observer.device_records_processed == 0
    assert observer.delivered_records_processed == 0
    assert xtst.pre_disable_batch_sent
    assert xtst.end_sent
    assert xtst.free_data_count == 5
    assert xtst.free_context_count == 1
    assert x11.close_count == 2
    assert observer._thread is None
    assert observer._delivery_thread is None

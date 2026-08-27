"""Focused contract tests for the stdlib Windows input observer."""

from __future__ import annotations

import ctypes
import threading
import time

import pytest

from openadapt_capture.input_observer import (
    InputObserverError,
    InputObserverPermissionError,
    ObservedKey,
    ObservedMouseButton,
    ObservedMouseMove,
    ObservedMouseScroll,
)
from openadapt_capture.input_observer.windows import (
    ERROR_ACCESS_DENIED,
    HC_ACTION,
    KBDLLHOOKSTRUCT,
    LLKHF_INJECTED,
    LLMHF_INJECTED,
    MSLLHOOKSTRUCT,
    POINT,
    VK_CAPITAL,
    VK_SHIFT,
    WM_KEYDOWN,
    WM_KEYUP,
    WM_LBUTTONDOWN,
    WM_MOUSEHWHEEL,
    WM_MOUSEMOVE,
    WM_MOUSEWHEEL,
    WM_XBUTTONUP,
    XBUTTON2,
    WindowsInputObserver,
    _key_identity,
    _mouse_event,
    _signed_high_word,
)


class FakeKernel32:
    def GetCurrentThreadId(self) -> int:
        return 4242

    def GetModuleHandleW(self, _name):
        return 99


class FakeUser32:
    def __init__(
        self,
        *,
        hook_results: list[int] | None = None,
        last_error: int = 0,
        unhook_failures: set[int] | None = None,
        foreground_thread_id: int = 8080,
        keyboard_layout: int = 1,
        translation_entered: threading.Event | None = None,
        translation_release: threading.Event | None = None,
        translation_error: BaseException | None = None,
        translation_result: int = 1,
        translation_script: list[tuple[int, str | None]] | None = None,
    ) -> None:
        self.hook_results = list(hook_results or [101, 102])
        self.last_error = last_error
        self.unhook_failures = unhook_failures or set()
        self.installed: list[int] = []
        self.unhooked: list[int] = []
        self.next_calls: list[tuple[object, int, int, int]] = []
        self.posted = threading.Event()
        self.foreground_thread_id = foreground_thread_id
        self.keyboard_layout = keyboard_layout
        self.translation_entered = translation_entered
        self.translation_release = translation_release
        self.translation_error = translation_error
        self.translation_result = translation_result
        self.translation_script = list(translation_script or [])
        self.async_key_state: dict[int, int] = {}
        self.toggle_key_state: dict[int, int] = {}
        self.layout_thread_ids: list[int] = []
        self.translation_layouts: list[int] = []

    def PeekMessageW(self, *_args) -> int:
        return 0

    def SetWindowsHookExW(self, hook_type, _callback, _module, _thread_id):
        result = self.hook_results.pop(0)
        if result:
            self.installed.append(hook_type)
        return result

    def UnhookWindowsHookEx(self, hook) -> int:
        self.unhooked.append(int(hook))
        return int(hook not in self.unhook_failures)

    def CallNextHookEx(self, hook, code, wparam, lparam) -> int:
        self.next_calls.append((hook, code, wparam, lparam))
        return 73

    def GetMessageW(self, *_args) -> int:
        self.posted.wait(timeout=2)
        return 0

    def TranslateMessage(self, *_args) -> int:
        return 1

    def DispatchMessageW(self, *_args) -> int:
        return 1

    def PostThreadMessageW(self, *_args) -> int:
        self.posted.set()
        return 1

    def GetAsyncKeyState(self, vk_code) -> int:
        return self.async_key_state.get(vk_code, 0)

    def GetKeyState(self, vk_code) -> int:
        return self.toggle_key_state.get(vk_code, 0)

    def GetForegroundWindow(self) -> int:
        return 9001

    def GetWindowThreadProcessId(self, hwnd, process_id) -> int:
        assert hwnd == 9001
        assert process_id is None
        return self.foreground_thread_id

    def ToUnicodeEx(
        self,
        vk_code,
        _scan_code,
        keyboard_state,
        buffer,
        _buffer_size,
        flags,
        _layout,
    ) -> int:
        assert flags == 4
        self.translation_layouts.append(int(_layout))
        if self.translation_entered is not None:
            self.translation_entered.set()
        if self.translation_release is not None:
            self.translation_release.wait(timeout=2)
        if self.translation_error is not None:
            raise self.translation_error
        if self.translation_script:
            result, scripted_character = self.translation_script.pop(0)
            if scripted_character is not None:
                buffer[0] = scripted_character
            return result
        character = chr(vk_code)
        shift = bool(keyboard_state[VK_SHIFT] & 0x80)
        caps_lock = bool(keyboard_state[VK_CAPITAL] & 0x01)
        if shift ^ caps_lock:
            character = character.upper()
        else:
            character = character.lower()
        buffer[0] = character
        return self.translation_result

    def GetKeyboardLayout(self, thread_id):
        self.layout_thread_ids.append(thread_id)
        return self.keyboard_layout


def make_observer(
    callback,
    *,
    user32: FakeUser32 | None = None,
    observe_keyboard: bool = True,
    observe_mouse: bool = True,
    capture_mouse_moves: bool = True,
    delivery_queue_size: int = 4096,
    translation_queue_size: int | None = None,
    clock=lambda: 1234.5,
) -> WindowsInputObserver:
    return WindowsInputObserver(
        callback,
        observe_keyboard=observe_keyboard,
        observe_mouse=observe_mouse,
        capture_mouse_moves=capture_mouse_moves,
        startup_timeout=1,
        shutdown_timeout=1,
        delivery_queue_size=delivery_queue_size,
        translation_queue_size=translation_queue_size,
        _user32=user32 or FakeUser32(),
        _kernel32=FakeKernel32(),
        _clock=clock,
    )


def wait_until(predicate, *, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


def test_signed_wheel_delta_and_mouse_normalization() -> None:
    assert _signed_high_word(120 << 16) == 120
    assert _signed_high_word(((-240) & 0xFFFF) << 16) == -240

    vertical = MSLLHOOKSTRUCT(
        pt=POINT(-10, 25),
        mouseData=((-240) & 0xFFFF) << 16,
    )
    horizontal = MSLLHOOKSTRUCT(
        pt=POINT(30, 40),
        mouseData=(120 & 0xFFFF) << 16,
    )
    assert _mouse_event(
        WM_MOUSEWHEEL,
        vertical,
        capture_mouse_moves=True,
    ) == ObservedMouseScroll(x=-10.0, y=25.0, dx=0.0, dy=-2.0)
    assert _mouse_event(
        WM_MOUSEHWHEEL,
        horizontal,
        capture_mouse_moves=True,
    ) == ObservedMouseScroll(x=30.0, y=40.0, dx=1.0, dy=0.0)


def test_mouse_move_button_xbutton_and_injected_filtering() -> None:
    payload = MSLLHOOKSTRUCT(pt=POINT(12, 34))
    assert _mouse_event(
        WM_MOUSEMOVE,
        payload,
        capture_mouse_moves=True,
    ) == ObservedMouseMove(x=12.0, y=34.0)
    assert (
        _mouse_event(
            WM_MOUSEMOVE,
            payload,
            capture_mouse_moves=False,
        )
        is None
    )
    assert _mouse_event(
        WM_LBUTTONDOWN,
        payload,
        capture_mouse_moves=True,
    ) == ObservedMouseButton(x=12.0, y=34.0, button="left", pressed=True)

    payload.mouseData = XBUTTON2 << 16
    assert _mouse_event(
        WM_XBUTTONUP,
        payload,
        capture_mouse_moves=True,
    ) == ObservedMouseButton(x=12.0, y=34.0, button="x2", pressed=False)

    payload.flags = LLMHF_INJECTED
    assert (
        _mouse_event(
            WM_LBUTTONDOWN,
            payload,
            capture_mouse_moves=True,
        )
        is None
    )


def test_key_identity_preserves_physical_and_canonical_semantics() -> None:
    assert _key_identity(0x41, "A") == {
        "key_name": None,
        "key_char": "A",
        "key_vk": "65",
        "canonical_key_name": None,
        "canonical_key_char": "a",
        "canonical_key_vk": "65",
    }
    assert _key_identity(0xA1, None) == {
        "key_name": "shift_r",
        "key_char": None,
        "key_vk": "161",
        "canonical_key_name": "shift",
        "canonical_key_char": None,
        "canonical_key_vk": "16",
    }


def test_callbacks_emit_normalized_events_filter_injected_and_chain() -> None:
    events = []
    user32 = FakeUser32()
    observer = make_observer(events.append, user32=user32)
    observer.start()

    key_payload = KBDLLHOOKSTRUCT(vkCode=0x41, scanCode=30)
    assert (
        observer._keyboard_hook_callback(
            HC_ACTION,
            WM_KEYDOWN,
            ctypes.addressof(key_payload),
        )
        == 73
    )
    key_payload.flags = LLKHF_INJECTED
    observer._keyboard_hook_callback(
        HC_ACTION,
        WM_KEYUP,
        ctypes.addressof(key_payload),
    )

    mouse_payload = MSLLHOOKSTRUCT(pt=POINT(5, 7))
    observer._mouse_hook_callback(
        HC_ACTION,
        WM_LBUTTONDOWN,
        ctypes.addressof(mouse_payload),
    )
    mouse_payload.flags = LLMHF_INJECTED
    observer._mouse_hook_callback(
        HC_ACTION,
        WM_MOUSEMOVE,
        ctypes.addressof(mouse_payload),
    )

    assert wait_until(lambda: len(events) == 2)
    assert events == [
        ObservedKey(
            pressed=True,
            key_char="a",
            key_vk="65",
            canonical_key_char="a",
            canonical_key_vk="65",
            timestamp=1234.5,
        ),
        ObservedMouseButton(
            x=5.0,
            y=7.0,
            button="left",
            pressed=True,
            timestamp=1234.5,
        ),
    ]
    assert len(user32.next_calls) == 4
    observer.stop()


def test_injected_transition_invalidates_an_in_flight_frame() -> None:
    observer = make_observer(lambda _event: None)
    observer.start()
    cut = observer.begin_frame_capture()
    payload = KBDLLHOOKSTRUCT(
        vkCode=0x41,
        scanCode=30,
        flags=LLKHF_INJECTED,
    )

    observer._keyboard_hook_callback(
        HC_ACTION,
        WM_KEYDOWN,
        ctypes.addressof(payload),
    )

    assert not observer.finish_frame_capture(cut)
    observer.complete_frame_capture(cut)
    observer.stop()


def test_native_callback_active_before_frame_invalidates_the_cut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer = make_observer(lambda _event: None)
    callback_reserved = threading.Event()
    release_callback = threading.Event()
    original_enqueue = observer._enqueue_input

    def hold_after_reservation(transition) -> None:
        callback_reserved.set()
        assert release_callback.wait(timeout=2)
        original_enqueue(transition)

    monkeypatch.setattr(observer, "_enqueue_input", hold_after_reservation)
    observer.start()
    payload = KBDLLHOOKSTRUCT(vkCode=0x41, scanCode=30)
    callback_thread = threading.Thread(
        target=observer._keyboard_hook_callback,
        args=(HC_ACTION, WM_KEYDOWN, ctypes.addressof(payload)),
    )
    callback_thread.start()
    assert callback_reserved.wait(timeout=2)

    cut = observer.begin_frame_capture()
    release_callback.set()
    callback_thread.join(timeout=2)
    assert not callback_thread.is_alive()
    assert not observer.finish_frame_capture(cut)
    observer.complete_frame_capture(cut)
    observer.stop()


def test_partial_setup_permission_failure_rolls_back_installed_hook() -> None:
    user32 = FakeUser32(
        hook_results=[501, 0],
        last_error=ERROR_ACCESS_DENIED,
    )
    observer = make_observer(lambda _event: None, user32=user32)

    with pytest.raises(InputObserverPermissionError, match="mouse hook"):
        observer.start()

    assert user32.installed == [13]
    assert user32.unhooked == [501]
    assert observer._translation_thread is None


def test_threaded_lifecycle_wakes_and_unhooks_every_installed_hook() -> None:
    user32 = FakeUser32()
    observer = make_observer(lambda _event: None, user32=user32)

    observer.start()
    observer.stop()

    assert user32.installed == [13, 14]
    assert user32.posted.is_set()
    assert user32.unhooked == [101, 102]
    assert observer._delivery_thread is None
    assert observer._translation_thread is None


def test_slow_consumer_never_blocks_hook_callback_or_hook_chain() -> None:
    entered = threading.Event()
    release = threading.Event()
    delivered = []
    user32 = FakeUser32()

    def consume(event) -> None:
        entered.set()
        release.wait(timeout=2)
        delivered.append(event)

    observer = make_observer(consume, user32=user32)
    observer.start()
    payload = MSLLHOOKSTRUCT(pt=POINT(9, 11))

    callback_result = []
    callback_returned = threading.Event()

    def invoke_hook() -> None:
        callback_result.append(
            observer._mouse_hook_callback(
                HC_ACTION,
                WM_LBUTTONDOWN,
                ctypes.addressof(payload),
            )
        )
        callback_returned.set()

    caller = threading.Thread(target=invoke_hook)
    caller.start()
    assert callback_returned.wait(timeout=0.5)
    caller.join(timeout=0.5)
    assert not caller.is_alive()
    assert callback_result == [73]
    assert entered.wait(timeout=1)
    assert not release.is_set()
    assert user32.next_calls[-1][1:] == (
        HC_ACTION,
        WM_LBUTTONDOWN,
        ctypes.addressof(payload),
    )

    release.set()
    observer.stop()
    assert delivered == [
        ObservedMouseButton(
            x=9.0,
            y=11.0,
            button="left",
            pressed=True,
            timestamp=1234.5,
        )
    ]


def test_keyboard_translation_uses_layout_snapshotted_at_hook_receipt() -> None:
    translation_entered = threading.Event()
    translation_release = threading.Event()
    events = []
    user32 = FakeUser32(
        foreground_thread_id=8765,
        keyboard_layout=44,
        translation_entered=translation_entered,
        translation_release=translation_release,
    )
    observer = make_observer(events.append, user32=user32)
    observer.start()

    first = KBDLLHOOKSTRUCT(vkCode=0x41, scanCode=0x1E)
    second = KBDLLHOOKSTRUCT(vkCode=0x42, scanCode=0x30)
    assert (
        observer._keyboard_hook_callback(
            HC_ACTION,
            WM_KEYDOWN,
            ctypes.addressof(first),
        )
        == 73
    )
    assert translation_entered.wait(timeout=1)
    assert (
        observer._keyboard_hook_callback(
            HC_ACTION,
            WM_KEYDOWN,
            ctypes.addressof(second),
        )
        == 73
    )

    user32.foreground_thread_id = 9876
    user32.keyboard_layout = 55
    translation_release.set()
    assert wait_until(lambda: len(events) == 2)

    third = KBDLLHOOKSTRUCT(vkCode=0x43, scanCode=0x2E)
    assert (
        observer._keyboard_hook_callback(
            HC_ACTION,
            WM_KEYDOWN,
            ctypes.addressof(third),
        )
        == 73
    )
    assert wait_until(lambda: len(events) == 3)
    assert events == [
        ObservedKey(
            pressed=True,
            key_char="a",
            key_vk="65",
            canonical_key_char="a",
            canonical_key_vk="65",
            timestamp=1234.5,
        ),
        ObservedKey(
            pressed=True,
            key_char="b",
            key_vk="66",
            canonical_key_char="b",
            canonical_key_vk="66",
            timestamp=1234.5,
        ),
        ObservedKey(
            pressed=True,
            key_char="c",
            key_vk="67",
            canonical_key_char="c",
            canonical_key_vk="67",
            timestamp=1234.5,
        ),
    ]
    assert user32.layout_thread_ids == [8765, 8765, 9876]
    assert user32.translation_layouts == [44, 44, 55]
    observer.stop()


def test_keyboard_state_tracks_toggle_without_repeat_retoggling() -> None:
    events = []
    user32 = FakeUser32()
    observer = make_observer(events.append, user32=user32)
    observer.start()

    caps_lock = KBDLLHOOKSTRUCT(vkCode=VK_CAPITAL, scanCode=0x3A)
    letter = KBDLLHOOKSTRUCT(vkCode=0x41, scanCode=0x1E)
    observer._keyboard_hook_callback(
        HC_ACTION,
        WM_KEYDOWN,
        ctypes.addressof(caps_lock),
    )
    observer._keyboard_hook_callback(
        HC_ACTION,
        WM_KEYDOWN,
        ctypes.addressof(caps_lock),
    )
    observer._keyboard_hook_callback(
        HC_ACTION,
        WM_KEYDOWN,
        ctypes.addressof(letter),
    )

    assert wait_until(lambda: len(events) == 3)
    assert events[-1] == ObservedKey(
        pressed=True,
        key_char="A",
        key_vk="65",
        canonical_key_char="a",
        canonical_key_vk="65",
        timestamp=1234.5,
    )
    assert observer._keyboard_state[VK_CAPITAL] & 0x01
    observer.stop()


def test_dead_key_preserves_physical_identity_without_claiming_text() -> None:
    events = []
    user32 = FakeUser32(translation_result=-1)
    observer = make_observer(events.append, user32=user32)
    observer.start()
    dead_key = KBDLLHOOKSTRUCT(vkCode=0xDE, scanCode=0x28)

    observer._keyboard_hook_callback(
        HC_ACTION,
        WM_KEYDOWN,
        ctypes.addressof(dead_key),
    )

    assert wait_until(lambda: len(events) == 1)
    assert events == [
        ObservedKey(
            pressed=True,
            key_vk="222",
            canonical_key_vk="222",
            timestamp=1234.5,
        )
    ]
    observer.stop()


def test_dead_key_resolution_is_unverifiable_then_normal_text_recovers() -> None:
    events = []
    user32 = FakeUser32(
        translation_script=[
            (-1, "´"),
            (1, "e"),
            (1, "x"),
        ]
    )
    observer = make_observer(events.append, user32=user32)
    observer.start()
    dead_key = KBDLLHOOKSTRUCT(vkCode=0xDE, scanCode=0x28)
    resolving_key = KBDLLHOOKSTRUCT(vkCode=0x45, scanCode=0x12)
    normal_key = KBDLLHOOKSTRUCT(vkCode=0x58, scanCode=0x2D)

    observer._keyboard_hook_callback(
        HC_ACTION,
        WM_KEYDOWN,
        ctypes.addressof(dead_key),
    )
    observer._keyboard_hook_callback(
        HC_ACTION,
        WM_KEYUP,
        ctypes.addressof(dead_key),
    )
    observer._keyboard_hook_callback(
        HC_ACTION,
        WM_KEYDOWN,
        ctypes.addressof(resolving_key),
    )
    observer._keyboard_hook_callback(
        HC_ACTION,
        WM_KEYDOWN,
        ctypes.addressof(resolving_key),
    )
    observer._keyboard_hook_callback(
        HC_ACTION,
        WM_KEYUP,
        ctypes.addressof(resolving_key),
    )
    observer._keyboard_hook_callback(
        HC_ACTION,
        WM_KEYDOWN,
        ctypes.addressof(normal_key),
    )

    assert wait_until(lambda: len(events) == 6)
    assert events == [
        ObservedKey(
            pressed=True,
            key_vk="222",
            canonical_key_vk="222",
            timestamp=1234.5,
        ),
        ObservedKey(
            pressed=False,
            key_vk="222",
            canonical_key_vk="222",
            timestamp=1234.5,
        ),
        ObservedKey(
            pressed=True,
            key_vk="69",
            canonical_key_vk="69",
            timestamp=1234.5,
        ),
        ObservedKey(
            pressed=True,
            key_vk="69",
            canonical_key_vk="69",
            timestamp=1234.5,
        ),
        ObservedKey(
            pressed=False,
            key_vk="69",
            canonical_key_vk="69",
            timestamp=1234.5,
        ),
        ObservedKey(
            pressed=True,
            key_char="x",
            key_vk="88",
            canonical_key_char="x",
            canonical_key_vk="88",
            timestamp=1234.5,
        ),
    ]
    assert user32.translation_script == []
    observer.stop()


def test_blocked_keyboard_translation_does_not_block_hook_or_reorder_mouse() -> None:
    translation_entered = threading.Event()
    translation_release = threading.Event()
    events = []
    user32 = FakeUser32(
        translation_entered=translation_entered,
        translation_release=translation_release,
    )
    clock_value = [111.25]
    observer = make_observer(
        events.append,
        user32=user32,
        clock=lambda: clock_value[0],
    )
    observer.start()
    key = KBDLLHOOKSTRUCT(vkCode=0x41, scanCode=0x1E)
    mouse = MSLLHOOKSTRUCT(pt=POINT(21, 22))

    callback_result = []
    callback_returned = threading.Event()

    def invoke_key_hook() -> None:
        callback_result.append(
            observer._keyboard_hook_callback(
                HC_ACTION,
                WM_KEYDOWN,
                ctypes.addressof(key),
            )
        )
        callback_returned.set()

    caller = threading.Thread(target=invoke_key_hook)
    caller.start()
    assert callback_returned.wait(timeout=0.5)
    caller.join(timeout=0.5)
    assert not caller.is_alive()
    assert callback_result == [73]
    assert translation_entered.wait(timeout=1)
    assert not translation_release.is_set()

    clock_value[0] = 222.5
    assert (
        observer._mouse_hook_callback(
            HC_ACTION,
            WM_LBUTTONDOWN,
            ctypes.addressof(mouse),
        )
        == 73
    )
    translation_release.set()

    assert wait_until(lambda: len(events) == 2)
    assert events == [
        ObservedKey(
            pressed=True,
            key_char="a",
            key_vk="65",
            canonical_key_char="a",
            canonical_key_vk="65",
            timestamp=111.25,
        ),
        ObservedMouseButton(
            x=21.0,
            y=22.0,
            button="left",
            pressed=True,
            timestamp=222.5,
        ),
    ]
    observer.stop()


def test_keyboard_hook_reserves_receipt_before_translation_queue() -> None:
    translation_entered = threading.Event()
    translation_release = threading.Event()
    user32 = FakeUser32(
        translation_entered=translation_entered,
        translation_release=translation_release,
    )
    reserved_timestamps = []
    events = []

    class Receipt:
        finished = False

        def fail(self, _error) -> None:
            self.finished = True

    def consume(_event) -> None:
        raise AssertionError("reserved input must use the receipt consumer")

    def reserve(timestamp):
        reserved_timestamps.append(timestamp)
        return Receipt()

    def deliver(event, receipt) -> None:
        events.append(event)
        receipt.finished = True

    setattr(consume, "_openadapt_input_receipt", reserve)
    setattr(consume, "_openadapt_input_delivery", deliver)
    observer = make_observer(
        consume,
        user32=user32,
        clock=lambda: 111.25,
    )
    observer.start()
    key = KBDLLHOOKSTRUCT(vkCode=0x41, scanCode=0x1E)

    assert (
        observer._keyboard_hook_callback(
            HC_ACTION,
            WM_KEYDOWN,
            ctypes.addressof(key),
        )
        == 73
    )
    assert translation_entered.wait(timeout=1)
    assert reserved_timestamps == [111.25]
    assert events == []

    translation_release.set()
    assert wait_until(lambda: len(events) == 1)
    observer.stop()


def test_translation_queue_overflow_fails_loud_and_unhooks() -> None:
    translation_entered = threading.Event()
    translation_release = threading.Event()
    user32 = FakeUser32(
        translation_entered=translation_entered,
        translation_release=translation_release,
    )
    observer = make_observer(
        lambda _event: None,
        user32=user32,
        translation_queue_size=1,
    )
    observer.start()

    first = KBDLLHOOKSTRUCT(vkCode=0x41, scanCode=0x1E)
    second = KBDLLHOOKSTRUCT(vkCode=0x42, scanCode=0x30)
    third = KBDLLHOOKSTRUCT(vkCode=0x43, scanCode=0x2E)
    observer._keyboard_hook_callback(
        HC_ACTION,
        WM_KEYDOWN,
        ctypes.addressof(first),
    )
    assert translation_entered.wait(timeout=1)
    observer._keyboard_hook_callback(
        HC_ACTION,
        WM_KEYDOWN,
        ctypes.addressof(second),
    )
    assert (
        observer._keyboard_hook_callback(
            HC_ACTION,
            WM_KEYDOWN,
            ctypes.addressof(third),
        )
        == 73
    )

    translation_release.set()
    with pytest.raises(InputObserverError, match="translation queue overflowed"):
        observer.stop()
    assert user32.unhooked == [101, 102]
    assert observer._translation_thread is None


def test_translation_failure_propagates_and_cleans_up() -> None:
    user32 = FakeUser32(translation_error=RuntimeError("layout failed"))
    observer = make_observer(lambda _event: None, user32=user32)
    observer.start()
    key = KBDLLHOOKSTRUCT(vkCode=0x41, scanCode=0x1E)

    assert (
        observer._keyboard_hook_callback(
            HC_ACTION,
            WM_KEYDOWN,
            ctypes.addressof(key),
        )
        == 73
    )

    assert wait_until(lambda: observer._failure is not None)
    with pytest.raises(InputObserverError, match="input translation failed"):
        observer.stop()
    assert user32.unhooked == [101, 102]
    assert observer._translation_thread is None


def test_delivery_overflow_fails_loud_wakes_and_cleans_up() -> None:
    entered = threading.Event()
    release = threading.Event()
    delivery_count = 0
    user32 = FakeUser32()

    def consume(_event) -> None:
        nonlocal delivery_count
        delivery_count += 1
        if delivery_count == 1:
            entered.set()
            release.wait(timeout=2)

    observer = make_observer(
        consume,
        user32=user32,
        delivery_queue_size=1,
        translation_queue_size=4096,
    )
    observer.start()
    payload = MSLLHOOKSTRUCT(pt=POINT(3, 4))

    observer._mouse_hook_callback(
        HC_ACTION,
        WM_LBUTTONDOWN,
        ctypes.addressof(payload),
    )
    assert entered.wait(timeout=1)
    observer._mouse_hook_callback(
        HC_ACTION,
        WM_LBUTTONDOWN,
        ctypes.addressof(payload),
    )
    result = observer._mouse_hook_callback(
        HC_ACTION,
        WM_LBUTTONDOWN,
        ctypes.addressof(payload),
    )

    assert result == 73
    assert wait_until(user32.posted.is_set)
    release.set()
    with pytest.raises(InputObserverError, match="queue overflowed"):
        observer.stop()
    assert user32.unhooked == [101, 102]
    assert observer._delivery_thread is None


def test_teardown_attempts_every_hook_and_surfaces_release_failure() -> None:
    user32 = FakeUser32(unhook_failures={101})
    observer = make_observer(lambda _event: None, user32=user32)
    observer._keyboard_hook = 101
    observer._mouse_hook = 102

    with pytest.raises(InputObserverError, match="keyboard hook"):
        observer._teardown()

    assert user32.unhooked == [101, 102]
    assert observer._keyboard_hook is None
    assert observer._mouse_hook is None


def test_consumer_failure_is_surfaced_after_clean_teardown() -> None:
    user32 = FakeUser32()

    def fail(_event) -> None:
        raise ValueError("consumer failed")

    observer = make_observer(fail, user32=user32)
    observer.start()
    payload = MSLLHOOKSTRUCT(pt=POINT(5, 7))

    observer._mouse_hook_callback(
        HC_ACTION,
        WM_LBUTTONDOWN,
        ctypes.addressof(payload),
    )

    assert wait_until(lambda: observer._failure is not None)
    assert user32.posted.is_set()
    with pytest.raises(InputObserverError, match="input consumer failed"):
        observer.stop()
    assert user32.unhooked == [101, 102]
    assert observer._delivery_thread is None

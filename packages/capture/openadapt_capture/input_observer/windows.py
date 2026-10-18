"""Native Windows global input observation using permissive stdlib ``ctypes``.

The implementation uses the documented ``WH_KEYBOARD_LL`` and ``WH_MOUSE_LL``
hooks.  It intentionally exposes observation only: input injection remains a
separate concern, and events marked as injected by Windows are discarded.
"""

from __future__ import annotations

import ctypes
import queue
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .base import (
    InputCallback,
    InputObserverError,
    InputObserverPermissionError,
    InputObserverUnavailableError,
    ObservedInput,
    ObservedKey,
    ObservedMouseButton,
    ObservedMouseMove,
    ObservedMouseScroll,
    ThreadedInputObserver,
)

# Hook and message constants from winuser.h.
WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
HC_ACTION = 0

WM_QUIT = 0x0012
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A
WM_XBUTTONDOWN = 0x020B
WM_XBUTTONUP = 0x020C
WM_MOUSEHWHEEL = 0x020E
WM_USER = 0x0400

PM_NOREMOVE = 0x0000
WHEEL_DELTA = 120
XBUTTON1 = 0x0001
XBUTTON2 = 0x0002
TO_UNICODE_NO_STATE_CHANGE = 0x0004

LLKHF_EXTENDED = 0x00000001
LLKHF_LOWER_IL_INJECTED = 0x00000002
LLKHF_INJECTED = 0x00000010
LLMHF_INJECTED = 0x00000001
LLMHF_LOWER_IL_INJECTED = 0x00000002

ERROR_ACCESS_DENIED = 5
ERROR_PRIVILEGE_NOT_HELD = 1314

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_CAPITAL = 0x14
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_LMENU = 0xA4
VK_RMENU = 0xA5
VK_NUMLOCK = 0x90
VK_SCROLL = 0x91

# Fixed-width aliases keep the structures testable off Windows as well as
# matching the Windows ABI on both 32- and 64-bit Python.
DWORD = ctypes.c_uint32
LONG = ctypes.c_int32
ULONG_PTR = ctypes.c_size_t
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
LRESULT = ctypes.c_ssize_t
HANDLE = ctypes.c_void_p
HWND = ctypes.c_void_p
HHOOK = ctypes.c_void_p
HINSTANCE = ctypes.c_void_p
SHORT = ctypes.c_short

WINFUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
HOOKPROC = WINFUNCTYPE(LRESULT, ctypes.c_int, WPARAM, LPARAM)


class POINT(ctypes.Structure):
    """Windows screen coordinate."""

    _fields_ = [("x", LONG), ("y", LONG)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    """Payload delivered to a low-level keyboard hook."""

    _fields_ = [
        ("vkCode", DWORD),
        ("scanCode", DWORD),
        ("flags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MSLLHOOKSTRUCT(ctypes.Structure):
    """Payload delivered to a low-level mouse hook."""

    _fields_ = [
        ("pt", POINT),
        ("mouseData", DWORD),
        ("flags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MSG(ctypes.Structure):
    """Message-loop payload."""

    _fields_ = [
        ("hwnd", HWND),
        ("message", ctypes.c_uint32),
        ("wParam", WPARAM),
        ("lParam", LPARAM),
        ("time", DWORD),
        ("pt", POINT),
        ("lPrivate", DWORD),
    ]


@dataclass(frozen=True, slots=True)
class _RawKeyboardTransition:
    """Scalar copy of a keyboard hook payload safe to process off-hook."""

    pressed: bool
    vk_code: int
    scan_code: int
    flags: int
    keyboard_layout: int
    timestamp: float
    receipt: object | None = None


@dataclass(frozen=True, slots=True)
class _RawMouseTransition:
    """Scalar copy of a mouse hook payload safe to process off-hook."""

    message: int
    x: int
    y: int
    mouse_data: int
    flags: int
    timestamp: float
    receipt: object | None = None


_RawInputTransition = _RawKeyboardTransition | _RawMouseTransition


_PHYSICAL_KEY_NAMES = {
    0x08: "backspace",
    0x09: "tab",
    0x0D: "enter",
    0x10: "shift",
    0x11: "ctrl",
    0x12: "alt",
    0x13: "pause",
    0x14: "caps_lock",
    0x1B: "esc",
    0x20: "space",
    0x21: "page_up",
    0x22: "page_down",
    0x23: "end",
    0x24: "home",
    0x25: "left",
    0x26: "up",
    0x27: "right",
    0x28: "down",
    0x2C: "print_screen",
    0x2D: "insert",
    0x2E: "delete",
    0x5B: "cmd_l",
    0x5C: "cmd_r",
    0x5D: "menu",
    0x70: "f1",
    0x71: "f2",
    0x72: "f3",
    0x73: "f4",
    0x74: "f5",
    0x75: "f6",
    0x76: "f7",
    0x77: "f8",
    0x78: "f9",
    0x79: "f10",
    0x7A: "f11",
    0x7B: "f12",
    0x7C: "f13",
    0x7D: "f14",
    0x7E: "f15",
    0x7F: "f16",
    0x80: "f17",
    0x81: "f18",
    0x82: "f19",
    0x83: "f20",
    0x84: "f21",
    0x85: "f22",
    0x86: "f23",
    0x87: "f24",
    0x90: "num_lock",
    0x91: "scroll_lock",
    0xA0: "shift_l",
    0xA1: "shift_r",
    0xA2: "ctrl_l",
    0xA3: "ctrl_r",
    0xA4: "alt_l",
    0xA5: "alt_r",
}

_CANONICAL_MODIFIERS = {
    0xA0: ("shift", 0x10),
    0xA1: ("shift", 0x10),
    0xA2: ("ctrl", 0x11),
    0xA3: ("ctrl", 0x11),
    0xA4: ("alt", 0x12),
    0xA5: ("alt", 0x12),
    0x5B: ("cmd", 0x5B),
    0x5C: ("cmd", 0x5B),
}

_BUTTON_MESSAGES = {
    WM_LBUTTONDOWN: ("left", True),
    WM_LBUTTONUP: ("left", False),
    WM_RBUTTONDOWN: ("right", True),
    WM_RBUTTONUP: ("right", False),
    WM_MBUTTONDOWN: ("middle", True),
    WM_MBUTTONUP: ("middle", False),
}


def _set_signature(function: Any, argtypes: list[Any], restype: Any) -> None:
    """Set a ctypes signature while allowing plain Python fakes in tests."""

    try:
        function.argtypes = argtypes
        function.restype = restype
    except (AttributeError, TypeError):
        return


def _handle_value(handle: object) -> int:
    """Return a stable integer value for a ctypes or fake Windows handle."""

    if handle is None:
        return 0
    if isinstance(handle, int):
        return handle
    value = getattr(handle, "value", None)
    return int(value or 0)


def _signed_high_word(value: int) -> int:
    """Extract a signed 16-bit wheel delta from ``mouseData``."""

    high_word = (int(value) >> 16) & 0xFFFF
    return high_word - 0x10000 if high_word & 0x8000 else high_word


def _key_identity(vk_code: int, character: str | None) -> dict[str, str | None]:
    """Normalize physical and canonical key identity for the public event model."""

    key_name = _PHYSICAL_KEY_NAMES.get(vk_code)
    canonical_name = key_name
    canonical_vk = vk_code
    canonical_character = character.lower() if character else None

    modifier = _CANONICAL_MODIFIERS.get(vk_code)
    if modifier is not None:
        canonical_name, canonical_vk = modifier
        character = None
        canonical_character = None
    elif key_name is not None:
        character = None
        canonical_character = None

    return {
        "key_name": key_name,
        "key_char": character,
        "key_vk": str(vk_code),
        "canonical_key_name": canonical_name,
        "canonical_key_char": canonical_character,
        "canonical_key_vk": str(canonical_vk),
    }


def _mouse_event(
    message: int,
    payload: MSLLHOOKSTRUCT,
    *,
    capture_mouse_moves: bool,
    timestamp: float | None = None,
) -> ObservedInput | None:
    """Translate a Windows low-level mouse payload to the common contract."""

    injected = bool(payload.flags & (LLMHF_INJECTED | LLMHF_LOWER_IL_INJECTED))
    if injected:
        return None

    x = float(payload.pt.x)
    y = float(payload.pt.y)
    if message == WM_MOUSEMOVE:
        if not capture_mouse_moves:
            return None
        return ObservedMouseMove(x=x, y=y, timestamp=timestamp)

    button_transition = _BUTTON_MESSAGES.get(message)
    if button_transition is not None:
        button, pressed = button_transition
        return ObservedMouseButton(
            x=x,
            y=y,
            button=button,
            pressed=pressed,
            timestamp=timestamp,
        )

    if message in {WM_XBUTTONDOWN, WM_XBUTTONUP}:
        xbutton = (int(payload.mouseData) >> 16) & 0xFFFF
        button = "x1" if xbutton == XBUTTON1 else "x2" if xbutton == XBUTTON2 else None
        if button is None:
            return None
        return ObservedMouseButton(
            x=x,
            y=y,
            button=button,
            pressed=message == WM_XBUTTONDOWN,
            timestamp=timestamp,
        )

    if message == WM_MOUSEWHEEL:
        return ObservedMouseScroll(
            x=x,
            y=y,
            dx=0.0,
            dy=_signed_high_word(payload.mouseData) / WHEEL_DELTA,
            timestamp=timestamp,
        )
    if message == WM_MOUSEHWHEEL:
        return ObservedMouseScroll(
            x=x,
            y=y,
            dx=_signed_high_word(payload.mouseData) / WHEEL_DELTA,
            dy=0.0,
            timestamp=timestamp,
        )
    return None


class WindowsInputObserver(ThreadedInputObserver):
    """Observe global Windows keyboard and mouse input with low-level hooks."""

    def __init__(
        self,
        callback: InputCallback,
        *,
        observe_keyboard: bool,
        observe_mouse: bool,
        capture_mouse_moves: bool,
        startup_timeout: float = 5.0,
        # A clean Windows install can need more than five seconds for the first
        # pywinauto/comtypes UIA initialization on the delivery thread. Keep
        # native hook setup at five seconds and give only UIA the larger bound.
        delivery_startup_timeout: float = 20.0,
        shutdown_timeout: float = 5.0,
        delivery_queue_size: int = 4096,
        translation_queue_size: int | None = None,
        _user32: Any | None = None,
        _kernel32: Any | None = None,
        _clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(
            callback,
            observe_keyboard=observe_keyboard,
            observe_mouse=observe_mouse,
            capture_mouse_moves=capture_mouse_moves,
            startup_timeout=startup_timeout,
            delivery_startup_timeout=delivery_startup_timeout,
            shutdown_timeout=shutdown_timeout,
            delivery_queue_size=delivery_queue_size,
        )
        self._user32 = _user32
        self._kernel32 = _kernel32
        self._clock = _clock
        self._keyboard_hook: object | None = None
        self._mouse_hook: object | None = None
        self._keyboard_proc: Any | None = None
        self._mouse_proc: Any | None = None
        self._thread_id: int | None = None
        self.translation_queue_size = (
            delivery_queue_size if translation_queue_size is None else translation_queue_size
        )
        if self.translation_queue_size <= 0:
            raise ValueError("translation_queue_size must be positive")
        self._translation_queue: queue.Queue[_RawInputTransition | object] = queue.Queue(
            maxsize=self.translation_queue_size
        )
        self._translation_sentinel = object()
        self._translation_stop_requested = threading.Event()
        self._translation_thread: threading.Thread | None = None
        self._keyboard_state = (ctypes.c_ubyte * 256)()
        self._keys_down: set[int] = set()
        self._dead_key_pending = False
        self._unverifiable_text_keys: set[int] = set()

    def _load_apis(self) -> None:
        if self._user32 is not None and self._kernel32 is not None:
            return
        if sys.platform != "win32":
            raise InputObserverUnavailableError(
                "Windows input observation requires a native Windows session"
            )
        try:
            self._user32 = ctypes.WinDLL("user32", use_last_error=True)
            self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        except (AttributeError, OSError) as exc:
            raise InputObserverUnavailableError(
                f"Windows input APIs are unavailable: {exc}"
            ) from exc

    def _configure_apis(self) -> None:
        user32 = self._user32
        kernel32 = self._kernel32
        assert user32 is not None
        assert kernel32 is not None
        _set_signature(
            user32.SetWindowsHookExW,
            [ctypes.c_int, HOOKPROC, HINSTANCE, DWORD],
            HHOOK,
        )
        _set_signature(user32.UnhookWindowsHookEx, [HHOOK], ctypes.c_int)
        _set_signature(
            user32.CallNextHookEx,
            [HHOOK, ctypes.c_int, WPARAM, LPARAM],
            LRESULT,
        )
        _set_signature(
            user32.GetMessageW,
            [ctypes.POINTER(MSG), HWND, ctypes.c_uint32, ctypes.c_uint32],
            ctypes.c_int,
        )
        _set_signature(user32.TranslateMessage, [ctypes.POINTER(MSG)], ctypes.c_int)
        _set_signature(user32.DispatchMessageW, [ctypes.POINTER(MSG)], LRESULT)
        _set_signature(
            user32.PeekMessageW,
            [
                ctypes.POINTER(MSG),
                HWND,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_uint32,
            ],
            ctypes.c_int,
        )
        _set_signature(
            user32.PostThreadMessageW,
            [DWORD, ctypes.c_uint32, WPARAM, LPARAM],
            ctypes.c_int,
        )
        _set_signature(user32.GetAsyncKeyState, [ctypes.c_int], SHORT)
        _set_signature(user32.GetKeyState, [ctypes.c_int], SHORT)
        _set_signature(user32.GetForegroundWindow, [], HWND)
        _set_signature(
            user32.GetWindowThreadProcessId,
            [HWND, ctypes.POINTER(DWORD)],
            DWORD,
        )
        _set_signature(
            user32.ToUnicodeEx,
            [
                ctypes.c_uint,
                ctypes.c_uint,
                ctypes.POINTER(ctypes.c_ubyte),
                ctypes.POINTER(ctypes.c_wchar),
                ctypes.c_int,
                ctypes.c_uint,
                HANDLE,
            ],
            ctypes.c_int,
        )
        _set_signature(user32.GetKeyboardLayout, [DWORD], HANDLE)
        _set_signature(kernel32.GetModuleHandleW, [ctypes.c_wchar_p], HINSTANCE)
        _set_signature(kernel32.GetCurrentThreadId, [], DWORD)

    def _last_error(self) -> int:
        for api in (self._user32, self._kernel32):
            value = getattr(api, "last_error", None)
            if value is not None:
                return int(value)
        get_last_error = getattr(ctypes, "get_last_error", None)
        return int(get_last_error()) if get_last_error is not None else 0

    def _hook_error(self, hook_name: str) -> InputObserverError:
        error_code = self._last_error()
        detail = f"Windows error {error_code}" if error_code else "unknown Windows error"
        message = f"could not install the global {hook_name} hook ({detail})"
        if error_code in {ERROR_ACCESS_DENIED, ERROR_PRIVILEGE_NOT_HELD}:
            return InputObserverPermissionError(message)
        return InputObserverUnavailableError(message)

    def _setup(self) -> None:
        self._load_apis()
        self._configure_apis()
        user32 = self._user32
        kernel32 = self._kernel32
        assert user32 is not None
        assert kernel32 is not None

        self._thread_id = int(kernel32.GetCurrentThreadId())
        # Force creation of the thread message queue before another thread can
        # request shutdown with PostThreadMessageW.
        message = MSG()
        user32.PeekMessageW(
            ctypes.byref(message),
            None,
            WM_USER,
            WM_USER,
            PM_NOREMOVE,
        )

        if self.observe_keyboard:
            self._initialize_keyboard_state()
        self._start_input_translation()

        self._keyboard_proc = HOOKPROC(self._keyboard_hook_callback)
        self._mouse_proc = HOOKPROC(self._mouse_hook_callback)
        module = kernel32.GetModuleHandleW(None)

        if self.observe_keyboard:
            hook = user32.SetWindowsHookExW(
                WH_KEYBOARD_LL,
                self._keyboard_proc,
                module,
                0,
            )
            if not _handle_value(hook):
                raise self._hook_error("keyboard")
            self._keyboard_hook = hook

        if self.observe_mouse:
            hook = user32.SetWindowsHookExW(
                WH_MOUSE_LL,
                self._mouse_proc,
                module,
                0,
            )
            if not _handle_value(hook):
                raise self._hook_error("mouse")
            self._mouse_hook = hook

    def _run_loop(self) -> None:
        user32 = self._user32
        assert user32 is not None
        message = MSG()
        while not self._stop_requested.is_set():
            result = int(user32.GetMessageW(ctypes.byref(message), None, 0, 0))
            if result == 0:
                return
            if result == -1:
                error_code = self._last_error()
                raise InputObserverError(
                    "Windows input-observer message loop failed " f"(Windows error {error_code})"
                )
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))

    def _wake(self) -> None:
        user32 = self._user32
        thread_id = self._thread_id
        if user32 is None or thread_id is None:
            return
        user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)

    def _teardown(self) -> None:
        user32 = self._user32
        failures: list[str] = []
        if user32 is not None:
            for name, attribute in (
                ("keyboard", "_keyboard_hook"),
                ("mouse", "_mouse_hook"),
            ):
                hook = getattr(self, attribute)
                if hook is None:
                    continue
                try:
                    released = user32.UnhookWindowsHookEx(hook)
                except BaseException as exc:
                    failures.append(f"{name} hook ({exc})")
                else:
                    if not released:
                        failures.append(f"{name} hook (Windows error {self._last_error()})")
                finally:
                    setattr(self, attribute, None)
        self._stop_input_translation(failures)
        self._keyboard_proc = None
        self._mouse_proc = None
        self._thread_id = None
        if failures:
            raise InputObserverError(
                "could not release Windows input observer " + ", ".join(failures)
            )

    def _record_callback_failure(self, exc: BaseException) -> None:
        self._fail(exc)

    def _call_next(self, hook: object | None, code: int, wparam: int, lparam: int) -> int:
        user32 = self._user32
        assert user32 is not None
        return int(user32.CallNextHookEx(hook, code, wparam, lparam))

    @staticmethod
    def _physical_vk(transition: _RawKeyboardTransition) -> int:
        """Resolve generic modifier virtual keys to their physical side."""

        if transition.vk_code == VK_SHIFT:
            return VK_RSHIFT if transition.scan_code == 0x36 else VK_LSHIFT
        if transition.vk_code == VK_CONTROL:
            return VK_RCONTROL if transition.flags & LLKHF_EXTENDED else VK_LCONTROL
        if transition.vk_code == VK_MENU:
            return VK_RMENU if transition.flags & LLKHF_EXTENDED else VK_LMENU
        return transition.vk_code

    def _sync_generic_modifier_state(self) -> None:
        groups = {
            VK_SHIFT: {VK_SHIFT, VK_LSHIFT, VK_RSHIFT},
            VK_CONTROL: {VK_CONTROL, VK_LCONTROL, VK_RCONTROL},
            VK_MENU: {VK_MENU, VK_LMENU, VK_RMENU},
        }
        for generic_vk, members in groups.items():
            pressed = any(member in self._keys_down for member in members)
            toggled = self._keyboard_state[generic_vk] & 0x01
            self._keyboard_state[generic_vk] = toggled | (0x80 if pressed else 0)

    def _initialize_keyboard_state(self) -> None:
        user32 = self._user32
        assert user32 is not None
        self._keyboard_state = (ctypes.c_ubyte * 256)()
        self._keys_down.clear()
        self._dead_key_pending = False
        self._unverifiable_text_keys.clear()
        for vk_code in {
            VK_LSHIFT,
            VK_RSHIFT,
            VK_LCONTROL,
            VK_RCONTROL,
            VK_LMENU,
            VK_RMENU,
        }:
            if int(user32.GetAsyncKeyState(vk_code)) & 0x8000:
                self._keys_down.add(vk_code)
                self._keyboard_state[vk_code] |= 0x80
        for vk_code in {VK_CAPITAL, VK_NUMLOCK, VK_SCROLL}:
            if int(user32.GetKeyState(vk_code)) & 0x0001:
                self._keyboard_state[vk_code] |= 0x01
        self._sync_generic_modifier_state()

    def _apply_keyboard_transition(self, transition: _RawKeyboardTransition) -> int:
        physical_vk = self._physical_vk(transition)
        was_pressed = physical_vk in self._keys_down
        if transition.pressed:
            self._keys_down.add(physical_vk)
            self._keyboard_state[physical_vk] |= 0x80
        else:
            self._keys_down.discard(physical_vk)
            self._keyboard_state[physical_vk] &= 0x7F

        if (
            transition.pressed
            and not was_pressed
            and physical_vk in {VK_CAPITAL, VK_NUMLOCK, VK_SCROLL}
        ):
            self._keyboard_state[physical_vk] ^= 0x01
        self._sync_generic_modifier_state()
        return physical_vk

    def _foreground_keyboard_layout(self) -> int:
        user32 = self._user32
        assert user32 is not None
        foreground = user32.GetForegroundWindow()
        if not _handle_value(foreground):
            raise InputObserverUnavailableError(
                "Windows could not identify the foreground window for keyboard layout"
            )
        thread_id = int(user32.GetWindowThreadProcessId(foreground, None))
        if not thread_id:
            raise InputObserverUnavailableError(
                "Windows could not identify the foreground input thread"
            )
        layout = user32.GetKeyboardLayout(thread_id)
        if not _handle_value(layout):
            raise InputObserverUnavailableError(
                "Windows could not resolve the foreground keyboard layout"
            )
        return _handle_value(layout)

    def _keyboard_character(
        self,
        transition: _RawKeyboardTransition,
    ) -> tuple[str | None, bool]:
        user32 = self._user32
        assert user32 is not None
        buffer = ctypes.create_unicode_buffer(8)
        count = int(
            user32.ToUnicodeEx(
                transition.vk_code,
                transition.scan_code,
                self._keyboard_state,
                buffer,
                len(buffer),
                TO_UNICODE_NO_STATE_CHANGE,
                transition.keyboard_layout,
            )
        )
        if count == 0:
            return None, False
        if count < 0:
            # A dead key is composition state, not committed text. Preserve
            # physical identity but do not claim that its spacing accent was
            # typed before a later key resolves the composition.
            return None, True
        character_count = count
        if ctypes.sizeof(ctypes.c_wchar) == 2:
            raw = ctypes.string_at(buffer, character_count * 2)
            return raw.decode("utf-16-le"), False
        return "".join(buffer[:character_count]), False

    def _translated_character(
        self,
        transition: _RawKeyboardTransition,
        physical_vk: int,
    ) -> str | None:
        """Return committed text without guessing across dead-key composition.

        ``TO_UNICODE_NO_STATE_CHANGE`` prevents the observer from mutating the
        application's keyboard state, but it also means Windows cannot give us
        a trustworthy committed character for the key that resolves a dead-key
        sequence.  Preserve both physical events and suppress their text until
        the resolving key is released; the following normal key then recovers.
        """

        if physical_vk in self._unverifiable_text_keys:
            if not transition.pressed:
                self._unverifiable_text_keys.discard(physical_vk)
            return None

        character, is_dead_key = self._keyboard_character(transition)
        if is_dead_key:
            self._dead_key_pending = True
            self._unverifiable_text_keys.add(physical_vk)
            return None

        if transition.pressed and self._dead_key_pending:
            self._dead_key_pending = False
            self._unverifiable_text_keys.add(physical_vk)
            return None

        return character

    def _translate_keyboard(
        self,
        transition: _RawKeyboardTransition,
    ) -> ObservedKey:
        physical_vk = self._apply_keyboard_transition(transition)
        character = (
            None
            if physical_vk in _PHYSICAL_KEY_NAMES
            else self._translated_character(transition, physical_vk)
        )
        return ObservedKey(
            pressed=transition.pressed,
            timestamp=transition.timestamp,
            **_key_identity(physical_vk, character),
        )

    def _translate_mouse(
        self,
        transition: _RawMouseTransition,
    ) -> ObservedInput | None:
        payload = MSLLHOOKSTRUCT(
            pt=POINT(transition.x, transition.y),
            mouseData=transition.mouse_data,
            flags=transition.flags,
        )
        return _mouse_event(
            transition.message,
            payload,
            capture_mouse_moves=self.capture_mouse_moves,
            timestamp=transition.timestamp,
        )

    def _input_translation_main(self) -> None:
        while True:
            if self._translation_stop_requested.is_set() and self._translation_queue.empty():
                return
            try:
                item = self._translation_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if item is self._translation_sentinel:
                    return
                assert isinstance(
                    item,
                    (_RawKeyboardTransition, _RawMouseTransition),
                )
                event = (
                    self._translate_keyboard(item)
                    if isinstance(item, _RawKeyboardTransition)
                    else self._translate_mouse(item)
                )
                if event is not None:
                    self._emit_received(event, item.receipt)
                elif item.receipt is not None:
                    raise InputObserverError(
                        "Windows reserved an input receipt that did not normalize "
                        "to a recordable event"
                    )
            except BaseException as exc:
                failure = (
                    exc
                    if isinstance(exc, InputObserverError)
                    else InputObserverError(f"Windows input translation failed: {exc}")
                )
                if isinstance(
                    item,
                    (_RawKeyboardTransition, _RawMouseTransition),
                ):
                    self._fail_receipt(item.receipt, failure)
                self._fail(failure)
                self._discard_translation_queue(failure)
                return
            finally:
                self._translation_queue.task_done()

    def _discard_translation_queue(self, failure: BaseException) -> None:
        """Fail receipts left behind by a stopped translation worker."""
        while True:
            try:
                item = self._translation_queue.get_nowait()
            except queue.Empty:
                return
            else:
                if isinstance(
                    item,
                    (_RawKeyboardTransition, _RawMouseTransition),
                ):
                    self._fail_receipt(item.receipt, failure)
                self._translation_queue.task_done()

    def _start_input_translation(self) -> None:
        thread = self._translation_thread
        if thread is not None and thread.is_alive():
            raise InputObserverError("Windows input translation worker is already running")
        self._translation_queue = queue.Queue(maxsize=self.translation_queue_size)
        self._translation_stop_requested.clear()
        thread = threading.Thread(
            target=self._input_translation_main,
            name=f"{type(self).__name__}-input-translation",
            daemon=True,
        )
        self._translation_thread = thread
        try:
            thread.start()
        except BaseException:
            self._translation_thread = None
            raise

    def _stop_input_translation(self, failures: list[str]) -> None:
        thread = self._translation_thread
        if thread is None:
            return
        self._translation_stop_requested.set()
        deadline = time.monotonic() + (self.shutdown_timeout * 0.8)
        if thread.is_alive():
            try:
                self._translation_queue.put_nowait(self._translation_sentinel)
            except queue.Full:
                pass
            thread.join(max(0.0, deadline - time.monotonic()))
        if thread.is_alive():
            failures.append(
                "input translation worker did not stop within " f"{self.shutdown_timeout:.1f}s"
            )
        else:
            self._translation_thread = None

    def _enqueue_input(self, transition: _RawInputTransition) -> None:
        try:
            self._translation_queue.put_nowait(transition)
        except queue.Full:
            failure = InputObserverError(
                "Windows input translation queue overflowed; " "recording coverage is incomplete"
            )
            self._fail(failure)
            raise failure

    def _keyboard_hook_callback(
        self,
        code: int,
        wparam: int,
        lparam: int,
    ) -> int:
        receipt: object | None = None
        callback_entered = False
        try:
            if code == HC_ACTION and wparam in {
                WM_KEYDOWN,
                WM_KEYUP,
                WM_SYSKEYDOWN,
                WM_SYSKEYUP,
            }:
                self._begin_native_callback()
                callback_entered = True
                timestamp = self._clock()
                payload = ctypes.cast(
                    lparam,
                    ctypes.POINTER(KBDLLHOOKSTRUCT),
                ).contents
                injected = bool(payload.flags & (LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED))
                if injected:
                    self._mark_native_activity()
                else:
                    receipt = self._reserve_receipt(timestamp)
                    keyboard_layout = self._foreground_keyboard_layout()
                    self._enqueue_input(
                        _RawKeyboardTransition(
                            pressed=wparam in {WM_KEYDOWN, WM_SYSKEYDOWN},
                            vk_code=int(payload.vkCode),
                            scan_code=int(payload.scanCode),
                            flags=int(payload.flags),
                            keyboard_layout=keyboard_layout,
                            timestamp=timestamp,
                            receipt=receipt,
                        )
                    )
        except BaseException as exc:
            self._fail_receipt(receipt, exc)
            self._record_callback_failure(exc)
        try:
            return self._call_next(self._keyboard_hook, code, wparam, lparam)
        finally:
            if callback_entered:
                try:
                    self._end_native_callback()
                except BaseException as exc:
                    self._record_callback_failure(exc)

    def _mouse_hook_callback(
        self,
        code: int,
        wparam: int,
        lparam: int,
    ) -> int:
        receipt: object | None = None
        callback_entered = False
        try:
            if code == HC_ACTION:
                observed_message = (
                    wparam in _BUTTON_MESSAGES
                    or wparam in {
                        WM_XBUTTONDOWN,
                        WM_XBUTTONUP,
                        WM_MOUSEWHEEL,
                        WM_MOUSEHWHEEL,
                    }
                    or (wparam == WM_MOUSEMOVE and self.capture_mouse_moves)
                )
                if observed_message:
                    self._begin_native_callback()
                    callback_entered = True
                    timestamp = self._clock()
                    payload = ctypes.cast(
                        lparam,
                        ctypes.POINTER(MSLLHOOKSTRUCT),
                    ).contents
                    injected = bool(
                        payload.flags
                        & (LLMHF_INJECTED | LLMHF_LOWER_IL_INJECTED)
                    )
                    if injected:
                        self._mark_native_activity()
                    else:
                        receipt = self._reserve_receipt(timestamp)
                        self._enqueue_input(
                            _RawMouseTransition(
                                message=wparam,
                                x=int(payload.pt.x),
                                y=int(payload.pt.y),
                                mouse_data=int(payload.mouseData),
                                flags=int(payload.flags),
                                timestamp=timestamp,
                                receipt=receipt,
                            )
                        )
        except BaseException as exc:
            self._fail_receipt(receipt, exc)
            self._record_callback_failure(exc)
        try:
            return self._call_next(self._mouse_hook, code, wparam, lparam)
        finally:
            if callback_entered:
                try:
                    self._end_native_callback()
                except BaseException as exc:
                    self._record_callback_failure(exc)

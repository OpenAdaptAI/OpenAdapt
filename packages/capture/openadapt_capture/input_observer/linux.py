"""Fail-closed Linux global input observation through the X RECORD extension.

The normative RECORD protocol guarantees exactly one selected ``device_event``
for every generated device event, whether or not any client receives it, in
device generation order.  Each delivered copy is a separate record after that
device event.  Device key/button records deliberately guarantee only ``time``
and ``detail``; device motion additionally guarantees root coordinates.  This
observer therefore never samples current X state after an event.  It preserves
the global device stream and enriches key/button events only from matching
delivered core events, whose wire records carry event-time state and
coordinates.  Missing enrichment degrades keyboard text to physical identity
and makes pointer actions fail closed unless an exact earlier pointer position
is known.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

from openadapt_capture.x11_threads import ensure_xlib_thread_support

from .base import (
    InputObserverError,
    InputObserverPermissionError,
    InputObserverUnavailableError,
    ObservedInput,
    ObservedKey,
    ObservedMouseButton,
    ObservedMouseMove,
    ObservedMouseScroll,
    ThreadedInputObserver,
    add_exception_note,
)

_KEY_PRESS = 2
_KEY_RELEASE = 3
_BUTTON_PRESS = 4
_BUTTON_RELEASE = 5
_MOTION_NOTIFY = 6
_CORE_DEVICE_EVENT_TYPES = {
    _KEY_PRESS,
    _KEY_RELEASE,
    _BUTTON_PRESS,
    _BUTTON_RELEASE,
    _MOTION_NOTIFY,
}

_XRECORD_FROM_SERVER = 0
_XRECORD_START_OF_DATA = 4
_XRECORD_END_OF_DATA = 5
_XRECORD_ALL_CLIENTS = 3
_XRECORD_FROM_SERVER_TIME = 1 << 0
_X_QUERY_POINTER = 38
_CORE_EVENT_BYTES = 32
_CORRELATION_TIMEOUT_SECONDS = 0.100
_LOOP_INTERVAL_SECONDS = 0.010

_XKB_COMPOSE_COMPOSING = 1
_XKB_COMPOSE_COMPOSED = 2
_XKB_COMPOSE_CANCELLED = 3

_SPECIAL_KEY_NAMES = {
    "Alt_L": "alt",
    "Alt_R": "alt_r",
    "BackSpace": "backspace",
    "Caps_Lock": "caps_lock",
    "Control_L": "ctrl",
    "Control_R": "ctrl_r",
    "Delete": "delete",
    "Down": "down",
    "End": "end",
    "Escape": "esc",
    "Home": "home",
    "Left": "left",
    "Meta_L": "cmd",
    "Meta_R": "cmd_r",
    "Page_Down": "page_down",
    "Page_Up": "page_up",
    "Return": "enter",
    "Right": "right",
    "Shift_L": "shift",
    "Shift_R": "shift_r",
    "Super_L": "cmd",
    "Super_R": "cmd_r",
    "Tab": "tab",
    "Up": "up",
    "space": "space",
}
_KEYSYM_CHARACTERS = {
    "ampersand": "&",
    "apostrophe": "'",
    "asciicircum": "^",
    "asciitilde": "~",
    "asterisk": "*",
    "at": "@",
    "backslash": "\\",
    "bar": "|",
    "braceleft": "{",
    "braceright": "}",
    "bracketleft": "[",
    "bracketright": "]",
    "colon": ":",
    "comma": ",",
    "dollar": "$",
    "equal": "=",
    "exclam": "!",
    "greater": ">",
    "less": "<",
    "minus": "-",
    "numbersign": "#",
    "parenleft": "(",
    "parenright": ")",
    "percent": "%",
    "period": ".",
    "plus": "+",
    "question": "?",
    "quotedbl": '"',
    "semicolon": ";",
    "slash": "/",
    "underscore": "_",
}


class _XRecordRange8(ctypes.Structure):
    _fields_ = [
        ("first", ctypes.c_ubyte),
        ("last", ctypes.c_ubyte),
    ]


class _XRecordRange16(ctypes.Structure):
    _fields_ = [
        ("first", ctypes.c_ushort),
        ("last", ctypes.c_ushort),
    ]


class _XRecordExtRange(ctypes.Structure):
    _fields_ = [
        ("ext_major", _XRecordRange8),
        ("ext_minor", _XRecordRange16),
    ]


class _XRecordRange(ctypes.Structure):
    """ABI mirror of ``X11/extensions/record.h::XRecordRange``."""

    _fields_ = [
        ("core_requests", _XRecordRange8),
        ("core_replies", _XRecordRange8),
        ("ext_requests", _XRecordExtRange),
        ("ext_replies", _XRecordExtRange),
        ("delivered_events", _XRecordRange8),
        ("device_events", _XRecordRange8),
        ("errors", _XRecordRange8),
        ("client_started", ctypes.c_int),
        ("client_died", ctypes.c_int),
    ]


class _XRecordInterceptData(ctypes.Structure):
    """ABI mirror of ``XRecordInterceptData`` from ``record.h``."""

    _fields_ = [
        ("id_base", ctypes.c_ulong),
        ("server_time", ctypes.c_ulong),
        ("client_seq", ctypes.c_ulong),
        ("category", ctypes.c_int),
        ("client_swapped", ctypes.c_int),
        ("data", ctypes.POINTER(ctypes.c_ubyte)),
        ("data_len", ctypes.c_ulong),
    ]


_XRecordInterceptProc = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p,
    ctypes.POINTER(_XRecordInterceptData),
)


@dataclass(frozen=True, slots=True)
class _CoreWireEvent:
    event_type: int
    detail: int
    injected: bool
    time: int
    root: int
    event: int
    child: int
    root_x: int
    root_y: int
    event_x: int
    event_y: int
    state: int


@dataclass(frozen=True, slots=True)
class _DeliveredCandidate:
    state: int
    root_x: float
    root_y: float
    injected: bool
    id_base: int


@dataclass(slots=True)
class _PendingDeviceEvent:
    event_type: int
    detail: int
    server_time: int
    injected: bool
    receipt_timestamp: float
    deadline: float
    candidate: _DeliveredCandidate | None = None


def _event_byteorder(*, client_swapped: bool) -> str:
    native_little = sys.byteorder == "little"
    event_little = native_little != client_swapped
    return "little" if event_little else "big"


def _decode_core_event(
    data: bytes,
    *,
    client_swapped: bool,
) -> _CoreWireEvent:
    """Decode one 32-byte core xEvent in the recorded client's byte order."""
    if len(data) != _CORE_EVENT_BYTES:
        raise InputObserverError(
            f"X RECORD core event was {len(data)} bytes, expected "
            f"{_CORE_EVENT_BYTES}"
        )
    byteorder = _event_byteorder(client_swapped=client_swapped)
    encoded_type = data[0]
    event_type = encoded_type & 0x7F
    return _CoreWireEvent(
        event_type=event_type,
        detail=data[1],
        injected=bool(encoded_type & 0x80),
        time=int.from_bytes(data[4:8], byteorder),
        root=int.from_bytes(data[8:12], byteorder),
        event=int.from_bytes(data[12:16], byteorder),
        child=int.from_bytes(data[16:20], byteorder),
        root_x=int.from_bytes(data[20:22], byteorder, signed=True),
        root_y=int.from_bytes(data[22:24], byteorder, signed=True),
        event_x=int.from_bytes(data[24:26], byteorder, signed=True),
        event_y=int.from_bytes(data[26:28], byteorder, signed=True),
        state=int.from_bytes(data[28:30], byteorder),
    )


def normalize_xinput_button_event(
    *,
    detail: int,
    pressed: bool,
    x: float,
    y: float,
    injected: bool = False,
    timestamp: float | None = None,
) -> ObservedInput | None:
    """Map X11 core button numbers to a click or normalized wheel event."""
    if detail in {4, 5, 6, 7}:
        if not pressed:
            return None
        delta_by_button = {
            4: (0.0, 1.0),
            5: (0.0, -1.0),
            6: (-1.0, 0.0),
            7: (1.0, 0.0),
        }
        dx, dy = delta_by_button[detail]
        return ObservedMouseScroll(
            x=x,
            y=y,
            dx=dx,
            dy=dy,
            injected=injected,
            timestamp=timestamp,
        )
    button_by_detail = {
        1: "left",
        2: "middle",
        3: "right",
        8: "x1",
        9: "x2",
    }
    button = button_by_detail.get(detail)
    if button is None and detail > 0:
        button = f"button{detail}"
    if button is None:
        return None
    return ObservedMouseButton(
        x=x,
        y=y,
        button=button,
        pressed=pressed,
        injected=injected,
        timestamp=timestamp,
    )


def normalize_xinput_key_event(
    *,
    keycode: int,
    pressed: bool,
    keysym_name: str | None,
    injected: bool = False,
    character: str | None = None,
    derive_character: bool = True,
    timestamp: float | None = None,
) -> ObservedKey:
    """Normalize an X11 keycode and XKB keysym name."""
    key_name = _SPECIAL_KEY_NAMES.get(keysym_name or "")
    if derive_character and keysym_name:
        if len(keysym_name) == 1 and keysym_name.isprintable():
            character = keysym_name
        else:
            character = _KEYSYM_CHARACTERS.get(keysym_name)
    if key_name is None and character is None:
        key_name = keysym_name or f"keycode_{keycode}"
    virtual_key = str(keycode)
    return ObservedKey(
        pressed=pressed,
        key_name=key_name,
        key_char=character,
        key_vk=virtual_key,
        canonical_key_name=key_name,
        canonical_key_char=character.lower() if character else None,
        canonical_key_vk=virtual_key,
        injected=injected,
        timestamp=timestamp,
    )


class LinuxXInputObserver(ThreadedInputObserver):
    """Observe complete X11 input without post-event state sampling."""

    def __init__(self, *args, environ: dict[str, str] | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._environ = environ if environ is not None else os.environ
        self._x11: Any = None
        self._xtst: Any = None
        self._xkbcommon: Any = None
        self._control_display: Any = None
        self._data_display: Any = None
        self._record_range: Any = None
        self._record_context = 0
        self._record_enabled = False
        self._record_started = False
        self._record_ended = False
        self._setup_complete = False
        self._accepting_events = False
        self._waiting_baseline_marker = False
        self._baseline_marker_seen = False
        self._control_id_base = 0
        self._root = 0
        self._record_callback: Any = None
        self._record_callback_failure: BaseException | None = None
        self._pending: _PendingDeviceEvent | None = None
        self._delivered_correlation_uncertain = False
        self._last_pointer: tuple[float, float] | None = None
        self._text_state_uncertain = False
        self._composition_mode: str | None = None
        self._composition_remaining = 0
        self._unverifiable_keycodes: set[int] = set()
        self._compose_context: Any = None
        self._compose_table: Any = None
        self._compose_state: Any = None

    def _setup(self) -> None:
        if not sys.platform.startswith("linux"):
            raise InputObserverUnavailableError(
                "X RECORD input observation is available only on Linux"
            )
        session_type = self._environ.get("XDG_SESSION_TYPE", "").lower()
        if session_type == "wayland" or self._environ.get("WAYLAND_DISPLAY"):
            raise InputObserverUnavailableError(
                "native Wayland does not expose complete global input to X RECORD. "
                "Run an X11 session; OpenAdapt refuses XWayland-only capture because "
                "it would silently omit input from native Wayland applications."
            )
        display_name = self._environ.get("DISPLAY")
        if not display_name:
            raise InputObserverUnavailableError(
                "X RECORD requires an X11 desktop and DISPLAY is not set"
            )

        # The factory normally performs this process-wide initialization, but
        # the observer is also a public class and may be constructed directly.
        # XInitThreads must precede every other Xlib call or library-backed
        # capture may be unsafe once screen and input threads run concurrently.
        ensure_xlib_thread_support()

        x11_name = ctypes.util.find_library("X11")
        xtst_name = ctypes.util.find_library("Xtst")
        xkbcommon_name = ctypes.util.find_library("xkbcommon")
        if not x11_name or not xtst_name or not xkbcommon_name:
            raise InputObserverUnavailableError(
                "Linux input observation requires the system libX11, libXtst "
                "(X RECORD), and permissively licensed libxkbcommon runtime libraries"
            )
        try:
            self._x11 = ctypes.CDLL(x11_name)
            self._xtst = ctypes.CDLL(xtst_name)
            self._xkbcommon = ctypes.CDLL(xkbcommon_name)
        except OSError as exc:
            raise InputObserverUnavailableError(
                f"could not load X11 input libraries: {exc}"
            ) from exc

        self._configure_api()
        self._initialize_compose()
        encoded_display = display_name.encode()
        self._control_display = self._x11.XOpenDisplay(encoded_display)
        if not self._control_display:
            raise InputObserverPermissionError(
                f"could not open X11 display {display_name!r}; authorize the "
                "recording user with the display server and retry"
            )
        self._data_display = self._x11.XOpenDisplay(encoded_display)
        if not self._data_display:
            raise InputObserverPermissionError(
                f"could not open the X RECORD data connection to {display_name!r}"
            )
        self._root = int(self._x11.XDefaultRootWindow(self._control_display))
        probe_resource = int(
            self._x11.XCreatePixmap(
                self._control_display,
                self._root,
                1,
                1,
                1,
            )
        )
        if not probe_resource:
            raise InputObserverError(
                "X11 could not allocate a resource ID for the recording boundary"
            )
        id_base_mask = int(self._xtst.XRecordIdBaseMask(self._control_display))
        self._control_id_base = probe_resource & id_base_mask
        self._x11.XFreePixmap(self._control_display, probe_resource)

        major = ctypes.c_int()
        minor = ctypes.c_int()
        if not self._xtst.XRecordQueryVersion(
            self._control_display,
            ctypes.byref(major),
            ctypes.byref(minor),
        ):
            raise InputObserverUnavailableError(
                "the X11 server does not expose the X RECORD extension"
            )

        record_range = self._xtst.XRecordAllocRange()
        if not record_range:
            raise InputObserverUnavailableError(
                "libXtst could not allocate an X RECORD event range"
            )
        self._record_range = record_range
        # QueryPointer replies serve only as an ordered setup marker.  Input
        # acceptance begins inside the callback that observes our control
        # connection's reply, after the exact baseline position is stored.
        record_range.contents.core_replies = _XRecordRange8(
            _X_QUERY_POINTER,
            _X_QUERY_POINTER,
        )
        record_range.contents.delivered_events = _XRecordRange8(
            _KEY_PRESS,
            _MOTION_NOTIFY,
        )
        record_range.contents.device_events = _XRecordRange8(
            _KEY_PRESS,
            _MOTION_NOTIFY,
        )
        client_specs = (ctypes.c_ulong * 1)(_XRECORD_ALL_CLIENTS)
        ranges = (ctypes.POINTER(_XRecordRange) * 1)(record_range)
        context = self._xtst.XRecordCreateContext(
            self._control_display,
            _XRECORD_FROM_SERVER_TIME,
            client_specs,
            1,
            ranges,
            1,
        )
        if not context:
            raise InputObserverPermissionError(
                "the X11 server refused creation of a global X RECORD context"
            )
        self._record_context = int(context)
        self._record_callback = _XRecordInterceptProc(self._record_intercept)
        if not self._xtst.XRecordEnableContextAsync(
            self._data_display,
            self._record_context,
            self._record_callback,
            None,
        ):
            raise InputObserverPermissionError(
                "the X11 server refused to enable the global X RECORD context"
            )
        self._record_enabled = True
        self._raise_callback_failure()
        if not self._record_started:
            raise InputObserverError(
                "XRecordEnableContextAsync returned before StartOfData; "
                "the event stream cannot be trusted"
            )
        self._waiting_baseline_marker = True
        self._query_pointer_baseline()
        self._wait_for_baseline_marker()
        self._raise_if_setup_cancelled()
        if not self._accepting_events:
            raise InputObserverError(
                "X RECORD observed the pointer-baseline marker without arming input"
            )
        self._setup_complete = True

    def _raise_if_setup_cancelled(self) -> None:
        """Keep an outer readiness timeout from becoming a partial start."""
        if (
            not self._stop_requested.is_set()
            and self._startup_failure is None
        ):
            return
        self._waiting_baseline_marker = False
        self._accepting_events = False
        raise InputObserverError(
            "X RECORD setup was cancelled before the input boundary was ready"
        )

    def _wait_for_baseline_marker(
        self,
        *,
        timeout: float | None = None,
    ) -> None:
        """Wait interruptibly for the ordered QueryPointer boundary."""
        marker_timeout = self.startup_timeout if timeout is None else timeout
        marker_deadline = time.monotonic() + marker_timeout
        while not self._baseline_marker_seen:
            self._raise_if_setup_cancelled()
            self._xtst.XRecordProcessReplies(self._data_display)
            self._raise_if_setup_cancelled()
            self._raise_callback_failure()
            if time.monotonic() >= marker_deadline:
                raise InputObserverError(
                    "X RECORD did not deliver the ordered pointer-baseline marker "
                    f"within {marker_timeout:.1f}s"
                )
            self._stop_requested.wait(0.001)

    def _configure_api(self) -> None:
        self._x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self._x11.XOpenDisplay.restype = ctypes.c_void_p
        self._x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self._x11.XCloseDisplay.restype = ctypes.c_int
        self._x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        self._x11.XDefaultRootWindow.restype = ctypes.c_ulong
        self._x11.XCreatePixmap.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_uint,
        ]
        self._x11.XCreatePixmap.restype = ctypes.c_ulong
        self._x11.XFreePixmap.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self._x11.XFreePixmap.restype = ctypes.c_int
        self._x11.XFree.argtypes = [ctypes.c_void_p]
        self._x11.XFree.restype = ctypes.c_int
        self._x11.XQueryPointer.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_uint),
        ]
        self._x11.XQueryPointer.restype = ctypes.c_int
        self._x11.XkbLookupKeySym.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ubyte,
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_ulong),
        ]
        self._x11.XkbLookupKeySym.restype = ctypes.c_int
        self._x11.XKeysymToString.argtypes = [ctypes.c_ulong]
        self._x11.XKeysymToString.restype = ctypes.c_char_p

        self._xtst.XRecordQueryVersion.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
        ]
        self._xtst.XRecordQueryVersion.restype = ctypes.c_int
        self._xtst.XRecordIdBaseMask.argtypes = [ctypes.c_void_p]
        self._xtst.XRecordIdBaseMask.restype = ctypes.c_ulong
        self._xtst.XRecordAllocRange.argtypes = []
        self._xtst.XRecordAllocRange.restype = ctypes.POINTER(_XRecordRange)
        self._xtst.XRecordCreateContext.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.c_int,
            ctypes.POINTER(ctypes.POINTER(_XRecordRange)),
            ctypes.c_int,
        ]
        self._xtst.XRecordCreateContext.restype = ctypes.c_ulong
        self._xtst.XRecordEnableContextAsync.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            _XRecordInterceptProc,
            ctypes.c_void_p,
        ]
        self._xtst.XRecordEnableContextAsync.restype = ctypes.c_int
        self._xtst.XRecordProcessReplies.argtypes = [ctypes.c_void_p]
        self._xtst.XRecordProcessReplies.restype = None
        self._xtst.XRecordFreeData.argtypes = [
            ctypes.POINTER(_XRecordInterceptData)
        ]
        self._xtst.XRecordFreeData.restype = None
        self._xtst.XRecordDisableContext.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
        ]
        self._xtst.XRecordDisableContext.restype = ctypes.c_int
        self._xtst.XRecordFreeContext.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
        ]
        self._xtst.XRecordFreeContext.restype = ctypes.c_int

        self._xkbcommon.xkb_keysym_to_utf8.argtypes = [
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_char),
            ctypes.c_size_t,
        ]
        self._xkbcommon.xkb_keysym_to_utf8.restype = ctypes.c_int
        self._xkbcommon.xkb_context_new.argtypes = [ctypes.c_int]
        self._xkbcommon.xkb_context_new.restype = ctypes.c_void_p
        self._xkbcommon.xkb_context_unref.argtypes = [ctypes.c_void_p]
        self._xkbcommon.xkb_compose_table_new_from_locale.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        self._xkbcommon.xkb_compose_table_new_from_locale.restype = ctypes.c_void_p
        self._xkbcommon.xkb_compose_table_unref.argtypes = [ctypes.c_void_p]
        self._xkbcommon.xkb_compose_state_new.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        self._xkbcommon.xkb_compose_state_new.restype = ctypes.c_void_p
        self._xkbcommon.xkb_compose_state_unref.argtypes = [ctypes.c_void_p]
        self._xkbcommon.xkb_compose_state_feed.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        self._xkbcommon.xkb_compose_state_feed.restype = ctypes.c_int
        self._xkbcommon.xkb_compose_state_get_status.argtypes = [ctypes.c_void_p]
        self._xkbcommon.xkb_compose_state_get_status.restype = ctypes.c_int
        self._xkbcommon.xkb_compose_state_get_utf8.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_char),
            ctypes.c_size_t,
        ]
        self._xkbcommon.xkb_compose_state_get_utf8.restype = ctypes.c_int
        self._xkbcommon.xkb_compose_state_reset.argtypes = [ctypes.c_void_p]

    def _initialize_compose(self) -> None:
        locale_name = (
            self._environ.get("LC_ALL")
            or self._environ.get("LC_CTYPE")
            or self._environ.get("LANG")
            or "C"
        )
        self._compose_context = self._xkbcommon.xkb_context_new(0)
        if not self._compose_context:
            raise InputObserverUnavailableError(
                "libxkbcommon could not create a compose context"
            )
        self._compose_table = self._xkbcommon.xkb_compose_table_new_from_locale(
            self._compose_context,
            locale_name.encode(),
            0,
        )
        if not self._compose_table:
            raise InputObserverUnavailableError(
                f"libxkbcommon could not load compose rules for locale {locale_name!r}"
            )
        self._compose_state = self._xkbcommon.xkb_compose_state_new(
            self._compose_table,
            0,
        )
        if not self._compose_state:
            raise InputObserverUnavailableError(
                "libxkbcommon could not create compose state"
            )

    def _record_intercept(
        self,
        _closure: ctypes.c_void_p,
        recorded_data_pointer: ctypes.POINTER(_XRecordInterceptData),
    ) -> None:
        """Copy and process one RECORD datum without leaking across the callback."""
        try:
            recorded = recorded_data_pointer.contents
            if recorded.category == _XRECORD_START_OF_DATA:
                self._record_started = True
                return
            if recorded.category == _XRECORD_END_OF_DATA:
                self._record_ended = True
                return
            if self._record_callback_failure is not None:
                # After coverage has failed, drain only to the terminal marker.
                # Emitting later records would make a failed recording appear
                # partially usable.
                return
            if recorded.category != _XRECORD_FROM_SERVER:
                return
            byte_length = int(recorded.data_len) * 4
            if byte_length != _CORE_EVENT_BYTES or not recorded.data:
                raise InputObserverError(
                    "X RECORD returned a malformed core input event: "
                    f"{byte_length} bytes"
                )
            payload = ctypes.string_at(recorded.data, byte_length)
            if (
                self._waiting_baseline_marker
                and int(recorded.id_base) == self._control_id_base
                and payload[0] == 1
            ):
                self._waiting_baseline_marker = False
                if (
                    self._stop_requested.is_set()
                    or self._startup_failure is not None
                ):
                    self._accepting_events = False
                    return
                self._baseline_marker_seen = True
                self._accepting_events = True
                return
            if self._delivery_start_was_aborted():
                # XRecordProcessReplies may have copied a complete native batch
                # before the outer start() deadline fired. Once that transaction
                # is cancelled, do not process the remaining non-marker records
                # from the already-armed batch. A normal committed shutdown still
                # drains its tail through EndOfData below.
                self._waiting_baseline_marker = False
                self._accepting_events = False
                return
            event = _decode_core_event(
                payload,
                client_swapped=bool(recorded.client_swapped),
            )
            if event.event_type not in _CORE_DEVICE_EVENT_TYPES:
                # QueryPointer replies from other clients are included by the
                # marker range but cannot arm this observer.
                return
            if not self._accepting_events:
                # Establishing the baseline is a deliberate cut: every event
                # before our recorded QueryPointer reply is discarded.
                return
            if int(recorded.id_base) == 0:
                # Normative RECORD semantics make this the one global record
                # for every generated device event.  Zero does not mean the
                # event was necessarily undelivered; any delivered copies
                # follow separately with their target client's id-base.
                if recorded.client_swapped:
                    raise InputObserverError(
                        "X RECORD marked a device event as byte-swapped; "
                        "device-event byte order must match the recording client"
                    )
                self._handle_device_event(event)
            else:
                self._handle_delivered_event(event, id_base=int(recorded.id_base))
        except BaseException as exc:
            if self._record_callback_failure is None:
                self._record_callback_failure = exc
            self._stop_requested.set()
        finally:
            self._xtst.XRecordFreeData(recorded_data_pointer)

    def _query_pointer_baseline(self) -> None:
        """Sample the pointer once, before the recorded acceptance boundary."""
        root_return = ctypes.c_ulong()
        child_return = ctypes.c_ulong()
        root_x = ctypes.c_int()
        root_y = ctypes.c_int()
        window_x = ctypes.c_int()
        window_y = ctypes.c_int()
        mask = ctypes.c_uint()
        if not self._x11.XQueryPointer(
            self._control_display,
            self._root,
            ctypes.byref(root_return),
            ctypes.byref(child_return),
            ctypes.byref(root_x),
            ctypes.byref(root_y),
            ctypes.byref(window_x),
            ctypes.byref(window_y),
            ctypes.byref(mask),
        ):
            raise InputObserverError(
                "X11 could not establish the exact pointer position at the "
                "recording boundary"
            )
        self._last_pointer = (float(root_x.value), float(root_y.value))

    def _handle_device_event(self, event: _CoreWireEvent) -> None:
        """Accept the next event in the global device stream."""
        self._finalize_pending()
        observed_at = time.time()
        if event.event_type == _MOTION_NOTIFY:
            if not self.observe_mouse:
                return
            position = (float(event.root_x), float(event.root_y))
            self._last_pointer = position
            if self.capture_mouse_moves:
                self._emit(
                    ObservedMouseMove(
                        x=position[0],
                        y=position[1],
                        injected=event.injected,
                        timestamp=observed_at,
                    )
                )
            return
        if event.event_type in {_KEY_PRESS, _KEY_RELEASE}:
            if not self.observe_keyboard:
                return
        elif event.event_type in {_BUTTON_PRESS, _BUTTON_RELEASE}:
            if not self.observe_mouse:
                return
        else:  # pragma: no cover - guarded by the callback range check
            return
        self._pending = _PendingDeviceEvent(
            event_type=event.event_type,
            detail=event.detail,
            server_time=event.time,
            injected=event.injected,
            receipt_timestamp=observed_at,
            deadline=time.monotonic() + _CORRELATION_TIMEOUT_SECONDS,
        )

    def _handle_delivered_event(
        self,
        event: _CoreWireEvent,
        *,
        id_base: int,
    ) -> None:
        """Attach exact delivered-event state to the matching device event."""
        pending = self._pending
        if pending is None or self._delivered_correlation_uncertain:
            return
        if (
            event.event_type != pending.event_type
            or event.time != pending.server_time
            or event.detail != pending.detail
        ):
            return
        candidate = _DeliveredCandidate(
            state=event.state,
            root_x=float(event.root_x),
            root_y=float(event.root_y),
            injected=event.injected,
            id_base=id_base,
        )
        current = pending.candidate
        if current is not None and (
            current.state != candidate.state
            or current.root_x != candidate.root_x
            or current.root_y != candidate.root_y
            or current.injected != candidate.injected
        ):
            raise InputObserverError(
                "X RECORD delivered conflicting event-time state for the same "
                f"device event from clients {current.id_base:#x} and {id_base:#x}"
            )
        if current is None:
            pending.candidate = candidate

    def _finalize_pending(self) -> None:
        pending = self._pending
        if pending is None:
            return
        self._pending = None
        candidate = pending.candidate
        if candidate is None:
            # RECORD guarantees delivered copies precede the next device event
            # only in the absence of grabs.  Once one copy is late or absent,
            # a later identical (type, time, detail) tuple could be stale.
            # Permanently stop correlating rather than risk attaching it to a
            # newer physical event.
            self._delivered_correlation_uncertain = True
        injected = pending.injected or (
            candidate.injected if candidate is not None else False
        )
        if pending.event_type in {_KEY_PRESS, _KEY_RELEASE}:
            pressed = pending.event_type == _KEY_PRESS
            if candidate is None:
                self._text_state_uncertain = True
                if self._compose_state is not None:
                    self._xkbcommon.xkb_compose_state_reset(self._compose_state)
                self._emit(
                    normalize_xinput_key_event(
                        keycode=pending.detail,
                        pressed=pressed,
                        keysym_name=None,
                        injected=injected,
                        character=None,
                        derive_character=False,
                        timestamp=pending.receipt_timestamp,
                    )
                )
                return
            keysym, keysym_name = self._lookup_keysym(
                pending.detail,
                candidate.state,
            )
            self._emit(
                normalize_xinput_key_event(
                    keycode=pending.detail,
                    pressed=pressed,
                    keysym_name=keysym_name,
                    injected=injected,
                    character=self._resolved_character(
                        keycode=pending.detail,
                        keysym=keysym,
                        keysym_name=keysym_name,
                        pressed=pressed,
                    ),
                    derive_character=False,
                    timestamp=pending.receipt_timestamp,
                )
            )
            return

        if candidate is not None:
            position = (candidate.root_x, candidate.root_y)
            self._last_pointer = position
        else:
            position = self._last_pointer
        if position is None:
            raise InputObserverError(
                "X RECORD delivered a button event without event-time coordinates "
                "before any exact pointer position was observed"
            )
        event = normalize_xinput_button_event(
            detail=pending.detail,
            pressed=pending.event_type == _BUTTON_PRESS,
            x=position[0],
            y=position[1],
            injected=injected,
            timestamp=pending.receipt_timestamp,
        )
        if event is not None:
            self._emit(event)

    def _finalize_expired_pending(self) -> None:
        pending = self._pending
        if pending is not None and time.monotonic() >= pending.deadline:
            self._finalize_pending()

    def _lookup_keysym(
        self,
        keycode: int,
        event_state: int,
    ) -> tuple[int, str | None]:
        """Resolve a key using the state carried by its delivered wire event."""
        consumed_modifiers = ctypes.c_uint()
        keysym = ctypes.c_ulong()
        if not self._x11.XkbLookupKeySym(
            self._control_display,
            keycode,
            event_state,
            ctypes.byref(consumed_modifiers),
            ctypes.byref(keysym),
        ):
            raise InputObserverError(
                f"XKB could not resolve keycode {keycode} from its event-time state"
            )
        name_pointer = (
            self._x11.XKeysymToString(keysym.value) if keysym.value else None
        )
        name = name_pointer.decode(errors="replace") if name_pointer else None
        return int(keysym.value), name

    def _compose_character(self, keysym: int) -> tuple[bool, str | None]:
        """Return whether compose consumed this press and any committed text."""
        self._xkbcommon.xkb_compose_state_feed(self._compose_state, keysym)
        status = int(
            self._xkbcommon.xkb_compose_state_get_status(self._compose_state)
        )
        if status == _XKB_COMPOSE_COMPOSING:
            return True, None
        if status == _XKB_COMPOSE_COMPOSED:
            buffer = ctypes.create_string_buffer(64)
            length = int(
                self._xkbcommon.xkb_compose_state_get_utf8(
                    self._compose_state,
                    buffer,
                    len(buffer),
                )
            )
            self._xkbcommon.xkb_compose_state_reset(self._compose_state)
            if length <= 0:
                return True, None
            return True, buffer.value.decode("utf-8", errors="strict")
        if status == _XKB_COMPOSE_CANCELLED:
            self._xkbcommon.xkb_compose_state_reset(self._compose_state)
            return True, None
        return False, None

    def _keysym_character(self, keysym: int) -> str | None:
        buffer = ctypes.create_string_buffer(64)
        length = int(
            self._xkbcommon.xkb_keysym_to_utf8(
                keysym,
                buffer,
                len(buffer),
            )
        )
        if length <= 1:
            return None
        return buffer.value.decode("utf-8", errors="strict")

    def _resolved_character(
        self,
        *,
        keycode: int,
        keysym: int,
        keysym_name: str | None,
        pressed: bool,
    ) -> str | None:
        if self._text_state_uncertain:
            return None
        if not pressed and keycode in self._unverifiable_keycodes:
            self._unverifiable_keycodes.discard(keycode)
            return None
        if pressed and self._compose_state is not None:
            consumed, composed_text = self._compose_character(keysym)
            if consumed:
                self._unverifiable_keycodes.add(keycode)
                return composed_text

        # Pure-unit and defensive fallback when no compose state exists.
        # Production setup fails unless locale-aware compose state was created.
        if keysym_name and (
            keysym_name.startswith("dead_")
            or keysym_name in {"Multi_key", "Compose"}
        ):
            if pressed:
                self._composition_mode = (
                    "dead" if keysym_name.startswith("dead_") else "multi"
                )
                self._composition_remaining = (
                    1 if self._composition_mode == "dead" else 2
                )
                self._unverifiable_keycodes.add(keycode)
            return None

        special_name = _SPECIAL_KEY_NAMES.get(keysym_name or "")
        if self._composition_mode is not None and special_name in {
            "enter",
            "esc",
            "space",
        }:
            if pressed:
                self._composition_mode = None
                self._composition_remaining = 0
                self._unverifiable_keycodes.add(keycode)
            return None
        if self._composition_mode is not None and special_name is None:
            if pressed:
                self._unverifiable_keycodes.add(keycode)
                self._composition_remaining -= 1
                if self._composition_remaining <= 0:
                    self._composition_mode = None
                    self._composition_remaining = 0
            return None
        if special_name is not None:
            return None
        return self._keysym_character(keysym)

    def _raise_callback_failure(self) -> None:
        failure = self._record_callback_failure
        if failure is not None:
            raise failure

    def _run_loop(self) -> None:
        while not self._stop_requested.is_set():
            self._xtst.XRecordProcessReplies(self._data_display)
            self._raise_callback_failure()
            self._finalize_expired_pending()
            self._stop_requested.wait(_LOOP_INTERVAL_SECONDS)
        self._raise_callback_failure()

    def _drain_record_tail(self) -> None:
        """Drain DisableContext's complete tail through EndOfData, or refuse."""
        # This drain runs inside the outer observer thread's join window.  Use
        # at most 80% so native frees, display closes, delivery shutdown, and
        # scheduler jitter retain a meaningful cleanup budget.
        drain_budget = max(0.0, self.shutdown_timeout * 0.8)
        deadline = time.monotonic() + drain_budget
        while not self._record_ended:
            self._xtst.XRecordProcessReplies(self._data_display)
            if self._record_ended:
                break
            if time.monotonic() >= deadline:
                timeout = InputObserverError(
                    "X RECORD did not deliver EndOfData within the bounded "
                    f"{drain_budget:.3f}s shutdown drain"
                )
                if self._record_callback_failure is not None:
                    add_exception_note(
                        self._record_callback_failure,
                        str(timeout),
                    )
                    raise self._record_callback_failure
                raise timeout
            time.sleep(
                min(
                    _LOOP_INTERVAL_SECONDS,
                    max(0.0, deadline - time.monotonic()),
                )
            )
        self._raise_callback_failure()
        # RECORD guarantees DisableContext flushes all complete protocol
        # elements before EndOfData.  Only now is the last device event known
        # to have received every possible delivered copy.
        self._finalize_pending()

    def _teardown(self) -> None:
        cleanup_failures: list[BaseException] = []

        def attempt(label: str, operation):
            try:
                return operation()
            except BaseException as exc:
                add_exception_note(exc, label)
                cleanup_failures.append(exc)
                return None

        if self._record_enabled and (
            not self._setup_complete or self._startup_failure is not None
        ):
            # A startup timeout can leave the ordered QueryPointer marker in
            # the server tail, including after _setup completes but before the
            # base lifecycle publishes readiness.  Teardown must free that
            # marker and subsequent records without letting a failed start
            # arm or retain event acceptance.
            self._waiting_baseline_marker = False
            self._accepting_events = False
        if (
            self._record_enabled
            and self._record_context
            and self._control_display
            and self._xtst is not None
        ):
            disabled = attempt(
                "while disabling the X RECORD context",
                lambda: self._xtst.XRecordDisableContext(
                    self._control_display,
                    self._record_context,
                ),
            )
            if disabled:
                attempt(
                    "while draining the X RECORD shutdown tail",
                    self._drain_record_tail,
                )
            elif disabled is not None:
                cleanup_failures.append(
                    InputObserverError(
                        "X RECORD context could not be disabled cleanly"
                    )
                )
        self._record_enabled = False
        if (
            self._record_context
            and self._control_display
            and self._xtst is not None
        ):
            freed_context = attempt(
                "while freeing the X RECORD context",
                lambda: self._xtst.XRecordFreeContext(
                    self._control_display,
                    self._record_context,
                ),
            )
            if freed_context is not None and not freed_context:
                cleanup_failures.append(
                    InputObserverError(
                        "X RECORD context could not be freed cleanly"
                    )
                )
        self._record_context = 0
        if self._record_range is not None and self._x11 is not None:
            attempt(
                "while freeing the X RECORD range",
                lambda: self._x11.XFree(self._record_range),
            )
        self._record_range = None
        if self._data_display and self._x11 is not None:
            attempt(
                "while closing the X RECORD data display",
                lambda: self._x11.XCloseDisplay(self._data_display),
            )
        if self._control_display and self._x11 is not None:
            attempt(
                "while closing the X RECORD control display",
                lambda: self._x11.XCloseDisplay(self._control_display),
            )
        self._data_display = None
        self._control_display = None
        if self._xkbcommon is not None:
            if self._compose_state:
                attempt(
                    "while releasing the XKB compose state",
                    lambda: self._xkbcommon.xkb_compose_state_unref(
                        self._compose_state
                    ),
                )
            if self._compose_table:
                attempt(
                    "while releasing the XKB compose table",
                    lambda: self._xkbcommon.xkb_compose_table_unref(
                        self._compose_table
                    ),
                )
            if self._compose_context:
                attempt(
                    "while releasing the XKB context",
                    lambda: self._xkbcommon.xkb_context_unref(
                        self._compose_context
                    ),
                )
        self._compose_state = None
        self._compose_table = None
        self._compose_context = None
        self._record_callback = None
        self._record_callback_failure = None
        self._record_started = False
        self._record_ended = False
        self._setup_complete = False
        self._accepting_events = False
        self._waiting_baseline_marker = False
        self._baseline_marker_seen = False
        self._control_id_base = 0
        self._root = 0
        self._pending = None
        self._delivered_correlation_uncertain = False
        self._last_pointer = None
        self._text_state_uncertain = False
        self._composition_mode = None
        self._composition_remaining = 0
        self._unverifiable_keycodes.clear()
        if cleanup_failures:
            primary = cleanup_failures[0]
            for secondary in cleanup_failures[1:]:
                add_exception_note(
                    primary,
                    f"additional teardown failure: {secondary}",
                )
            raise primary


__all__ = [
    "LinuxXInputObserver",
    "normalize_xinput_button_event",
    "normalize_xinput_key_event",
]

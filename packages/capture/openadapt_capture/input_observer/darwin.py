"""Native macOS global input observation through Quartz CGEventTap."""

from __future__ import annotations

import threading
import time
from typing import Any

from .base import (
    InputCallback,
    InputObserverError,
    InputObserverPermissionError,
    InputObserverUnavailableError,
    ObservedKey,
    ObservedMouseButton,
    ObservedMouseMove,
    ObservedMouseScroll,
    ThreadedInputObserver,
)

_KEY_NAMES = {
    36: "enter",
    48: "tab",
    49: "space",
    51: "backspace",
    53: "esc",
    54: "cmd_r",
    55: "cmd_l",
    56: "shift_l",
    57: "caps_lock",
    58: "alt_l",
    59: "ctrl_l",
    60: "shift_r",
    61: "alt_r",
    62: "ctrl_r",
    63: "fn",
    65: "decimal",
    67: "multiply",
    69: "add",
    71: "clear",
    75: "divide",
    76: "enter",
    78: "subtract",
    81: "equals",
    82: "num0",
    83: "num1",
    84: "num2",
    85: "num3",
    86: "num4",
    87: "num5",
    88: "num6",
    89: "num7",
    91: "num8",
    92: "num9",
    96: "f5",
    97: "f6",
    98: "f7",
    99: "f3",
    100: "f8",
    101: "f9",
    103: "f11",
    105: "f13",
    106: "f16",
    107: "f14",
    109: "f10",
    111: "f12",
    113: "f15",
    114: "insert",
    115: "home",
    116: "page_up",
    117: "delete",
    118: "f4",
    119: "end",
    120: "f2",
    121: "page_down",
    122: "f1",
    123: "left",
    124: "right",
    125: "down",
    126: "up",
}

_CANONICAL_KEY_NAMES = {
    "cmd_l": "cmd",
    "cmd_r": "cmd",
    "shift_l": "shift",
    "shift_r": "shift",
    "alt_l": "alt",
    "alt_r": "alt",
    "ctrl_l": "ctrl",
    "ctrl_r": "ctrl",
}

_MODIFIER_KEYCODES = frozenset({54, 55, 56, 57, 58, 59, 60, 61, 62, 63})


class DarwinInputObserver(ThreadedInputObserver):
    """Observe macOS keyboard and mouse input without modifying the event stream."""

    def __init__(
        self,
        callback: InputCallback,
        *,
        observe_keyboard: bool,
        observe_mouse: bool,
        capture_mouse_moves: bool,
        startup_timeout: float = 5.0,
        shutdown_timeout: float = 5.0,
        _quartz: Any | None = None,
        _application_services: Any | None = None,
    ) -> None:
        super().__init__(
            callback,
            observe_keyboard=observe_keyboard,
            observe_mouse=observe_mouse,
            capture_mouse_moves=capture_mouse_moves,
            startup_timeout=startup_timeout,
            shutdown_timeout=shutdown_timeout,
        )
        self._quartz = _quartz
        self._application_services = _application_services
        self._delivery_barrier_lock = threading.Lock()
        self._pending_delivery_barriers = 0
        self._event_tap: Any | None = None
        self._cf_run_loop: Any | None = None
        self._run_loop_source: Any | None = None
        self._tap_callback_ref: Any | None = None
        self._pressed_modifier_codes: set[int] = set()

    def _load_frameworks(self) -> None:
        if self._quartz is not None and self._application_services is not None:
            return
        try:
            import ApplicationServices
            import Quartz
        except ImportError as exc:
            raise InputObserverUnavailableError(
                "macOS input observation requires the permissively licensed "
                "PyObjC Quartz and ApplicationServices frameworks"
            ) from exc
        self._quartz = Quartz
        self._application_services = ApplicationServices

    def _setup(self) -> None:
        self._load_frameworks()
        quartz = self._quartz

        if not self._has_observation_permission():
            request_permission = getattr(
                quartz,
                "CGRequestListenEventAccess",
                None,
            )
            if callable(request_permission):
                request_permission()
            if not self._has_observation_permission():
                raise InputObserverPermissionError(
                    "macOS denied global input observation. Enable Input Monitoring "
                    "for OpenAdapt in System Settings > Privacy & Security."
                )
        if not self._has_active_filter_permission():
            raise InputObserverPermissionError(
                "macOS denied the ordered input barrier. Enable Accessibility "
                "for OpenAdapt in System Settings > Privacy & Security."
            )

        event_mask = 0
        for event_type in self._observed_event_types():
            event_mask |= quartz.CGEventMaskBit(event_type)

        self._tap_callback_ref = self._event_callback
        self._event_tap = quartz.CGEventTapCreate(
            quartz.kCGSessionEventTap,
            quartz.kCGHeadInsertEventTap,
            quartz.kCGEventTapOptionDefault,
            event_mask,
            self._tap_callback_ref,
            None,
        )
        if self._event_tap is None:
            if not self._has_observation_permission():
                raise InputObserverPermissionError(
                    "macOS Input Monitoring permission was revoked while "
                    "OpenAdapt was starting"
                )
            raise InputObserverUnavailableError(
                "macOS could not create an active Quartz event barrier"
            )

        self._cf_run_loop = quartz.CFRunLoopGetCurrent()
        self._run_loop_source = quartz.CFMachPortCreateRunLoopSource(
            None,
            self._event_tap,
            0,
        )
        if self._run_loop_source is None:
            raise InputObserverUnavailableError(
                "macOS could not create a run-loop source for the input observer"
            )
        quartz.CFRunLoopAddSource(
            self._cf_run_loop,
            self._run_loop_source,
            quartz.kCFRunLoopDefaultMode,
        )
        quartz.CGEventTapEnable(self._event_tap, True)

    def _has_observation_permission(self) -> bool:
        preflight = getattr(
            self._quartz,
            "CGPreflightListenEventAccess",
            None,
        )
        if callable(preflight):
            return bool(preflight())

        # CGPreflightListenEventAccess was introduced after AX event taps.
        # On older supported macOS versions, Accessibility trust is the
        # fail-loud permission oracle available through ApplicationServices.
        accessibility_check = getattr(
            self._application_services,
            "AXIsProcessTrusted",
            None,
        )
        if callable(accessibility_check):
            return bool(accessibility_check())
        raise InputObserverUnavailableError(
            "macOS does not expose an input-observation permission API"
        )

    def _has_active_filter_permission(self) -> bool:
        accessibility_check = getattr(
            self._application_services,
            "AXIsProcessTrusted",
            None,
        )
        if callable(accessibility_check):
            return bool(accessibility_check())
        raise InputObserverUnavailableError(
            "macOS does not expose the Accessibility permission required for "
            "ordered input"
        )

    def _observed_event_types(self) -> tuple[int, ...]:
        quartz = self._quartz
        event_types: list[int] = []
        if self.observe_keyboard:
            event_types.extend(
                (
                    quartz.kCGEventKeyDown,
                    quartz.kCGEventKeyUp,
                    quartz.kCGEventFlagsChanged,
                )
            )
        if self.observe_mouse:
            event_types.extend(
                (
                    quartz.kCGEventLeftMouseDown,
                    quartz.kCGEventLeftMouseUp,
                    quartz.kCGEventRightMouseDown,
                    quartz.kCGEventRightMouseUp,
                    quartz.kCGEventOtherMouseDown,
                    quartz.kCGEventOtherMouseUp,
                    quartz.kCGEventScrollWheel,
                )
            )
            if self.capture_mouse_moves:
                event_types.extend(
                    (
                        quartz.kCGEventMouseMoved,
                        quartz.kCGEventLeftMouseDragged,
                        quartz.kCGEventRightMouseDragged,
                        quartz.kCGEventOtherMouseDragged,
                    )
                )
        return tuple(event_types)

    def _run_loop(self) -> None:
        quartz = self._quartz
        while not self._stop_requested.is_set():
            quartz.CFRunLoopRunInMode(
                quartz.kCFRunLoopDefaultMode,
                0.1,
                False,
            )
            self._complete_delivery_barriers()
        self._complete_delivery_barriers()

    def _defer_delivery_barrier(self) -> None:
        """Keep the callback active until control returns from the CF run loop."""
        with self._delivery_barrier_lock:
            self._pending_delivery_barriers += 1

    def _complete_delivery_barriers(self) -> None:
        """Release callbacks only after Quartz has accepted their returned events."""
        with self._delivery_barrier_lock:
            pending = self._pending_delivery_barriers
            self._pending_delivery_barriers = 0
        for _ in range(pending):
            self._end_native_callback()

    def _wake(self) -> None:
        if self._cf_run_loop is not None and self._quartz is not None:
            self._quartz.CFRunLoopStop(self._cf_run_loop)

    def _teardown(self) -> None:
        quartz = self._quartz
        if quartz is None:
            return
        if self._event_tap is not None:
            quartz.CGEventTapEnable(self._event_tap, False)
        if self._cf_run_loop is not None and self._run_loop_source is not None:
            quartz.CFRunLoopRemoveSource(
                self._cf_run_loop,
                self._run_loop_source,
                quartz.kCFRunLoopDefaultMode,
            )
        if self._run_loop_source is not None:
            quartz.CFRunLoopSourceInvalidate(self._run_loop_source)
        if self._event_tap is not None:
            quartz.CFMachPortInvalidate(self._event_tap)
        # PyObjC owns the Core Foundation references returned above. Dropping
        # the Python references after invalidation releases them safely; an
        # extra CFRelease here would risk over-releasing PyObjC-owned objects.
        self._run_loop_source = None
        self._cf_run_loop = None
        self._event_tap = None
        self._tap_callback_ref = None
        self._pressed_modifier_codes.clear()

    def _event_callback(
        self,
        _proxy: Any,
        event_type: int,
        event: Any,
        _refcon: Any,
    ) -> Any:
        quartz = self._quartz
        if event_type in (
            quartz.kCGEventTapDisabledByTimeout,
            quartz.kCGEventTapDisabledByUserInput,
        ):
            if self._event_tap is not None:
                quartz.CGEventTapEnable(self._event_tap, True)
            self._fail(
                InputObserverError(
                    "macOS disabled the input event tap; recording coverage is "
                    "incomplete"
                )
            )
            return event

        callback_entered = False
        try:
            if event_type in self._observed_event_types():
                self._begin_native_callback()
                callback_entered = True
            self._handle_event(event_type, event, timestamp=time.time())
        except BaseException as exc:
            failure = (
                exc
                if isinstance(exc, InputObserverError)
                else InputObserverError(f"macOS input callback failed: {exc}")
            )
            self._fail(failure)
        finally:
            if callback_entered:
                try:
                    # This callback is an active filter at the head of the
                    # session event stream. Quartz cannot deliver the returned
                    # event while Python is still inside this function. Keep
                    # the frame barrier active until CFRunLoopRunInMode returns.
                    self._defer_delivery_barrier()
                except BaseException as exc:
                    failure = (
                        exc
                        if isinstance(exc, InputObserverError)
                        else InputObserverError(
                            f"macOS input callback completion failed: {exc}"
                        )
                    )
                    self._fail(failure)
        return event

    def _handle_event(
        self,
        event_type: int,
        event: Any,
        *,
        timestamp: float | None = None,
    ) -> None:
        quartz = self._quartz
        if event_type == quartz.kCGEventFlagsChanged and self.observe_keyboard:
            keycode = self._keycode(event)
            if keycode not in _MODIFIER_KEYCODES:
                # Quartz can emit flagsChanged with keycode 0 when it cannot
                # attribute the flag transition to one physical modifier.
                # The event must invalidate an in-flight frame, but it cannot
                # become an honest keyboard action.
                self._mark_native_activity()
                return
        injected = self._is_injected(event)
        # Production callbacks always pass the receipt time. Keeping ``None``
        # for direct normalization calls makes the pure helper independently
        # testable without inventing a second observation moment.
        observed_at = timestamp
        observed_type = event_type in self._observed_event_types()
        if injected and observed_type:
            self._mark_native_activity()
            receipt = None
        elif observed_type:
            receipt = self._reserve_receipt(observed_at)
        else:
            receipt = None

        if event_type in self._mouse_move_event_types():
            if self.observe_mouse and self.capture_mouse_moves:
                x, y = self._location(event)
                self._emit(
                    ObservedMouseMove(
                        x=x,
                        y=y,
                        injected=injected,
                        timestamp=observed_at,
                    ),
                    receipt=receipt,
                )
            return

        button_event = self._mouse_button_event(
            event_type,
            event,
            injected,
            timestamp=observed_at,
        )
        if button_event is not None:
            self._emit(button_event, receipt=receipt)
            return

        if event_type == quartz.kCGEventScrollWheel and self.observe_mouse:
            x, y = self._location(event)
            self._emit(
                ObservedMouseScroll(
                    x=x,
                    y=y,
                    dx=float(
                        quartz.CGEventGetIntegerValueField(
                            event,
                            quartz.kCGScrollWheelEventDeltaAxis2,
                        )
                    ),
                    dy=float(
                        quartz.CGEventGetIntegerValueField(
                            event,
                            quartz.kCGScrollWheelEventDeltaAxis1,
                        )
                    ),
                    injected=injected,
                    timestamp=observed_at,
                ),
                receipt=receipt,
            )
            return

        if event_type in (quartz.kCGEventKeyDown, quartz.kCGEventKeyUp):
            if self.observe_keyboard:
                self._emit(
                    self._key_event(
                        event,
                        pressed=event_type == quartz.kCGEventKeyDown,
                        injected=injected,
                        timestamp=observed_at,
                    ),
                    receipt=receipt,
                )
            return

        if event_type == quartz.kCGEventFlagsChanged and self.observe_keyboard:
            keycode = self._keycode(event)
            pressed = self._modifier_pressed(event, keycode)
            if pressed:
                self._pressed_modifier_codes.add(keycode)
            else:
                self._pressed_modifier_codes.discard(keycode)
            self._emit(
                self._key_event(
                    event,
                    pressed=pressed,
                    injected=injected,
                    include_character=False,
                    timestamp=observed_at,
                ),
                receipt=receipt,
            )

    def _modifier_pressed(self, event: Any, keycode: int) -> bool:
        quartz = self._quartz
        modifier_masks = {
            54: quartz.kCGEventFlagMaskCommand,
            55: quartz.kCGEventFlagMaskCommand,
            56: quartz.kCGEventFlagMaskShift,
            57: quartz.kCGEventFlagMaskAlphaShift,
            58: quartz.kCGEventFlagMaskAlternate,
            59: quartz.kCGEventFlagMaskControl,
            60: quartz.kCGEventFlagMaskShift,
            61: quartz.kCGEventFlagMaskAlternate,
            62: quartz.kCGEventFlagMaskControl,
            63: quartz.kCGEventFlagMaskSecondaryFn,
        }
        mask = modifier_masks.get(keycode)
        if mask is None:
            raise InputObserverError(
                f"macOS reported flags-changed for unknown keycode {keycode}"
            )

        flags = int(quartz.CGEventGetFlags(event))
        flag_is_active = bool(flags & mask)
        if keycode in self._pressed_modifier_codes:
            # This physical key was previously pressed. It is now released
            # even when a sibling key sharing the same aggregate flag remains
            # down (for example left Shift released while right Shift is held).
            return False
        # Deriving the initial observation from the OS flag makes a first
        # observed release after startup a release rather than a toggle drift.
        return flag_is_active

    def _mouse_move_event_types(self) -> tuple[int, ...]:
        quartz = self._quartz
        return (
            quartz.kCGEventMouseMoved,
            quartz.kCGEventLeftMouseDragged,
            quartz.kCGEventRightMouseDragged,
            quartz.kCGEventOtherMouseDragged,
        )

    def _mouse_button_event(
        self,
        event_type: int,
        event: Any,
        injected: bool,
        *,
        timestamp: float | None,
    ) -> ObservedMouseButton | None:
        if not self.observe_mouse:
            return None
        quartz = self._quartz
        transitions = {
            quartz.kCGEventLeftMouseDown: ("left", True),
            quartz.kCGEventLeftMouseUp: ("left", False),
            quartz.kCGEventRightMouseDown: ("right", True),
            quartz.kCGEventRightMouseUp: ("right", False),
            quartz.kCGEventOtherMouseDown: (None, True),
            quartz.kCGEventOtherMouseUp: (None, False),
        }
        transition = transitions.get(event_type)
        if transition is None:
            return None
        button, pressed = transition
        if button is None:
            number = int(
                quartz.CGEventGetIntegerValueField(
                    event,
                    quartz.kCGMouseEventButtonNumber,
                )
            )
            button = "middle" if number == 2 else f"button{number + 1}"
        x, y = self._location(event)
        return ObservedMouseButton(
            x=x,
            y=y,
            button=button,
            pressed=pressed,
            injected=injected,
            timestamp=timestamp,
        )

    def _key_event(
        self,
        event: Any,
        *,
        pressed: bool,
        injected: bool,
        include_character: bool = True,
        timestamp: float | None,
    ) -> ObservedKey:
        keycode = self._keycode(event)
        key_name = _KEY_NAMES.get(keycode)
        key_char = (
            self._keyboard_character(event)
            if include_character and key_name is None
            else None
        )
        canonical_name = _CANONICAL_KEY_NAMES.get(key_name, key_name)
        canonical_char = key_char.lower() if key_char is not None else None
        vk = str(keycode)
        return ObservedKey(
            pressed=pressed,
            key_name=key_name,
            key_char=key_char,
            key_vk=vk,
            canonical_key_name=canonical_name,
            canonical_key_char=canonical_char,
            canonical_key_vk=vk,
            injected=injected,
            timestamp=timestamp,
        )

    def _keyboard_character(self, event: Any) -> str | None:
        length, text = self._quartz.CGEventKeyboardGetUnicodeString(
            event,
            8,
            None,
            None,
        )
        if length <= 0 or not text:
            return None
        return str(text)[: int(length)]

    def _keycode(self, event: Any) -> int:
        return int(
            self._quartz.CGEventGetIntegerValueField(
                event,
                self._quartz.kCGKeyboardEventKeycode,
            )
        )

    def _location(self, event: Any) -> tuple[float, float]:
        point = self._quartz.CGEventGetLocation(event)
        return float(point.x), float(point.y)

    def _is_injected(self, event: Any) -> bool:
        quartz = self._quartz
        source_pid_field = getattr(quartz, "kCGEventSourceUnixProcessID", None)
        if source_pid_field is None:
            return False
        source_pid = int(
            quartz.CGEventGetIntegerValueField(event, source_pid_field)
        )
        return source_pid > 0

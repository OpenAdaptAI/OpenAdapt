"""Factory for the supported native input observers."""

from __future__ import annotations

import sys

from .base import InputCallback, InputObserver, InputObserverUnavailableError


def create_input_observer(
    callback: InputCallback,
    *,
    observe_keyboard: bool = True,
    observe_mouse: bool = True,
    capture_mouse_moves: bool = True,
    platform_name: str | None = None,
) -> InputObserver:
    """Create the complete native observer for the current desktop platform."""
    selected = platform_name or sys.platform
    kwargs = {
        "callback": callback,
        "observe_keyboard": observe_keyboard,
        "observe_mouse": observe_mouse,
        "capture_mouse_moves": capture_mouse_moves,
    }
    if selected == "darwin":
        from .darwin import DarwinInputObserver

        return DarwinInputObserver(**kwargs)
    if selected == "win32":
        from .windows import WindowsInputObserver

        return WindowsInputObserver(**kwargs)
    if selected.startswith("linux"):
        if sys.platform.startswith("linux"):
            from openadapt_capture.x11_threads import ensure_xlib_thread_support

            ensure_xlib_thread_support()
        from .linux import LinuxXInputObserver

        return LinuxXInputObserver(**kwargs)
    raise InputObserverUnavailableError(
        f"native input observation is not implemented for platform {selected!r}"
    )

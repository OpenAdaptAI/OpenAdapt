"""Process-wide Xlib threading initialization for Linux capture paths."""

from __future__ import annotations

import ctypes
import ctypes.util
import sys
import threading


class X11ThreadInitializationError(RuntimeError):
    """Raised when Xlib cannot be made safe for concurrent capture threads."""


_initialization_lock = threading.Lock()
_initialized = False


def ensure_xlib_thread_support() -> None:
    """Call ``XInitThreads`` before this process uses any Xlib-backed capture.

    Both MSS and the native Linux input observer use Xlib from different
    threads. Xlib requires ``XInitThreads`` before the first other Xlib call;
    continuing after a missing or rejected initialization could corrupt
    concurrent display access, so Linux callers fail loudly.
    """

    if not sys.platform.startswith("linux"):
        return

    global _initialized
    with _initialization_lock:
        if _initialized:
            return

        x11_name = ctypes.util.find_library("X11")
        if not x11_name:
            raise X11ThreadInitializationError(
                "Linux capture requires libX11 with XInitThreads support"
            )
        try:
            x11 = ctypes.CDLL(x11_name)
            xinit_threads = x11.XInitThreads
            xinit_threads.argtypes = []
            xinit_threads.restype = ctypes.c_int
            initialized = int(xinit_threads())
        except (AttributeError, OSError) as exc:
            raise X11ThreadInitializationError(
                f"could not initialize Xlib threading through {x11_name!r}: {exc}"
            ) from exc

        if initialized == 0:
            raise X11ThreadInitializationError(
                "XInitThreads rejected process-wide Xlib thread initialization"
            )
        _initialized = True

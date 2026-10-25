"""Which window receives native input: keyboard focus and topmost-at-point.

Window-scoped recording stores input only when it went to the recorded
target window. Bounds alone cannot show that: a user can switch to another
application and type, or click a window that overlaps the target's
rectangle. These probes report, for one instant, which window or process
has keyboard focus and which top-level window is topmost at a screen point.

Each probe returns a :class:`FocusOwner` or ``None`` when the answer cannot
be determined. Callers treat ``None`` and any exception as "not on the
target" (fail closed).

Platform bindings are imported lazily inside each probe so that importing
this module never touches a display (tests/test_headless_import.py).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Iterator, Protocol


@dataclass(frozen=True)
class FocusOwner:
    """A window and/or process observed as an input receiver.

    ``window_id`` uses the same identifier space as
    :attr:`openadapt_capture.window_capture.TargetWindow.window_id` on the
    same platform (CGWindowID, top-level HWND, or X11 client window).
    """

    window_id: int | None = None
    pid: int | None = None


class InputFocusProbe(Protocol):
    """Injectable seam that reports which window receives native input."""

    def keyboard_focus(self, *, at_receipt: bool = False) -> FocusOwner | None:
        """Return the keyboard-focus owner, or ``None`` when unknown.

        ``at_receipt`` is set when the call runs inside a native input hook.
        Implementations must then answer from a cheap local read.
        """
        ...

    def window_at(self, x: float, y: float) -> FocusOwner | None:
        """Return the topmost top-level window at a global screen point."""
        ...


def _point_in_bounds(x: float, y: float, bounds: tuple[float, float, float, float]) -> bool:
    left, top, width, height = bounds
    return left <= x < left + width and top <= y < top + height


class MacOSInputFocusProbe:
    """macOS: frontmost application and front-to-back Quartz window list.

    Bounds come from ``kCGWindowBounds``: global screen points with a
    top-left origin, the same space the macOS resolver binds.
    """

    @staticmethod
    def _frontmost_pid() -> int | None:
        from AppKit import NSWorkspace

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return None
        pid = int(app.processIdentifier())
        return pid if pid > 0 else None

    @staticmethod
    def _onscreen_app_windows() -> Iterator[dict]:
        """Yield on-screen layer-0 windows front to back, lazily."""
        import Quartz

        options = (
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
        )
        windows = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID)
        for info in windows or []:
            if int(info.get("kCGWindowLayer", 0) or 0) != 0:
                continue  # menu bar, status items, overlays
            if float(info.get("kCGWindowAlpha", 1.0) or 0.0) <= 0.0:
                continue  # invisible windows do not receive clicks
            b = info.get("kCGWindowBounds", {}) or {}
            bounds = (
                float(b.get("X", 0.0)),
                float(b.get("Y", 0.0)),
                float(b.get("Width", 0.0)),
                float(b.get("Height", 0.0)),
            )
            if bounds[2] <= 0 or bounds[3] <= 0:
                continue
            yield {
                "window_id": int(info.get("kCGWindowNumber", 0) or 0),
                "pid": int(info.get("kCGWindowOwnerPID", 0) or 0),
                "bounds": bounds,
            }

    def keyboard_focus(self, *, at_receipt: bool = False) -> FocusOwner | None:
        """Return the frontmost application's process.

        Inside the event tap only the cheap ``NSWorkspace`` read runs. At
        delivery the answer must also agree with the owner of the front-most
        on-screen application window. ``NSWorkspace`` is updated through the
        main run loop and can be stale in a process that does not run one, so
        a disagreement is reported as unknown.
        """
        pid = self._frontmost_pid()
        if pid is None or at_receipt:
            return None if pid is None else FocusOwner(pid=pid)
        front = next(self._onscreen_app_windows(), None)
        if front is None or front["pid"] != pid:
            return None
        return FocusOwner(pid=pid)

    def window_at(self, x: float, y: float) -> FocusOwner | None:
        """Return the first front-to-back layer-0 window containing the point."""
        for info in self._onscreen_app_windows():
            if _point_in_bounds(x, y, info["bounds"]):
                return FocusOwner(window_id=info["window_id"], pid=info["pid"])
        return None


class WindowsInputFocusProbe:
    """Windows: foreground window and ``WindowFromPoint`` root ancestor."""

    _GA_ROOT = 2

    @staticmethod
    def _user32():
        import ctypes
        import ctypes.wintypes as wintypes

        # A private handle: setting prototypes here must not change the
        # shared ``ctypes.windll.user32`` the resolver uses.
        user32 = ctypes.WinDLL("user32")
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetForegroundWindow.argtypes = []
        user32.WindowFromPoint.restype = wintypes.HWND
        user32.WindowFromPoint.argtypes = [wintypes.POINT]
        user32.GetAncestor.restype = wintypes.HWND
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        return user32

    def _owner(self, user32, hwnd) -> FocusOwner | None:
        import ctypes
        import ctypes.wintypes as wintypes

        if not hwnd:
            return None
        root = user32.GetAncestor(hwnd, self._GA_ROOT) or hwnd
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(root, ctypes.byref(pid))
        return FocusOwner(window_id=int(root), pid=int(pid.value) or None)

    def keyboard_focus(self, *, at_receipt: bool = False) -> FocusOwner | None:
        """Return the foreground top-level window and its process."""
        del at_receipt  # GetForegroundWindow is already a cheap local read
        user32 = self._user32()
        return self._owner(user32, user32.GetForegroundWindow())

    def window_at(self, x: float, y: float) -> FocusOwner | None:
        """Return the root ancestor of the window under the physical point."""
        import ctypes.wintypes as wintypes

        user32 = self._user32()
        return self._owner(user32, user32.WindowFromPoint(wintypes.POINT(int(x), int(y))))


class X11InputFocusProbe:
    """X11: ``_NET_ACTIVE_WINDOW`` and the topmost viewable root child."""

    def __init__(self, client_factory=None) -> None:
        self._client_factory = client_factory

    def _client(self):
        from openadapt_capture.window_capture_linux import X11CompositeClient

        return (self._client_factory or X11CompositeClient)()

    def keyboard_focus(self, *, at_receipt: bool = False) -> FocusOwner | None:
        """Return the EWMH active client window and its ``_NET_WM_PID``."""
        del at_receipt
        with self._client() as client:
            window_id = client.active_window()
            if not window_id:
                return None
            return FocusOwner(window_id=int(window_id), pid=client.window_pid(window_id))

    def window_at(self, x: float, y: float) -> FocusOwner | None:
        """Return the client window inside the topmost root child at the point."""
        with self._client() as client:
            window_id = client.client_window_at(int(x), int(y))
            if not window_id:
                return None
            return FocusOwner(window_id=int(window_id), pid=client.window_pid(window_id))


class UnavailableInputFocusProbe:
    """A platform without a focus probe: every answer is unknown."""

    def keyboard_focus(self, *, at_receipt: bool = False) -> FocusOwner | None:
        """Report unknown so window-scoped input fails closed."""
        del at_receipt
        return None

    def window_at(self, x: float, y: float) -> FocusOwner | None:
        """Report unknown so window-scoped input fails closed."""
        del x, y
        return None


def platform_input_focus_probe() -> InputFocusProbe:
    """Return this platform's probe; constructing it touches no display."""
    if sys.platform == "darwin":
        return MacOSInputFocusProbe()
    if sys.platform == "win32":
        return WindowsInputFocusProbe()
    if sys.platform.startswith("linux"):
        return X11InputFocusProbe()
    return UnavailableInputFocusProbe()


__all__ = [
    "FocusOwner",
    "InputFocusProbe",
    "MacOSInputFocusProbe",
    "UnavailableInputFocusProbe",
    "WindowsInputFocusProbe",
    "X11InputFocusProbe",
    "platform_input_focus_probe",
]

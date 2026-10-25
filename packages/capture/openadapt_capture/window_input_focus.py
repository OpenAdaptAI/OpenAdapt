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
    A probe that can name the focused window must set it: the scope then
    requires that exact window, and a matching ``pid`` alone is not enough.
    ``dialog`` is set when focus is in a dialog or sheet, which never counts
    as the target even when the target's own process owns it.
    """

    window_id: int | None = None
    pid: int | None = None
    dialog: bool = False


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


# Quartz window levels: kCGNormalWindowLevel and kCGStatusWindowLevel.
# A window between them is raised above document windows: a floating panel
# (3), a modal panel such as an NSAlert or password prompt (8), a torn-off
# menu (3). The status level and above are the menu bar, status items and
# system overlays, which never take an app's keyboard focus.
_MACOS_NORMAL_LAYER = 0
_MACOS_STATUS_LAYER = 25
# Accessibility subroles of a focused window that is a dialog, not a
# document window. Typing there never counts as typing into the target.
_MACOS_DIALOG_SUBROLES = frozenset({"AXDialog", "AXSystemDialog"})
_MACOS_AX_FOCUS_TIMEOUT_SECONDS = 0.25
_MACOS_AX_MAX_ANCESTORS = 32


def _macos_ax_window_number(element: object) -> int | None:
    """Return the CGWindowID behind an AX window element, or ``None``.

    Uses the long-standing HIServices ``_AXUIElementGetWindow`` call. There
    is no public API for this mapping; any failure is reported as unknown.
    """
    try:
        import ctypes

        import objc

        library = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        get_window = library._AXUIElementGetWindow
        get_window.restype = ctypes.c_int32
        get_window.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        number = ctypes.c_uint32(0)
        if get_window(objc.pyobjc_id(element), ctypes.byref(number)) != 0:
            return None
        return int(number.value) or None
    except Exception:
        return None


class MacOSInputFocusProbe:
    """macOS: frontmost application and front-to-back Quartz window list.

    Bounds come from ``kCGWindowBounds``: global screen points with a
    top-left origin, the same space the macOS resolver binds.

    The focused window is the front-most on-screen layer-0 window of the
    frontmost application. A raised window of that application (a modal or
    floating panel) in front of it takes keyboard focus, so it is reported
    as a dialog. When Accessibility can name the application's
    ``AXFocusedWindow``, it must agree with that window, and a focused dialog
    or sheet is reported as such.
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
        """Yield on-screen app windows front to back, lazily.

        These are document windows (layer 0) and the raised windows above
        them up to, not including, the status level: floating panels and
        modal panels. Each item carries its ``layer``.
        """
        import Quartz

        options = (
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
        )
        windows = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID)
        for info in windows or []:
            layer = int(info.get("kCGWindowLayer", 0) or 0)
            if not _MACOS_NORMAL_LAYER <= layer < _MACOS_STATUS_LAYER:
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
                "layer": layer,
            }

    @staticmethod
    def _ax_focused_window(pid: int) -> tuple[int | None, bool] | None:
        """Return ``(window number, is dialog)`` for the app's focused window.

        ``None`` when Accessibility cannot name a focused window (not
        trusted, timed out, or the app exposes none).
        """
        try:
            import ApplicationServices as ax

            success = ax.kAXErrorSuccess

            def attribute(element: object, name: str) -> object | None:
                error, value = ax.AXUIElementCopyAttributeValue(element, name, None)
                return value if error == success else None

            application = ax.AXUIElementCreateApplication(pid)
            if (
                ax.AXUIElementSetMessagingTimeout(
                    application, _MACOS_AX_FOCUS_TIMEOUT_SECONDS
                )
                != success
            ):
                return None
            window = attribute(application, "AXFocusedWindow")
            if window is None or attribute(window, "AXRole") != "AXWindow":
                return None
            dialog = attribute(window, "AXSubrole") in _MACOS_DIALOG_SUBROLES
            # A sheet hangs off its parent window. Focus inside it still
            # names the parent as the focused window.
            element = attribute(application, "AXFocusedUIElement")
            for _ in range(_MACOS_AX_MAX_ANCESTORS):
                if element is None or dialog:
                    break
                if attribute(element, "AXRole") == "AXSheet":
                    dialog = True
                    break
                element = attribute(element, "AXParent")
            return _macos_ax_window_number(window), dialog
        except Exception:
            return None

    def keyboard_focus(self, *, at_receipt: bool = False) -> FocusOwner | None:
        """Return the focused window and its process.

        Inside the event tap only the cheap ``NSWorkspace`` read runs and the
        answer names the process alone. At delivery the front-most on-screen
        application window must belong to that process. ``NSWorkspace`` is
        updated through the main run loop and can be stale in a process that
        does not run one, so a disagreement is reported as unknown. That
        window is the focused window, unless one of the application's raised
        windows is in front of it (a dialog), or Accessibility names a
        different focused window (unknown) or a dialog or sheet.
        """
        pid = self._frontmost_pid()
        if pid is None or at_receipt:
            return None if pid is None else FocusOwner(pid=pid)
        raised: dict | None = None
        front: dict | None = None
        for info in self._onscreen_app_windows():
            if info["layer"] != _MACOS_NORMAL_LAYER:
                if info["pid"] == pid and raised is None:
                    raised = info
                continue
            front = info
            break
        if front is None or front["pid"] != pid:
            return None
        if raised is not None:
            # A modal or floating panel of the app sits over its document
            # windows. Keys typed while it is up go to the panel.
            return FocusOwner(window_id=raised["window_id"] or None, pid=pid, dialog=True)
        window_id = front["window_id"] or None
        focused = self._ax_focused_window(pid)
        if focused is not None:
            ax_window_id, dialog = focused
            if dialog:
                return FocusOwner(window_id=ax_window_id or window_id, pid=pid, dialog=True)
            if ax_window_id is not None and ax_window_id != window_id:
                return None
        return FocusOwner(window_id=window_id, pid=pid)

    def window_at(self, x: float, y: float) -> FocusOwner | None:
        """Return the first front-to-back app window containing the point.

        A raised panel over the target (a modal password prompt) is returned
        as itself, so a click on it never counts as a click on the target.
        """
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

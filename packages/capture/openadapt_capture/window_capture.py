"""Window-scoped capture: record ONE window in that window's own pixel space.

Why this exists (the Citrix / remote-display wedge): openadapt-flow's
``RemoteDisplayBackend`` (``record --backend rdp``, ``rdp_window`` mode)
*replays* against the pixels of a single client window (Parallels, Citrix
Workspace, Microsoft Remote Desktop) captured per-window — on macOS via
``CGWindowListCreateImage`` by window id. A demonstration recorded FULL-SCREEN
is in a different coordinate space than that replay surface, so converters had
to work around the mismatch (record inside the session, or full-screen the
client). Window-scoped recording removes the mismatch at the source: frames
are captured in the target window's pixel space and input coordinates are
translated into that same space at capture time.

Coordinate semantics (kept in exact parity with openadapt-flow
``backends/remote_display.py`` — read that module before changing these):

- ``bounds`` is ``(x, y, w, h)`` in **screen points**, top-left origin — the
  space native platform observers use for global mouse coordinates.
- A captured frame contains the window's own **pixels** (macOS:
  ``CGWindowListCreateImage`` with ``kCGWindowImageBoundsIgnoreFraming`` — the
  identical call flow's replay capture path uses).
- ``scale`` = captured pixel width / bounds width (e.g. 2.0 on Retina).
- Replay maps a captured pixel to a screen point as
  ``screen = bounds_origin + pixel / scale`` (flow's ``_to_screen``).
  Recording therefore maps a global screen point to a window pixel as the
  exact inverse: ``pixel = (screen - bounds_origin) * scale``
  (:func:`translate_point`).

Importing this module must never touch the display (platform bindings are
imported lazily inside the resolver/capturer functions), preserving the
package's headless-import invariant (tests/test_headless_import.py).
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

from loguru import logger

if TYPE_CHECKING:
    from PIL import Image


class WindowCaptureError(RuntimeError):
    """The target window could not be resolved or captured.

    Raised LOUDLY (never swallowed into an empty frame): a recording that
    silently fell back to full-screen would produce coordinates in the wrong
    space — a corrupt demonstration that *looks* valid.
    """


@dataclass(frozen=True)
class WindowTarget:
    """How to find the window to record: case-insensitive substrings.

    Matches openadapt-flow ``RemoteDisplayBackend``'s window identification
    (owner substring, optional title substring to disambiguate).
    """

    owner: str | None = None
    title: str | None = None

    def __post_init__(self) -> None:
        """Validate that at least one selector is provided."""
        if not (self.owner or self.title):
            raise ValueError(
                "WindowTarget requires an 'owner' and/or 'title' substring "
                "(e.g. WindowTarget(owner='Parallels'))"
            )

    @classmethod
    def from_spec(cls, spec: "WindowTarget | dict | None") -> "WindowTarget | None":
        """Build a target from the ``Recorder(window=...)`` spec.

        Accepts ``None`` (window mode off), an existing :class:`WindowTarget`,
        or a dict of the shape ``{"owner": "Parallels", "title": None}``.
        Unknown keys are rejected loudly rather than ignored.
        """
        if spec is None:
            return None
        if isinstance(spec, WindowTarget):
            return spec
        if isinstance(spec, dict):
            unknown = set(spec) - {"owner", "title"}
            if unknown:
                raise ValueError(
                    f"unknown window spec keys {sorted(unknown)}; expected {{'owner', 'title'}}"
                )
            return cls(owner=spec.get("owner"), title=spec.get("title"))
        raise TypeError(f"window spec must be a dict or WindowTarget, got {type(spec).__name__}")


@dataclass(frozen=True)
class TargetWindow:
    """One resolved on-screen window.

    ``bounds`` is ``(x, y, w, h)`` in screen points, top-left origin — the
    same space as native global mouse coordinates. Same field semantics as
    openadapt-flow's ``WindowInfo``.
    """

    window_id: int
    owner: str
    title: str
    pid: int
    bounds: tuple[float, float, float, float]
    on_screen: bool = True


def translate_point(
    x: float,
    y: float,
    bounds: tuple[float, float, float, float],
    scale: float,
) -> tuple[float, float]:
    """Map a global screen point to window-relative captured-pixel coordinates.

    Exact inverse of flow's replay mapping ``screen = origin + pixel / scale``:

        ``pixel = (screen - origin) * scale``

    Points outside the window bounds translate to coordinates outside
    ``[0, viewport)`` (possibly negative); they are recorded as-is so
    downstream consumers can detect out-of-window input instead of receiving
    silently clamped (wrong) coordinates.
    """
    ox, oy = bounds[0], bounds[1]
    return ((x - ox) * scale, (y - oy) * scale)


ResolverFn = Callable[[WindowTarget], Optional[TargetWindow]]
CapturerFn = Callable[[TargetWindow], "Image.Image"]


class WindowCaptureScope:
    """Thread-safe tracker of the target window's bounds/scale during recording.

    The screen-reader thread calls :meth:`capture_frame` (which re-resolves the
    window each frame — windows move and resize); native observer threads call
    :meth:`translate` concurrently to convert global input coordinates into
    the captured frame's pixel space using the freshest bounds.

    ``resolver`` / ``capturer`` default to the platform implementations and
    are injectable for display-free unit tests.
    """

    def __init__(
        self,
        target: WindowTarget,
        resolver: ResolverFn | None = None,
        capturer: CapturerFn | None = None,
    ) -> None:
        """Initialize the scope for ``target``."""
        self.target = target
        self._resolver = resolver or resolve_window
        self._capturer = capturer or capture_window
        self._lock = threading.Lock()
        self._window: TargetWindow | None = None
        self._scale: float | None = None
        self._scale_x: float | None = None
        self._scale_y: float | None = None
        self._viewport: tuple[int, int] | None = None
        self._source_viewport: tuple[int, int] | None = None
        self._content_rect: tuple[int, int, int, int] | None = None
        self._fit_scale: float | None = None
        self._bound_window_id: int | None = None
        # Window of the last CAPTURED frame (not merely resolved): the
        # bounds-timeline 'changed' flag compares frame to frame, so a bare
        # resolve() (e.g. a pre-flight existence check) never suppresses the
        # first frame's timeline entry.
        self._frame_window: TargetWindow | None = None

    def resolve(self) -> TargetWindow:
        """Resolve the target window without changing captured-frame geometry.

        Raises:
            WindowCaptureError: If no matching window is on screen.
        """
        win = self._resolver(self.target)
        if win is None:
            raise WindowCaptureError(
                f"no window matching owner {self.target.owner!r} "
                f"title {self.target.title!r}; is the target application "
                "running with a visible window?"
            )
        return win

    def capture_frame(self) -> tuple["Image.Image", bool]:
        """Capture the target window's current pixels.

        Re-resolves the window first so bounds/scale can never disagree with
        the frame just captured (mirrors flow's ``screenshot()`` contract).
        The first successful frame fixes the recording viewport. If the window
        later resizes, the complete new frame is scaled to fit that viewport
        and letterboxed. This preserves one encodable video stream without
        discarding resize frames or mixing coordinate spaces.

        Returns:
            (PIL.Image in the window's pixel space,
             changed: True when window identity/bounds/title differ from the
             previous frame — the caller should persist a bounds-timeline
             event).

        Raises:
            WindowCaptureError: If the window is gone or capture fails.
        """
        with self._lock:
            prev = self._frame_window
            bound_window_id = self._bound_window_id
        win = self.resolve()
        if bound_window_id is not None and win.window_id != bound_window_id:
            raise WindowCaptureError(
                "the resolved target changed window identity during recording: "
                f"expected {bound_window_id}, got {win.window_id}"
            )
        source_image = self._capturer(win)
        if source_image.width <= 0 or source_image.height <= 0:
            raise WindowCaptureError("window capture returned an empty frame")
        source_viewport = (source_image.width, source_image.height)
        output_viewport = self._viewport or source_viewport
        output_width, output_height = output_viewport
        fit_scale = min(
            output_width / source_image.width,
            output_height / source_image.height,
        )
        fitted_width = max(1, min(output_width, round(source_image.width * fit_scale)))
        fitted_height = max(1, min(output_height, round(source_image.height * fit_scale)))
        offset_x = (output_width - fitted_width) // 2
        offset_y = (output_height - fitted_height) // 2
        if source_viewport == output_viewport:
            image = source_image
        else:
            from PIL import Image

            resized = source_image.resize((fitted_width, fitted_height), Image.Resampling.LANCZOS)
            image = Image.new("RGB", output_viewport, color=(0, 0, 0))
            image.paste(resized, (offset_x, offset_y))
        bounds_w = win.bounds[2] or float(source_image.width)
        bounds_h = win.bounds[3] or float(source_image.height)
        scale_x = fitted_width / bounds_w
        scale_y = fitted_height / bounds_h
        with self._lock:
            self._window = win
            # ``scale`` is the historical scalar field. Keep it as the x-axis
            # value for old readers. Current readers can use both exact axes.
            # Integer resize rounding can make the axes differ slightly.
            self._scale = scale_x
            self._scale_x = scale_x
            self._scale_y = scale_y
            self._viewport = output_viewport
            self._source_viewport = source_viewport
            self._content_rect = (offset_x, offset_y, fitted_width, fitted_height)
            self._fit_scale = fit_scale
            self._bound_window_id = win.window_id
            self._frame_window = win
        changed = (
            prev is None
            or prev.window_id != win.window_id
            or prev.bounds != win.bounds
            or prev.title != win.title
        )
        return image, changed

    def translate(self, x: float, y: float) -> tuple[float, float]:
        """Translate a global screen point into window-relative pixels.

        Uses the bounds/scale of the most recently captured frame (the frame
        this input event will be associated with).

        Raises:
            WindowCaptureError: If called before the first
                :meth:`capture_frame` (no bounds are known yet, and guessing
                a coordinate space would be a silent wrong action).
        """
        with self._lock:
            window = self._window
            scale_x = self._scale_x
            scale_y = self._scale_y
            content_rect = self._content_rect
        if window is None or scale_x is None or scale_y is None or content_rect is None:
            raise WindowCaptureError(
                "translate() called before the first captured frame; "
                "capture_frame() must succeed before input can be scoped"
            )
        return (
            (x - window.bounds[0]) * scale_x + content_rect[0],
            (y - window.bounds[1]) * scale_y + content_rect[1],
        )

    def window_event_data(self) -> dict:
        """Bounds-timeline entry for the WindowEvent table.

        Keys match the WindowEvent columns written by the recorder pipeline
        (``title``/``left``/``top``/``width``/``height``/``window_id`` +
        ``state`` JSON). Integer columns truncate, so exact float bounds and
        the pixel scale live in ``state``.
        """
        with self._lock:
            window = self._window
            scale = self._scale
            scale_x = self._scale_x
            scale_y = self._scale_y
            viewport = self._viewport
            source_viewport = self._source_viewport
            content_rect = self._content_rect
            fit_scale = self._fit_scale
        if window is None:
            raise WindowCaptureError("no resolved window; call capture_frame() first")
        x, y, w, h = window.bounds
        return {
            "title": window.title,
            "left": int(x),
            "top": int(y),
            "width": int(w),
            "height": int(h),
            "window_id": str(window.window_id),
            "state": {
                "window_capture": True,
                "owner": window.owner,
                "pid": window.pid,
                "bounds": [x, y, w, h],
                "scale": scale,
                "scale_x": scale_x,
                "scale_y": scale_y,
                "viewport": list(viewport) if viewport else None,
                "source_viewport": (list(source_viewport) if source_viewport else None),
                "content_rect": list(content_rect) if content_rect else None,
                "fit_scale": fit_scale,
                "on_screen": window.on_screen,
            },
        }

    def snapshot(self) -> dict:
        """Window-scoping metadata persisted on the Recording (config JSON).

        Converters (e.g. openadapt-flow's capture adapter) read this to know
        the recording is already in window-pixel space (``coordinate_space``)
        and which window it was scoped to.
        """
        with self._lock:
            window = self._window
            scale = self._scale
            scale_x = self._scale_x
            scale_y = self._scale_y
            viewport = self._viewport
            source_viewport = self._source_viewport
            content_rect = self._content_rect
            fit_scale = self._fit_scale
        data: dict = {
            "target": {"owner": self.target.owner, "title": self.target.title},
            "coordinate_space": "window_pixels",
        }
        if window is not None:
            data.update(
                {
                    "window_id": window.window_id,
                    "owner": window.owner,
                    "title": window.title,
                    "pid": window.pid,
                    "initial_bounds": list(window.bounds),
                    "scale": scale,
                    "scale_x": scale_x,
                    "scale_y": scale_y,
                    "viewport": list(viewport) if viewport else None,
                    "source_viewport": (list(source_viewport) if source_viewport else None),
                    "content_rect": list(content_rect) if content_rect else None,
                    "fit_scale": fit_scale,
                }
            )
        return data


# ---------------------------------------------------------------------------
# Platform implementations (lazy imports: module import stays display-free)
# ---------------------------------------------------------------------------


def resolve_window(target: WindowTarget) -> TargetWindow | None:
    """Find the front-most/largest window matching ``target`` on this platform."""
    if sys.platform == "darwin":
        return _resolve_window_macos(target)
    if sys.platform == "win32":
        return _resolve_window_windows(target)
    raise WindowCaptureError(
        f"window-scoped capture is not supported on {sys.platform} (supported: darwin, win32)"
    )


def capture_window(window: TargetWindow) -> "Image.Image":
    """Capture ``window``'s pixels as a PIL image on this platform."""
    if sys.platform == "darwin":
        return _capture_window_macos(window)
    if sys.platform == "win32":
        return _capture_window_windows(window)
    raise WindowCaptureError(
        f"window-scoped capture is not supported on {sys.platform} (supported: darwin, win32)"
    )


def _resolve_window_macos(target: WindowTarget) -> TargetWindow | None:
    """macOS: CGWindowList by owner/title substring.

    Same selection semantics as flow's ``MacWindowClient.find_window``:
    ``kCGWindowListOptionAll`` (a momentarily hidden client is still
    resolvable/capturable), layer 0 only, case-insensitive substring match,
    largest window wins.
    """
    import Quartz

    owner_l = target.owner.lower() if target.owner else None
    title_l = target.title.lower() if target.title else None
    wins = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID)
    best: TargetWindow | None = None
    best_area = -1.0
    for w in wins or []:
        owner = str(w.get("kCGWindowOwnerName", "") or "")
        name = str(w.get("kCGWindowName", "") or "")
        if owner_l is not None and owner_l not in owner.lower():
            continue
        if title_l is not None and title_l not in name.lower():
            continue
        if int(w.get("kCGWindowLayer", 0) or 0) != 0:
            continue  # skip menubar/overlay layers; the app window is layer 0
        b = w.get("kCGWindowBounds", {}) or {}
        bounds = (
            float(b.get("X", 0.0)),
            float(b.get("Y", 0.0)),
            float(b.get("Width", 0.0)),
            float(b.get("Height", 0.0)),
        )
        area = bounds[2] * bounds[3]
        if area > best_area:
            best_area = area
            best = TargetWindow(
                window_id=int(w.get("kCGWindowNumber", 0) or 0),
                owner=owner,
                title=name,
                pid=int(w.get("kCGWindowOwnerPID", 0) or 0),
                bounds=bounds,
                on_screen=bool(w.get("kCGWindowIsOnscreen", False)),
            )
    return best


def _capture_window_macos(window: TargetWindow) -> "Image.Image":
    """macOS: per-window capture via ``CGWindowListCreateImage``.

    The identical call (``CGRectNull`` + ``kCGWindowListOptionIncludingWindow``
    + ``kCGWindowImageBoundsIgnoreFraming``) and BGRA->RGB conversion as flow's
    ``MacWindowClient.capture`` — the replay surface — so recorded frames and
    replay frames share coordinate semantics byte for byte.
    """
    import Quartz
    from PIL import Image

    img_ref = Quartz.CGWindowListCreateImage(
        Quartz.CGRectNull,
        Quartz.kCGWindowListOptionIncludingWindow,
        window.window_id,
        Quartz.kCGWindowImageBoundsIgnoreFraming,
    )
    if img_ref is None:
        raise WindowCaptureError(
            f"CGWindowListCreateImage returned None for window "
            f"{window.window_id} ({window.owner!r}); if this recurs, check "
            "Screen Recording permission for the recording process"
        )
    w = int(Quartz.CGImageGetWidth(img_ref))
    h = int(Quartz.CGImageGetHeight(img_ref))
    if w <= 0 or h <= 0:
        raise WindowCaptureError("captured window image has zero size")
    bpr = int(Quartz.CGImageGetBytesPerRow(img_ref))
    provider = Quartz.CGImageGetDataProvider(img_ref)
    data = Quartz.CGDataProviderCopyData(provider)
    buf = bytes(data)
    # CGImage from the window server is BGRA, premultiplied; read with the row
    # stride and drop alpha for a stable RGB frame.
    img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", bpr, 1)
    return img.convert("RGB")


def _resolve_window_windows(target: WindowTarget) -> TargetWindow | None:
    """Windows: enumerate top-level windows via Win32 (ctypes, no extra deps).

    ``owner`` matches the owning process's executable name (e.g. ``prl_client``
    for Parallels Client, ``wfica32`` for Citrix Workspace); ``title`` matches
    the window title. Both case-insensitive substrings; the largest visible
    match wins (parity with the macOS resolver). Bounds prefer the DWM
    extended frame (excludes the invisible resize border GetWindowRect
    includes) so captured pixels align with the visible window.
    """
    import ctypes
    import ctypes.wintypes as wintypes

    import psutil

    user32 = ctypes.windll.user32
    owner_l = target.owner.lower() if target.owner else None
    title_l = target.title.lower() if target.title else None

    matches: list[TargetWindow] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _enum_cb(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        if not title:
            return True  # unnamed tool/host windows
        if title_l is not None and title_l not in title.lower():
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            proc_name = psutil.Process(pid.value).name()
        except psutil.Error:
            proc_name = ""
        if owner_l is not None and owner_l not in proc_name.lower():
            return True
        rect = _window_rect(hwnd)
        if rect is None:
            return True
        left, top, right, bottom = rect
        matches.append(
            TargetWindow(
                window_id=int(hwnd),
                owner=proc_name,
                title=title,
                pid=int(pid.value),
                bounds=(
                    float(left),
                    float(top),
                    float(right - left),
                    float(bottom - top),
                ),
                on_screen=True,
            )
        )
        return True

    user32.EnumWindows(_enum_cb, 0)
    if not matches:
        return None
    return max(matches, key=lambda m: m.bounds[2] * m.bounds[3])


def _window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """Window rectangle in screen coordinates (DWM extended frame preferred)."""
    import ctypes
    import ctypes.wintypes as wintypes

    rect = wintypes.RECT()
    try:
        DWMWA_EXTENDED_FRAME_BOUNDS = 9
        res = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd),
            ctypes.wintypes.DWORD(DWMWA_EXTENDED_FRAME_BOUNDS),
            ctypes.byref(rect),
            ctypes.sizeof(rect),
        )
        if res == 0:
            return (rect.left, rect.top, rect.right, rect.bottom)
    except (AttributeError, OSError):  # pragma: no cover - dwmapi always present
        pass
    if not ctypes.windll.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        return None
    return (rect.left, rect.top, rect.right, rect.bottom)


def _capture_window_windows(window: TargetWindow) -> "Image.Image":
    """Windows: grab the window's screen region via mss (already a dependency).

    A region grab of the resolved bounds: unlike the macOS per-window buffer
    this captures whatever overlaps the rectangle, so the target window should
    stay unoccluded during recording (same operational guidance as flow's
    RECORDING.md). Bounds are re-resolved every frame, so a moved window stays
    correctly scoped.
    """
    from PIL import Image

    from openadapt_capture.utils import get_process_local_sct

    x, y, w, h = window.bounds
    if w <= 0 or h <= 0:
        raise WindowCaptureError(f"window {window.window_id} has empty bounds")
    monitor = {"left": int(x), "top": int(y), "width": int(w), "height": int(h)}
    try:
        sct_img = get_process_local_sct().grab(monitor)
    except Exception as exc:  # mss.ScreenShotError subclasses vary
        raise WindowCaptureError(f"window region grab failed: {exc}") from exc
    return Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")


def build_window_scope(owner: str | None, title: str | None) -> WindowCaptureScope | None:
    """Build a :class:`WindowCaptureScope` when a target is configured.

    Central place the recorder uses to turn (possibly-empty) config values
    into window mode: returns ``None`` when neither selector is set.
    """
    if not (owner or title):
        return None
    logger.info(f"window-scoped capture: owner={owner!r} title={title!r}")
    return WindowCaptureScope(WindowTarget(owner=owner, title=title))

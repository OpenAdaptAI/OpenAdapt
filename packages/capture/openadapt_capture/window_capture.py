"""Window-scoped capture: record ONE window in that window's own pixel space.

Why this exists (the Citrix / remote-display wedge): openadapt-flow's
``RemoteDisplayBackend`` (``record --backend rdp``, ``rdp_window`` mode)
*replays* against the pixels of a single client window (Parallels, Citrix
Workspace, Microsoft Remote Desktop) captured by exact window id. A
demonstration recorded FULL-SCREEN
is in a different coordinate space than that replay surface, so converters had
to work around the mismatch (record inside the session, or full-screen the
client). Window-scoped recording removes the mismatch at the source: frames
are captured in the target window's pixel space and input coordinates are
translated into that same space at capture time.

Coordinate semantics (kept in exact parity with openadapt-flow
``backends/remote_display.py`` — read that module before changing these):

- ``bounds`` is ``(x, y, w, h)`` in **screen points**, top-left origin — the
  space native platform observers use for global mouse coordinates.
- A captured frame contains the window's own **pixels**. Current macOS uses a
  ScreenCaptureKit desktop-independent window filter. Exact-window Quartz and
  system-utility providers remain compatibility paths.
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

import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Iterator, Optional

from loguru import logger

if TYPE_CHECKING:
    from PIL import Image

WINDOW_CAPTURE_SCHEMA_VERSION = "openadapt.capture.window-scoped/v2"


def window_geometry_epoch_sha256(state: dict) -> str:
    """Hash the exact native coordinate contract for one published frame."""
    payload = {
        key: state.get(key)
        for key in (
            "schema_version",
            "window_id",
            "owner",
            "pid",
            "process_start_time",
            "coordinate_source",
            "geometry_generation",
            "display_topology_sha256",
            "bounds",
            "scale",
            "scale_x",
            "scale_y",
            "viewport",
            "source_viewport",
            "content_rect",
            "fit_scale",
        )
    }
    encoded = json.dumps(
        {
            "schema_domain": "openadapt.capture.window-geometry-epoch/v1",
            **payload,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def window_capture_evidence_sha256(state: dict) -> str:
    """Hash provider evidence without changing the stable geometry digest."""
    payload = {
        key: state.get(key)
        for key in (
            "geometry_epoch_sha256",
            "capture_source",
            "visibility_independent",
            "on_screen",
            "frame_status",
            "frame_display_time",
            "pixel_display_time",
            "stream_generation",
            "stream_sequence",
        )
    }
    encoded = json.dumps(
        {
            "schema_domain": "openadapt.capture.window-frame-evidence/v1",
            **payload,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class WindowCaptureError(RuntimeError):
    """The target window could not be resolved or captured.

    Raised LOUDLY (never swallowed into an empty frame): a recording that
    silently fell back to full-screen would produce coordinates in the wrong
    space — a corrupt demonstration that *looks* valid.
    """


class WindowCapturePermissionError(WindowCaptureError):
    """The operating system denied exact-window capture."""


class WindowCaptureAmbiguousError(WindowCaptureError):
    """The configured selectors match more than one capturable window."""


class WindowCaptureUnavailableError(WindowCaptureError):
    """The exact-window capture provider is not available."""


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
    """One resolved exact window.

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
    process_start_time: float | None = None
    coordinate_source: str = "platform-screen"
    capture_source: str = "platform-window-image"
    visibility_independent: bool = False

    @property
    def identity(self) -> tuple[int, int, float | None, str]:
        """Return the process-bound identity used for the whole session."""
        return (
            self.window_id,
            self.pid,
            self.process_start_time,
            self.owner.casefold(),
        )


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
        frame_rate: float | None = None,
    ) -> None:
        """Initialize the scope for ``target``."""
        self.target = target
        self._resolver = resolver or resolve_window
        self._capture_provider = None
        if capturer is None and sys.platform == "darwin":
            self._capture_provider = _MacOSWindowCaptureProvider(
                frame_rate=frame_rate,
            )
            self._capturer = self._capture_provider.capture
        else:
            self._capturer = capturer or capture_window
        self._lock = threading.Lock()
        self._observation_lock = threading.RLock()
        self._window: TargetWindow | None = None
        self._scale: float | None = None
        self._scale_x: float | None = None
        self._scale_y: float | None = None
        self._viewport: tuple[int, int] | None = None
        self._source_viewport: tuple[int, int] | None = None
        self._content_rect: tuple[int, int, int, int] | None = None
        self._fit_scale: float | None = None
        self._capture_source: str | None = None
        self._frame_status: str | None = None
        self._frame_display_time: int | None = None
        self._pixel_display_time: int | None = None
        self._stream_sequence: int | None = None
        self._stream_generation: int | None = None
        self._geometry_generation = 0
        self._geometry_signature: tuple | None = None
        self._published_generation = 0
        self._published_window: TargetWindow | None = None
        self._published_scale_x: float | None = None
        self._published_scale_y: float | None = None
        self._published_content_rect: tuple[int, int, int, int] | None = None
        self._bound_identity: tuple[int, int, float | None, str] | None = None
        self._display_topology: dict | None = None
        self._display_topology_guard: Callable[..., None] | None = None
        # Window of the last CAPTURED frame (not merely resolved): the
        # bounds-timeline 'changed' flag compares frame to frame, so a bare
        # resolve() (e.g. a pre-flight existence check) never suppresses the
        # first frame's timeline entry.
        self._frame_window: TargetWindow | None = None

    def close(self) -> None:
        """Release an owned platform capture provider.

        Injected capturers remain caller-owned. The macOS recorder owns one
        persistent ScreenCaptureKit stream and must stop it before the session
        reaches its terminal state.
        """
        provider = self._capture_provider
        if provider is not None:
            provider.close()

    @contextmanager
    def observation_boundary(self) -> Iterator[None]:
        """Serialize a frame acquisition with native input observation."""
        with self._observation_lock:
            yield

    def bind_display_topology(
        self,
        snapshot: dict,
        guard: Callable[..., None],
    ) -> None:
        """Bind the exact active-display inventory for this recording."""
        if not isinstance(snapshot, dict) or not snapshot.get("topology_sha256"):
            raise WindowCaptureError("window capture requires hashed display topology")
        with self._lock:
            if self._display_topology is not None:
                raise WindowCaptureError("display topology is already bound")
            self._display_topology = dict(snapshot)
            self._display_topology_guard = guard

    def _assert_display_topology(self) -> None:
        with self._lock:
            guard = self._display_topology_guard
        if guard is None:
            raise WindowCaptureError(
                "window capture requires a bound display-topology guard"
            )
        guard(force=True)

    def resolve(self) -> TargetWindow:
        """Resolve the target window without changing captured-frame geometry.

        Raises:
            WindowCaptureError: If no matching window can be captured safely.
        """
        win = self._resolver(self.target)
        if win is None:
            raise WindowCaptureError(
                f"no window matching owner {self.target.owner!r} "
                f"title {self.target.title!r}; is the target application "
                "running with a capturable window?"
            )
        if self.target.owner and self.target.owner.casefold() not in win.owner.casefold():
            raise WindowCaptureError(
                "the window resolver returned an owner outside the configured selector"
            )
        if self.target.title and self.target.title.casefold() not in win.title.casefold():
            raise WindowCaptureError(
                "the window resolver returned a title outside the configured selector"
            )
        if not win.on_screen and not win.visibility_independent:
            raise WindowCaptureError(
                "the resolved target window is not visible and the active "
                "capture provider requires visibility"
            )
        if win.pid <= 0:
            raise WindowCaptureError("the resolved target has no owning process identity")
        if (
            win.process_start_time is None
            or not math.isfinite(win.process_start_time)
            or win.process_start_time <= 0
        ):
            raise WindowCaptureError(
                "the resolved target has no stable process start identity"
            )
        if not win.coordinate_source.strip():
            raise WindowCaptureError("the resolved target has no coordinate source")
        self._assert_window_topology_compatibility(win)
        return win

    def _assert_window_topology_compatibility(self, win: TargetWindow) -> None:
        """Require an X11 root-pixel window to fit the retained monitor union."""
        if win.coordinate_source != "x11-root-physical-pixels":
            return
        with self._lock:
            topology = self._display_topology
        # A preliminary resolve can run before the recorder binds topology.
        # Every frame and action resolve runs after that binding.
        if topology is None:
            return
        if topology.get("coordinate_space") != "virtual_desktop_pixels":
            raise WindowCaptureError("X11 window capture requires virtual_desktop_pixels topology")
        monitors = topology.get("monitors")
        if not isinstance(monitors, list) or not monitors:
            raise WindowCaptureError("X11 window capture requires physical monitor bounds")
        try:
            monitor_rects = [tuple(float(value) for value in monitor) for monitor in monitors]
        except (TypeError, ValueError) as exc:
            raise WindowCaptureError(
                "X11 display topology contains invalid monitor bounds"
            ) from exc
        if any(len(monitor) != 4 for monitor in monitor_rects):
            raise WindowCaptureError("X11 display topology contains invalid monitor bounds")
        if not _rectangle_covered_by_monitors(win.bounds, monitor_rects):
            raise WindowCaptureError(
                "the X11 target window is not fully covered by the bound display topology"
            )

    def _assert_bound_identity(self, win: TargetWindow) -> None:
        """Reject a recycled window handle or a different owning process."""
        with self._lock:
            bound_identity = self._bound_identity
        if bound_identity is not None and win.identity != bound_identity:
            raise WindowCaptureError(
                "the resolved target changed window identity or owning process "
                "during recording"
            )

    def capture_frame(self, *, publish: bool = True) -> tuple["Image.Image", bool]:
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
            output_viewport = self._viewport

        self._assert_display_topology()
        pre = self.resolve()
        self._assert_bound_identity(pre)
        source_image = self._capturer(pre)
        post = self.resolve()
        self._assert_bound_identity(post)
        self._assert_display_topology()
        if pre.identity != post.identity:
            raise WindowCaptureError(
                "the target process identity changed while a frame was captured"
            )
        if pre.bounds != post.bounds:
            raise WindowCaptureError(
                "the target moved or resized while a frame was captured; "
                "no action can bind to mixed frame geometry"
            )
        if pre.on_screen and not post.on_screen:
            raise WindowCaptureError(
                "the target stopped being visible while a frame was captured; "
                "the frame's minimized state cannot be proven"
            )
        win = post
        if source_image.width <= 0 or source_image.height <= 0:
            raise WindowCaptureError("window capture returned an empty frame")
        capture_source = str(
            source_image.info.get("openadapt_capture_source", win.capture_source)
        )
        frame_status = source_image.info.get("openadapt_frame_status")
        frame_display_time = source_image.info.get("openadapt_frame_display_time")
        pixel_display_time = source_image.info.get("openadapt_pixel_display_time")
        stream_sequence = source_image.info.get("openadapt_stream_sequence")
        stream_generation = source_image.info.get("openadapt_stream_generation")
        source_viewport = (source_image.width, source_image.height)
        output_viewport = output_viewport or source_viewport
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
            topology = self._display_topology
        topology_sha256 = (
            str(topology["topology_sha256"]) if topology is not None else None
        )
        geometry_signature = (
            win.identity,
            win.bounds,
            win.coordinate_source,
            output_viewport,
            source_viewport,
            (offset_x, offset_y, fitted_width, fitted_height),
            scale_x,
            scale_y,
            topology_sha256,
        )
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
            self._capture_source = capture_source
            self._frame_status = (
                str(frame_status) if frame_status is not None else None
            )
            self._frame_display_time = (
                int(frame_display_time) if frame_display_time is not None else None
            )
            self._pixel_display_time = (
                int(pixel_display_time) if pixel_display_time is not None else None
            )
            self._stream_sequence = (
                int(stream_sequence) if stream_sequence is not None else None
            )
            self._stream_generation = (
                int(stream_generation) if stream_generation is not None else None
            )
            if self._bound_identity is None:
                self._bound_identity = win.identity
            if geometry_signature != self._geometry_signature:
                self._geometry_generation += 1
                self._geometry_signature = geometry_signature
            self._frame_window = win
            generation = self._geometry_generation
            if publish:
                self._publish_locked(generation)
        changed = (
            prev is None
            or prev.window_id != win.window_id
            or prev.bounds != win.bounds
            or prev.title != win.title
        )
        return image, changed

    def _publish_locked(self, generation: int) -> None:
        """Expose one queued frame geometry to action observers."""
        if generation != self._geometry_generation or self._window is None:
            raise WindowCaptureError(
                f"cannot publish geometry generation {generation}; "
                f"the current generation is {self._geometry_generation}"
            )
        self._published_generation = generation
        self._published_window = self._window
        self._published_scale_x = self._scale_x
        self._published_scale_y = self._scale_y
        self._published_content_rect = self._content_rect

    def publish_frame(self, generation: int) -> None:
        """Publish geometry after its pixels and metadata enter the queue."""
        with self._lock:
            self._publish_locked(generation)

    def current_generation(self) -> int:
        """Return the most recently captured, not necessarily published, epoch."""
        with self._lock:
            if self._geometry_generation < 1:
                raise WindowCaptureError("no captured geometry generation is available")
            return self._geometry_generation

    def translate(self, x: float, y: float) -> tuple[float, float]:
        """Translate a global screen point into window-relative pixels.

        Uses the bounds/scale of the most recently captured frame (the frame
        this input event will be associated with).

        Raises:
            WindowCaptureError: If called before the first
                :meth:`capture_frame` (no bounds are known yet, and guessing
                a coordinate space would be a silent wrong action).
        """
        px, py, _generation = self.translate_with_generation(x, y)
        return px, py

    def _geometry_for_action(
        self,
    ) -> tuple[TargetWindow, float, float, tuple[int, int, int, int], int]:
        """Return the published geometry after exact live revalidation."""
        self._assert_display_topology()
        with self._lock:
            window = self._published_window
            scale_x = self._published_scale_x
            scale_y = self._published_scale_y
            content_rect = self._published_content_rect
            generation = self._published_generation
        if window is None or scale_x is None or scale_y is None or content_rect is None:
            raise WindowCaptureError(
                "an action arrived before the first published frame; "
                "capture_frame() must succeed before input can be scoped"
            )
        live = self.resolve()
        self._assert_bound_identity(live)
        if not live.on_screen and not live.visibility_independent:
            raise WindowCaptureError(
                "the target window is not visible at action time and the active "
                "capture provider requires visibility"
            )
        if live.bounds != window.bounds:
            raise WindowCaptureError(
                "the target moved or resized after the last published frame; "
                "wait for a matching frame before recording input"
            )
        self._assert_display_topology()
        return window, scale_x, scale_y, content_rect, generation

    def reserve_action_geometry(
        self,
    ) -> tuple[TargetWindow, float, float, tuple[int, int, int, int], int]:
        """Snapshot published geometry without blocking a native input hook."""
        with self._lock:
            window = self._published_window
            scale_x = self._published_scale_x
            scale_y = self._published_scale_y
            content_rect = self._published_content_rect
            generation = self._published_generation
        if window is None or scale_x is None or scale_y is None or content_rect is None:
            raise WindowCaptureError(
                "an action arrived before the first published frame; "
                "capture_frame() must succeed before input can be scoped"
            )
        return window, scale_x, scale_y, content_rect, generation

    def _assert_reserved_geometry_current(
        self,
        geometry: tuple[TargetWindow, float, float, tuple[int, int, int, int], int],
    ) -> None:
        """Refuse if delivery-time state no longer matches receipt-time state."""
        reserved_window = geometry[0]
        self._assert_display_topology()
        live = self.resolve()
        self._assert_bound_identity(live)
        if live.bounds != reserved_window.bounds:
            raise WindowCaptureError(
                "the target moved or resized after native input receipt; "
                "the delayed input cannot be bound to its reserved frame"
            )
        self._assert_display_topology()

    def generation_for_reserved_geometry(
        self,
        geometry: tuple[TargetWindow, float, float, tuple[int, int, int, int], int],
    ) -> int:
        """Return one receipt-time generation after delivery-time revalidation."""
        self._assert_reserved_geometry_current(geometry)
        return geometry[4]

    def translate_reserved_geometry(
        self,
        geometry: tuple[TargetWindow, float, float, tuple[int, int, int, int], int],
        x: float,
        y: float,
    ) -> tuple[float, float, int]:
        """Translate against receipt-time geometry after exact revalidation."""
        self._assert_reserved_geometry_current(geometry)
        window, scale_x, scale_y, content_rect, generation = geometry
        return (
            (x - window.bounds[0]) * scale_x + content_rect[0],
            (y - window.bounds[1]) * scale_y + content_rect[1],
            generation,
        )

    def generation_for_action(self) -> int:
        """Bind a non-pointer action to the exact published frame epoch."""
        return self._geometry_for_action()[4]

    def assert_current(self) -> None:
        """Revalidate the bound process, bounds, and display topology."""
        self._geometry_for_action()

    def translate_with_generation(self, x: float, y: float) -> tuple[float, float, int]:
        """Translate against the exact published frame after revalidation."""
        window, scale_x, scale_y, content_rect, generation = self._geometry_for_action()
        return (
            (x - window.bounds[0]) * scale_x + content_rect[0],
            (y - window.bounds[1]) * scale_y + content_rect[1],
            generation,
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
            capture_source = self._capture_source
            frame_status = self._frame_status
            frame_display_time = self._frame_display_time
            pixel_display_time = self._pixel_display_time
            stream_sequence = self._stream_sequence
            stream_generation = self._stream_generation
            generation = self._geometry_generation
            topology = self._display_topology
        if window is None:
            raise WindowCaptureError("no resolved window; call capture_frame() first")
        x, y, w, h = window.bounds
        state = {
            "schema_version": WINDOW_CAPTURE_SCHEMA_VERSION,
            "window_capture": True,
            "window_id": str(window.window_id),
            "owner": window.owner,
            "pid": window.pid,
            "process_start_time": window.process_start_time,
            "coordinate_source": window.coordinate_source,
            "capture_source": capture_source or window.capture_source,
            "visibility_independent": window.visibility_independent,
            "frame_status": frame_status,
            "frame_display_time": frame_display_time,
            "pixel_display_time": pixel_display_time,
            "stream_generation": stream_generation,
            "stream_sequence": stream_sequence,
            "geometry_generation": generation,
            "display_topology_sha256": (
                topology.get("topology_sha256") if topology else None
            ),
            "bounds": [x, y, w, h],
            "scale": scale,
            "scale_x": scale_x,
            "scale_y": scale_y,
            "viewport": list(viewport) if viewport else None,
            "source_viewport": list(source_viewport) if source_viewport else None,
            "content_rect": list(content_rect) if content_rect else None,
            "fit_scale": fit_scale,
            "on_screen": window.on_screen,
        }
        state["geometry_epoch_sha256"] = window_geometry_epoch_sha256(state)
        state["capture_evidence_sha256"] = window_capture_evidence_sha256(state)
        return {
            "title": window.title,
            "left": int(x),
            "top": int(y),
            "width": int(w),
            "height": int(h),
            "window_id": str(window.window_id),
            "state": state,
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
            capture_source = self._capture_source
            generation = self._geometry_generation
            topology = self._display_topology
        data: dict = {
            "schema_version": WINDOW_CAPTURE_SCHEMA_VERSION,
            "target": {"owner": self.target.owner, "title": self.target.title},
            "coordinate_space": "window_pixels",
            "display_topology": topology,
        }
        if window is not None:
            data.update(
                {
                    "window_id": window.window_id,
                    "owner": window.owner,
                    "title": window.title,
                    "pid": window.pid,
                    "process_start_time": window.process_start_time,
                    "coordinate_source": window.coordinate_source,
                    "capture_source": capture_source or window.capture_source,
                    "visibility_independent": window.visibility_independent,
                    "geometry_generation": generation,
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


def _rectangle_covered_by_monitors(
    rectangle: tuple[float, float, float, float],
    monitors: list[tuple[float, float, float, float]],
) -> bool:
    """Return whether the union of monitor rectangles fully covers a window."""
    x, y, width, height = rectangle
    if width <= 0 or height <= 0:
        return False
    right = x + width
    bottom = y + height
    relevant = [
        monitor
        for monitor in monitors
        if monitor[2] > 0
        and monitor[3] > 0
        and monitor[0] < right
        and monitor[1] < bottom
        and monitor[0] + monitor[2] > x
        and monitor[1] + monitor[3] > y
    ]
    if not relevant:
        return False
    x_edges = {x, right}
    y_edges = {y, bottom}
    for left, top, monitor_width, monitor_height in relevant:
        x_edges.update({max(x, left), min(right, left + monitor_width)})
        y_edges.update({max(y, top), min(bottom, top + monitor_height)})
    sorted_x = sorted(x_edges)
    sorted_y = sorted(y_edges)
    for left, cell_right in zip(sorted_x, sorted_x[1:]):
        if cell_right <= left:
            continue
        midpoint_x = (left + cell_right) / 2
        for top, cell_bottom in zip(sorted_y, sorted_y[1:]):
            if cell_bottom <= top:
                continue
            midpoint_y = (top + cell_bottom) / 2
            if not any(
                monitor_left <= midpoint_x < monitor_left + monitor_width
                and monitor_top <= midpoint_y < monitor_top + monitor_height
                for monitor_left, monitor_top, monitor_width, monitor_height in relevant
            ):
                return False
    return True


def resolve_window(target: WindowTarget) -> TargetWindow | None:
    """Find the front-most/largest window matching ``target`` on this platform."""
    if sys.platform == "darwin":
        return _resolve_window_macos(target)
    if sys.platform == "win32":
        return _resolve_window_windows(target)
    if sys.platform.startswith("linux"):
        from openadapt_capture.window_capture_linux import (
            LinuxWindowCaptureError,
            resolve_window_linux,
        )

        try:
            return resolve_window_linux(target)
        except LinuxWindowCaptureError as exc:
            raise WindowCaptureError(str(exc)) from exc
    raise WindowCaptureError(
        f"window-scoped capture is not supported on {sys.platform} "
        "(supported: darwin, win32, linux-x11)"
    )


def capture_window(window: TargetWindow) -> "Image.Image":
    """Capture ``window``'s pixels as a PIL image on this platform."""
    if sys.platform == "darwin":
        return _capture_window_macos(window)
    if sys.platform == "win32":
        return _capture_window_windows(window)
    if sys.platform.startswith("linux"):
        from openadapt_capture.window_capture_linux import (
            LinuxWindowCaptureError,
            capture_window_linux,
        )

        try:
            return capture_window_linux(window)
        except LinuxWindowCaptureError as exc:
            raise WindowCaptureError(str(exc)) from exc
    raise WindowCaptureError(
        f"window-scoped capture is not supported on {sys.platform} "
        "(supported: darwin, win32, linux-x11)"
    )


def _process_start_time(pid: int) -> float:
    """Return a stable process creation identity or fail closed."""
    import psutil

    try:
        return float(psutil.Process(pid).create_time())
    except psutil.Error as exc:
        raise WindowCaptureError(
            f"could not bind window owner PID {pid} to its process start time"
        ) from exc


_MACOS_CAPTURE_TIMEOUT_SECONDS = 15.0
_MACOS_PROVIDER_CHAIN_TIMEOUT_SECONDS = 20.0
_MACOS_SCK_ATTEMPT_TIMEOUT_SECONDS = 12.0
_MACOS_AX_STATE_TIMEOUT_SECONDS = 1.0
_MACOS_AX_GEOMETRY_TOLERANCE_POINTS = 2.0
_MACOS_RUNTIME_LOCK = threading.Lock()
_MACOS_RUNTIME_READY = False
_MACOS_SC_OUTPUT_CLASS: type | None = None
_MACOS_SC_OUTPUT_CLASS_LOCK = threading.Lock()


def prepare_macos_window_capture_runtime() -> None:
    """Initialize AppKit before ScreenCaptureKit runs on a worker thread."""
    global _MACOS_RUNTIME_READY
    if (
        sys.platform != "darwin"
        or _MACOS_RUNTIME_READY
        or not _screen_capture_kit_available()
    ):
        return
    with _MACOS_RUNTIME_LOCK:
        if _MACOS_RUNTIME_READY:
            return
        try:
            import AppKit

            loaded = AppKit.NSApplicationLoad()
        except (ImportError, OSError) as exc:
            raise WindowCaptureUnavailableError(
                "macOS window capture could not initialize AppKit"
            ) from exc
        if loaded is False:
            raise WindowCaptureUnavailableError(
                "macOS window capture could not load AppKit"
            )
        _MACOS_RUNTIME_READY = True


def _screen_capture_kit_available() -> bool:
    """Return whether persistent desktop-independent capture is available."""
    try:
        import ScreenCaptureKit
    except ImportError:
        return False
    return all(
        hasattr(ScreenCaptureKit, name)
        for name in (
            "SCContentFilter",
            "SCShareableContent",
            "SCStream",
            "SCStreamConfiguration",
        )
    )


def _macos_visibility_independent_capture_available() -> bool:
    """Return whether an exact window can be captured without visibility."""
    return _screen_capture_kit_available() or (
        os.path.isfile("/usr/sbin/screencapture")
        and os.access("/usr/sbin/screencapture", os.X_OK)
    )


def _macos_completion(
    start: Callable[[Callable[..., None]], None],
    *,
    operation: str,
    timeout_seconds: float = _MACOS_CAPTURE_TIMEOUT_SECONDS,
) -> object:
    """Wait for one ScreenCaptureKit completion without requiring an event loop."""
    completed = threading.Event()
    result: dict[str, object | None] = {"value": None, "error": None}

    def completion(value: object | None, error: object | None) -> None:
        result["value"] = value
        result["error"] = error
        completed.set()

    try:
        start(completion)
    except Exception as exc:
        raise WindowCaptureUnavailableError(
            f"ScreenCaptureKit could not start {operation}"
        ) from exc
    if not completed.wait(max(0.001, timeout_seconds)):
        raise WindowCaptureUnavailableError(
            f"ScreenCaptureKit timed out during {operation}"
        )
    if result["error"] is not None:
        error = result["error"]
        code_getter = getattr(error, "code", None)
        code = int(code_getter()) if callable(code_getter) else None
        error_type = (
            WindowCapturePermissionError
            if code in {-3801, -3803}
            else WindowCaptureError
        )
        raise error_type(
            f"ScreenCaptureKit failed during {operation}"
            + (f" (error {code})" if code is not None else "")
        )
    if result["value"] is None:
        raise WindowCaptureError(
            f"ScreenCaptureKit returned no result during {operation}"
        )
    return result["value"]


def _macos_error_completion(
    start: Callable[[Callable[[object | None], None]], None],
    *,
    operation: str,
    timeout_seconds: float = _MACOS_CAPTURE_TIMEOUT_SECONDS,
) -> None:
    """Wait for a ScreenCaptureKit start/stop completion callback."""
    completed = threading.Event()
    result: dict[str, object | None] = {"error": None}

    def completion(error: object | None) -> None:
        result["error"] = error
        completed.set()

    try:
        start(completion)
    except Exception as exc:
        raise WindowCaptureUnavailableError(
            f"ScreenCaptureKit could not start {operation}"
        ) from exc
    if not completed.wait(max(0.001, timeout_seconds)):
        raise WindowCaptureUnavailableError(
            f"ScreenCaptureKit timed out during {operation}"
        )
    if result["error"] is not None:
        error = result["error"]
        code_getter = getattr(error, "code", None)
        code = int(code_getter()) if callable(code_getter) else None
        error_type = (
            WindowCapturePermissionError
            if code in {-3801, -3803}
            else WindowCaptureError
        )
        raise error_type(
            f"ScreenCaptureKit failed during {operation}"
            + (f" (error {code})" if code is not None else "")
        )


def _screen_capture_kit_window(
    window_id: int,
    *,
    timeout_seconds: float = _MACOS_CAPTURE_TIMEOUT_SECONDS,
) -> object:
    """Return the exact shareable window, including windows on other Spaces."""
    try:
        import ScreenCaptureKit
    except ImportError as exc:
        raise WindowCaptureUnavailableError(
            "ScreenCaptureKit requires pyobjc-framework-ScreenCaptureKit"
        ) from exc

    get_content = getattr(
        ScreenCaptureKit.SCShareableContent,
        "getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_",
    )
    content = _macos_completion(
        lambda callback: get_content(
            True,
            False,
            callback,
        ),
        operation="window enumeration",
        timeout_seconds=timeout_seconds,
    )
    windows = list(content.windows() or [])
    match = next(
        (candidate for candidate in windows if int(candidate.windowID()) == window_id),
        None,
    )
    if match is None:
        raise WindowCaptureError(
            f"ScreenCaptureKit cannot access exact window {window_id}"
        )
    return match


def _pil_image_from_cgimage(img_ref: object, *, source: str) -> "Image.Image":
    """Convert one ScreenCaptureKit or Quartz CGImage to stable RGB pixels."""
    import Quartz
    from PIL import Image

    width = int(Quartz.CGImageGetWidth(img_ref))
    height = int(Quartz.CGImageGetHeight(img_ref))
    if width <= 0 or height <= 0:
        raise WindowCaptureError("captured window image has zero size")
    bytes_per_row = int(Quartz.CGImageGetBytesPerRow(img_ref))
    provider = Quartz.CGImageGetDataProvider(img_ref)
    data = Quartz.CGDataProviderCopyData(provider)
    image = Image.frombuffer(
        "RGBA",
        (width, height),
        bytes(data),
        "raw",
        "BGRA",
        bytes_per_row,
        1,
    ).convert("RGB")
    image.info["openadapt_capture_source"] = source
    return image


def _screen_capture_kit_output_class() -> type:
    """Build the Objective-C stream delegate once, without display access."""
    global _MACOS_SC_OUTPUT_CLASS
    if _MACOS_SC_OUTPUT_CLASS is not None:
        return _MACOS_SC_OUTPUT_CLASS
    with _MACOS_SC_OUTPUT_CLASS_LOCK:
        if _MACOS_SC_OUTPUT_CLASS is not None:
            return _MACOS_SC_OUTPUT_CLASS
        try:
            import objc
            from Foundation import NSObject
        except ImportError as exc:
            raise WindowCaptureUnavailableError(
                "ScreenCaptureKit requires PyObjC Cocoa bindings"
            ) from exc

        class OpenAdaptSCStreamOutput(
            NSObject,
            protocols=[
                objc.protocolNamed("SCStreamOutput"),
                objc.protocolNamed("SCStreamDelegate"),
            ],
        ):
            def initWithOwner_(self, owner):
                self = objc.super(OpenAdaptSCStreamOutput, self).init()
                if self is not None:
                    self._openadapt_owner = owner
                return self

            @objc.typedSelector(b"v@:@^{opaqueCMSampleBuffer=}q")
            def stream_didOutputSampleBuffer_ofType_(
                self,
                _stream,
                sample_buffer,
                output_type,
            ):
                self._openadapt_owner._receive_sample(
                    self._openadapt_generation,
                    sample_buffer,
                    output_type,
                )

            @objc.typedSelector(b"v@:@@")
            def stream_didStopWithError_(self, _stream, error):
                self._openadapt_owner._receive_stop(
                    self._openadapt_generation,
                    error,
                )

        _MACOS_SC_OUTPUT_CLASS = OpenAdaptSCStreamOutput
        return _MACOS_SC_OUTPUT_CLASS


def _macos_dispatch_queue(label: bytes) -> object:
    """Create the serial dispatch queue required by SCStreamOutput."""
    try:
        import objc

        functions: dict[str, object] = {}
        objc.loadBundleFunctions(None, functions, [("dispatch_queue_create", b"@*@")])
        return functions["dispatch_queue_create"](label, None)
    except Exception as exc:
        raise WindowCaptureUnavailableError(
            "ScreenCaptureKit could not create its callback queue"
        ) from exc


def _pil_image_from_sample_buffer(sample_buffer: object) -> "Image.Image":
    """Copy a ScreenCaptureKit sample buffer into stable RGB pixels."""
    try:
        import CoreMedia
        import Quartz

        pixel_buffer = CoreMedia.CMSampleBufferGetImageBuffer(sample_buffer)
        if pixel_buffer is None:
            raise WindowCaptureError("ScreenCaptureKit returned no pixel buffer")
        ci_image = Quartz.CIImage.imageWithCVPixelBuffer_(pixel_buffer)
        context = Quartz.CIContext.contextWithOptions_(None)
        img_ref = context.createCGImage_fromRect_(ci_image, ci_image.extent())
    except WindowCaptureError:
        raise
    except Exception as exc:
        raise WindowCaptureError(
            "ScreenCaptureKit returned an unreadable pixel buffer"
        ) from exc
    if img_ref is None:
        raise WindowCaptureError("ScreenCaptureKit returned no window image")
    return _pil_image_from_cgimage(
        img_ref,
        source="macos-screencapturekit-stream",
    )


class _MacOSScreenCaptureKitStream:
    """One persistent desktop-independent stream for one exact window ID."""

    def __init__(self, *, frame_rate: float | None = None) -> None:
        self._condition = threading.Condition()
        self._capture_lock = threading.RLock()
        self._stream = None
        self._delegate = None
        self._queue = None
        self._window_id: int | None = None
        self._bounds_size: tuple[float, float] | None = None
        self._sequence = 0
        self._delivered_sequence = 0
        self._last_complete_image = None
        self._last_complete_display_time: int | None = None
        self._last_display_time: int | None = None
        self._last_status: int | None = None
        self._error: WindowCaptureError | None = None
        self._closing = False
        self._closed = False
        self._generation = 0
        self._frame_rate = (
            float(frame_rate)
            if frame_rate is not None
            and math.isfinite(float(frame_rate))
            and float(frame_rate) > 0
            else 10.0
        )

    def _receive_sample(
        self,
        generation: int,
        sample_buffer: object,
        output_type: int,
    ) -> None:
        with self._condition:
            if generation != self._generation or self._closing or self._closed:
                return
        try:
            import CoreMedia
            import ScreenCaptureKit

            if int(output_type) != int(ScreenCaptureKit.SCStreamOutputTypeScreen):
                return
            attachments = CoreMedia.CMSampleBufferGetSampleAttachmentsArray(
                sample_buffer,
                False,
            )
            attachment = list(attachments or [])[0] if attachments else {}
            status_value = attachment.get(ScreenCaptureKit.SCStreamFrameInfoStatus)
            if status_value is None:
                raise WindowCaptureError(
                    "ScreenCaptureKit frame has no explicit status"
                )
            status = int(status_value)
            display_time_value = attachment.get(
                ScreenCaptureKit.SCStreamFrameInfoDisplayTime
            )
            if display_time_value is None:
                raise WindowCaptureError(
                    "ScreenCaptureKit frame has no display time"
                )
            display_time = int(display_time_value)
            image = None
            error = None
            if status == int(ScreenCaptureKit.SCFrameStatusComplete):
                image = _pil_image_from_sample_buffer(sample_buffer)
            elif status not in (
                int(ScreenCaptureKit.SCFrameStatusIdle),
                int(ScreenCaptureKit.SCFrameStatusStarted),
            ):
                error = WindowCaptureError(
                    f"ScreenCaptureKit returned unusable frame status {status}"
                )
        except Exception as exc:
            image = None
            status = -1
            display_time = None
            error = (
                exc
                if isinstance(exc, WindowCaptureError)
                else WindowCaptureError("ScreenCaptureKit frame processing failed")
            )
        with self._condition:
            if generation != self._generation or self._closing or self._closed:
                return
            self._sequence += 1
            self._last_status = status
            if display_time is not None:
                self._last_display_time = display_time
            if image is not None:
                self._last_complete_image = image
                self._last_complete_display_time = display_time
            if error is not None:
                self._error = error
            self._condition.notify_all()

    def _receive_stop(self, generation: int, error: object | None) -> None:
        with self._condition:
            if generation != self._generation:
                return
            if not self._closing:
                code_getter = getattr(error, "code", None)
                code = int(code_getter()) if callable(code_getter) else None
                self._error = WindowCaptureError(
                    "ScreenCaptureKit stopped the exact-window stream"
                    + (f" (error {code})" if code is not None else "")
                )
            self._condition.notify_all()

    @staticmethod
    def _remaining(deadline: float, operation: str) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WindowCaptureUnavailableError(
                f"ScreenCaptureKit timed out before {operation}"
            )
        return remaining

    def _start(self, window: TargetWindow, *, deadline: float) -> None:
        try:
            import CoreMedia
            import ScreenCaptureKit
        except ImportError as exc:
            raise WindowCaptureUnavailableError(
                "ScreenCaptureKit requires its PyObjC framework bindings"
            ) from exc

        prepare_macos_window_capture_runtime()
        sc_window = _screen_capture_kit_window(
            window.window_id,
            timeout_seconds=self._remaining(deadline, "window enumeration"),
        )
        content_filter = (
            ScreenCaptureKit.SCContentFilter.alloc().initWithDesktopIndependentWindow_(
                sc_window
            )
        )
        point_scale = float(content_filter.pointPixelScale())
        if not math.isfinite(point_scale) or point_scale <= 0:
            raise WindowCaptureError(
                "ScreenCaptureKit returned an invalid point-to-pixel scale"
            )
        configuration = ScreenCaptureKit.SCStreamConfiguration.alloc().init()
        configuration.setWidth_(max(1, round(window.bounds[2] * point_scale)))
        configuration.setHeight_(max(1, round(window.bounds[3] * point_scale)))
        configuration.setShowsCursor_(False)
        configuration.setQueueDepth_(3)
        configuration.setMinimumFrameInterval_(
            CoreMedia.CMTimeMakeWithSeconds(1.0 / self._frame_rate, 600)
        )
        if hasattr(configuration, "setIgnoreShadowsSingleWindow_"):
            configuration.setIgnoreShadowsSingleWindow_(True)

        with self._condition:
            if self._closed:
                raise WindowCaptureError("ScreenCaptureKit stream is closed")
            self._generation += 1
            generation = self._generation
        output_class = _screen_capture_kit_output_class()
        delegate = output_class.alloc().initWithOwner_(self)
        delegate._openadapt_generation = generation
        queue = _macos_dispatch_queue(
            f"ai.openadapt.capture.window.{window.window_id}".encode("ascii")
        )
        stream = ScreenCaptureKit.SCStream.alloc().initWithFilter_configuration_delegate_(
            content_filter,
            configuration,
            delegate,
        )
        result = stream.addStreamOutput_type_sampleHandlerQueue_error_(
            delegate,
            ScreenCaptureKit.SCStreamOutputTypeScreen,
            queue,
            None,
        )
        if isinstance(result, tuple):
            added, add_error = result
        else:
            added, add_error = result, None
        if not added:
            code_getter = getattr(add_error, "code", None)
            code = int(code_getter()) if callable(code_getter) else None
            raise WindowCaptureError(
                "ScreenCaptureKit could not add the exact-window output"
                + (f" (error {code})" if code is not None else "")
            )
        with self._condition:
            if self._closed:
                raise WindowCaptureError("ScreenCaptureKit stream is closed")
            self._stream = stream
            self._delegate = delegate
            self._queue = queue
            self._window_id = window.window_id
            self._bounds_size = (window.bounds[2], window.bounds[3])
            self._sequence = 0
            self._delivered_sequence = 0
            self._last_complete_image = None
            self._last_complete_display_time = None
            self._last_display_time = None
            self._last_status = None
            self._error = None
            self._closing = False
        try:
            _macos_error_completion(
                lambda callback: stream.startCaptureWithCompletionHandler_(callback),
                operation=f"exact-window stream start for window {window.window_id}",
                timeout_seconds=self._remaining(deadline, "stream start"),
            )
        except Exception:
            self._stop_stream(suppress_errors=True, deadline=deadline)
            raise

    def capture(
        self,
        window: TargetWindow,
        *,
        deadline: float | None = None,
    ) -> "Image.Image":
        """Return the next complete or proven-idle frame from the stream."""
        try:
            import ScreenCaptureKit
        except ImportError as exc:
            raise WindowCaptureUnavailableError(
                "ScreenCaptureKit requires its PyObjC framework bindings"
            ) from exc
        usable_statuses = {
            int(ScreenCaptureKit.SCFrameStatusComplete),
            int(ScreenCaptureKit.SCFrameStatusIdle),
        }
        deadline = deadline or (
            time.monotonic() + _MACOS_SCK_ATTEMPT_TIMEOUT_SECONDS
        )
        with self._capture_lock:
            if self._closed:
                raise WindowCaptureError("ScreenCaptureKit stream is closed")
            desired_size = (window.bounds[2], window.bounds[3])
            if (
                self._stream is None
                or self._window_id != window.window_id
                or self._bounds_size != desired_size
            ):
                self._stop_stream(suppress_errors=False, deadline=deadline)
                self._start(window, deadline=deadline)
            with self._condition:
                while (
                    (
                        self._sequence <= self._delivered_sequence
                        or self._last_complete_image is None
                        or self._last_status not in usable_statuses
                    )
                    and self._error is None
                ):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise WindowCaptureUnavailableError(
                            "ScreenCaptureKit timed out waiting for an exact-window frame"
                        )
                    self._condition.wait(remaining)
                captured_error = self._error
                if captured_error is None:
                    self._delivered_sequence = self._sequence
                    image = self._last_complete_image.copy()
                    image.info["openadapt_capture_source"] = (
                        "macos-screencapturekit-stream"
                    )
                    image.info["openadapt_frame_status"] = (
                        "complete"
                        if self._last_status
                        == int(ScreenCaptureKit.SCFrameStatusComplete)
                        else "idle"
                    )
                    image.info["openadapt_frame_display_time"] = (
                        self._last_display_time
                    )
                    image.info["openadapt_pixel_display_time"] = (
                        self._last_complete_display_time
                    )
                    image.info["openadapt_stream_sequence"] = self._sequence
                    image.info["openadapt_stream_generation"] = self._generation
            if captured_error is not None:
                if self._closed:
                    self._stop_stream(
                        suppress_errors=True,
                        deadline=time.monotonic() + _MACOS_CAPTURE_TIMEOUT_SECONDS,
                    )
                raise captured_error
            return image

    def _stop_stream(
        self,
        *,
        suppress_errors: bool,
        deadline: float | None = None,
    ) -> None:
        """Stop the current generation. The caller holds ``_capture_lock``."""
        with self._condition:
            stream = self._stream
            self._closing = True
        stop_error = None
        if stream is not None:
            try:
                _macos_error_completion(
                    lambda callback: stream.stopCaptureWithCompletionHandler_(callback),
                    operation="exact-window stream stop",
                    timeout_seconds=(
                        self._remaining(deadline, "stream stop")
                        if deadline is not None
                        else _MACOS_CAPTURE_TIMEOUT_SECONDS
                    ),
                )
            except WindowCaptureError as exc:
                stop_error = exc
        with self._condition:
            self._stream = None
            self._delegate = None
            self._queue = None
            self._window_id = None
            self._bounds_size = None
            if not self._closed:
                self._closing = False
            self._condition.notify_all()
        if stop_error is not None:
            if suppress_errors:
                logger.warning("ScreenCaptureKit stream stop failed: {}", stop_error)
            else:
                raise stop_error

    def close(self, *, timeout_seconds: float = _MACOS_CAPTURE_TIMEOUT_SECONDS) -> None:
        """Stop the stream and wake any waiting capture."""
        deadline = time.monotonic() + max(0.001, timeout_seconds)
        with self._condition:
            self._closed = True
            self._error = WindowCaptureError("ScreenCaptureKit stream was closed")
            self._condition.notify_all()
        remaining = max(0.0, deadline - time.monotonic())
        if not self._capture_lock.acquire(timeout=remaining):
            logger.warning(
                "ScreenCaptureKit close timed out waiting for active frame capture; "
                "the capture owner will stop the stream"
            )
            return
        try:
            self._stop_stream(
                suppress_errors=True,
                deadline=deadline,
            )
        finally:
            self._capture_lock.release()


def _capture_window_macos_utility(
    window: TargetWindow,
    *,
    timeout_seconds: float = _MACOS_CAPTURE_TIMEOUT_SECONDS,
) -> "Image.Image":
    """Use the signed system utility as an exact-window compatibility path."""
    from PIL import Image

    with tempfile.TemporaryDirectory(prefix="openadapt-window-") as temp_dir:
        capture_path = f"{temp_dir}/window.png"
        try:
            result = subprocess.run(
                [
                    "/usr/sbin/screencapture",
                    "-x",
                    "-o",
                    "-l",
                    str(window.window_id),
                    capture_path,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=max(0.001, timeout_seconds),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WindowCaptureUnavailableError(
                "the macOS exact-window compatibility capture could not run"
            ) from exc
        if result.returncode != 0:
            raise WindowCapturePermissionError(
                "the macOS exact-window compatibility capture was denied"
            )
        try:
            with Image.open(capture_path) as image:
                captured = image.convert("RGB").copy()
        except OSError as exc:
            raise WindowCaptureError(
                "the macOS exact-window compatibility capture produced no readable image"
            ) from exc
    captured.info["openadapt_capture_source"] = "macos-screencapture-utility"
    return captured


def _macos_window_minimized(
    window: TargetWindow,
    *,
    deadline: float | None = None,
) -> bool | None:
    """Return the exact AX window's minimized state, or ``None`` if unknown.

    Quartz reports both minimized and other-Space windows as not on screen.
    A minimized window can expose stale backing pixels through an exact-window
    API. Off-screen capture therefore requires Accessibility to distinguish a
    normal window on another Space from a minimized window.
    """
    deadline = deadline or (time.monotonic() + _MACOS_AX_STATE_TIMEOUT_SECONDS)
    try:
        import ApplicationServices

        set_timeout = ApplicationServices.AXUIElementSetMessagingTimeout

        def attribute(element: object, name: str) -> object | None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            timeout_error = set_timeout(
                element,
                min(_MACOS_AX_STATE_TIMEOUT_SECONDS, remaining),
            )
            if timeout_error != ApplicationServices.kAXErrorSuccess:
                return None
            error, value = ApplicationServices.AXUIElementCopyAttributeValue(
                element,
                name,
                None,
            )
            if error != ApplicationServices.kAXErrorSuccess:
                return None
            return value

        application = ApplicationServices.AXUIElementCreateApplication(window.pid)
        candidates = attribute(application, "AXWindows")
        if candidates is None:
            return None
    except Exception:
        return None

    id_matches = []
    title_candidates = []
    for candidate in candidates or []:
        try:
            number = attribute(candidate, "AXWindowNumber")
            if number is not None:
                if int(number) == window.window_id:
                    id_matches.append(candidate)
                continue
            title = attribute(candidate, "AXTitle")
            if title is not None and str(title) == window.title:
                title_candidates.append(candidate)
        except Exception:
            continue

    title_matches = []
    if not id_matches:
        for candidate in title_candidates:
            try:
                position_value = attribute(candidate, "AXPosition")
                size_value = attribute(candidate, "AXSize")
                if position_value is None or size_value is None:
                    continue
                position_ok, position = ApplicationServices.AXValueGetValue(
                    position_value,
                    ApplicationServices.kAXValueCGPointType,
                    None,
                )
                size_ok, size = ApplicationServices.AXValueGetValue(
                    size_value,
                    ApplicationServices.kAXValueCGSizeType,
                    None,
                )
                if not position_ok or not size_ok:
                    continue
                expected = window.bounds
                actual = (
                    float(position.x),
                    float(position.y),
                    float(size.width),
                    float(size.height),
                )
                if all(
                    abs(actual[index] - expected[index])
                    <= _MACOS_AX_GEOMETRY_TOLERANCE_POINTS
                    for index in range(4)
                ):
                    title_matches.append(candidate)
            except Exception:
                continue

    matches = id_matches if id_matches else title_matches
    if len(matches) != 1:
        return None
    try:
        minimized = attribute(matches[0], "AXMinimized")
    except Exception:
        return None
    if minimized is None:
        return None
    return bool(minimized)


def _resolve_window_macos(target: WindowTarget) -> TargetWindow | None:
    """macOS: CGWindowList by owner/title substring.

    ScreenCaptureKit can capture an occluded window or a window on another
    Space. Older macOS versions retain the visible-window Quartz behavior.
    """
    import Quartz

    visibility_independent = _macos_visibility_independent_capture_available()
    owner_l = target.owner.lower() if target.owner else None
    title_l = target.title.lower() if target.title else None
    wins = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID)
    matches: list[TargetWindow] = []
    for w in wins or []:
        owner = str(w.get("kCGWindowOwnerName", "") or "")
        name = str(w.get("kCGWindowName", "") or "")
        if owner_l is not None and owner_l not in owner.lower():
            continue
        if title_l is not None and title_l not in name.lower():
            continue
        if int(w.get("kCGWindowLayer", 0) or 0) != 0:
            continue  # skip menubar/overlay layers; the app window is layer 0
        on_screen = bool(w.get("kCGWindowIsOnscreen", False))
        if not on_screen and not visibility_independent:
            continue
        b = w.get("kCGWindowBounds", {}) or {}
        bounds = (
            float(b.get("X", 0.0)),
            float(b.get("Y", 0.0)),
            float(b.get("Width", 0.0)),
            float(b.get("Height", 0.0)),
        )
        if bounds[2] <= 0 or bounds[3] <= 0:
            continue
        pid = int(w.get("kCGWindowOwnerPID", 0) or 0)
        matches.append(
            TargetWindow(
                window_id=int(w.get("kCGWindowNumber", 0) or 0),
                owner=owner,
                title=name,
                pid=pid,
                bounds=bounds,
                on_screen=on_screen,
                process_start_time=_process_start_time(pid),
                coordinate_source="quartz-screen-points",
                capture_source=(
                    "macos-exact-window-provider-chain"
                    if visibility_independent
                    else "macos-quartz-window-image"
                ),
                visibility_independent=visibility_independent,
            )
        )
    if not matches:
        return None

    exact = [
        match
        for match in matches
        if (target.owner is None or match.owner.casefold() == target.owner.casefold())
        and (target.title is None or match.title.casefold() == target.title.casefold())
    ]
    candidates = exact or matches
    if len(candidates) != 1:
        raise WindowCaptureAmbiguousError(
            f"window selectors matched {len(candidates)} capturable windows; "
            "provide the complete window title"
        )
    return candidates[0]


def _capture_window_macos(window: TargetWindow) -> "Image.Image":
    """Capture an exact macOS window through an explicit provider chain.

    ScreenCaptureKit is primary. Quartz remains for older systems. The signed
    system utility is the final exact-window compatibility path. No provider
    can substitute a full-screen image or another window.
    """
    provider = _MacOSWindowCaptureProvider()
    try:
        return provider.capture(window)
    finally:
        provider.close()


class _MacOSWindowCaptureProvider:
    """Session-owned exact-window providers with sticky safe fallback."""

    def __init__(self, *, frame_rate: float | None = None) -> None:
        self._state_lock = threading.Lock()
        self._closed = False
        self._stream = (
            _MacOSScreenCaptureKitStream(frame_rate=frame_rate)
            if _screen_capture_kit_available()
            else None
        )
        self._sck_disabled = self._stream is None
        self._quartz_disabled = False

    def _assert_open(self) -> None:
        with self._state_lock:
            if self._closed:
                raise WindowCaptureError("the exact-window capture provider is closed")

    def capture(self, window: TargetWindow) -> "Image.Image":
        """Capture only ``window`` and never substitute desktop pixels."""
        failures: list[BaseException] = []
        chain_deadline = time.monotonic() + _MACOS_PROVIDER_CHAIN_TIMEOUT_SECONDS
        self._assert_open()

        def assert_offscreen_target_is_safe() -> None:
            if window.on_screen:
                return
            minimized = _macos_window_minimized(window, deadline=chain_deadline)
            if minimized is True:
                raise WindowCaptureUnavailableError(
                    "the exact target window is minimized; restore it before recording"
                )
            if minimized is None:
                raise WindowCaptureUnavailableError(
                    "the exact off-screen target could not be proven non-minimized; "
                    "grant Accessibility access or move the window to the active Space"
                )

        assert_offscreen_target_is_safe()
        if not self._sck_disabled and self._stream is not None:
            try:
                captured = self._stream.capture(
                    window,
                    deadline=min(
                        chain_deadline,
                        time.monotonic() + _MACOS_SCK_ATTEMPT_TIMEOUT_SECONDS,
                    ),
                )
            except WindowCaptureError as exc:
                self._assert_open()
                failures.append(exc)
                self._stream.close(timeout_seconds=2.0)
                self._sck_disabled = True
                logger.warning(
                    "ScreenCaptureKit exact-window stream failed; disabling it "
                    "for this recording session"
                )
            else:
                self._assert_open()
                assert_offscreen_target_is_safe()
                return captured

        if window.on_screen and not self._quartz_disabled:
            self._assert_open()
            try:
                import Quartz

                img_ref = Quartz.CGWindowListCreateImage(
                    Quartz.CGRectNull,
                    Quartz.kCGWindowListOptionIncludingWindow,
                    window.window_id,
                    Quartz.kCGWindowImageBoundsIgnoreFraming,
                )
            except Exception as exc:
                self._assert_open()
                img_ref = None
                failures.append(
                    WindowCaptureUnavailableError(
                        "Quartz exact-window capture could not run"
                    )
                )
                logger.debug("Quartz exact-window capture failed: {}", exc)
            if img_ref is not None:
                captured = _pil_image_from_cgimage(
                    img_ref,
                    source="macos-quartz-window-image",
                )
                self._assert_open()
                return captured
            self._quartz_disabled = True
            self._assert_open()
            failures.append(
                WindowCapturePermissionError(
                    f"Quartz returned no image for exact window {window.window_id}"
                )
            )
            logger.warning(
                "Quartz exact-window capture returned no image; disabling it "
                "for this recording session"
            )
        elif not window.on_screen:
            failures.append(
                WindowCapturePermissionError(
                    "Quartz was skipped because the exact target is not on screen"
                )
            )

        try:
            self._assert_open()
            assert_offscreen_target_is_safe()
            remaining = chain_deadline - time.monotonic()
            if remaining <= 0:
                raise WindowCaptureUnavailableError(
                    "the exact-window provider chain exhausted its startup budget"
                )
            captured = _capture_window_macos_utility(
                window,
                timeout_seconds=min(_MACOS_CAPTURE_TIMEOUT_SECONDS, remaining),
            )
            self._assert_open()
            assert_offscreen_target_is_safe()
            return captured
        except WindowCaptureError as exc:
            self._assert_open()
            failures.append(exc)
            failure_type = (
                WindowCapturePermissionError
                if failures
                and all(
                    isinstance(item, WindowCapturePermissionError)
                    for item in failures
                )
                else WindowCaptureError
            )
            provider_details = "; ".join(
                f"{type(provider_failure).__name__}: {provider_failure}"
                for provider_failure in failures
            )
            failure = failure_type(
                f"all exact-window capture providers failed for window "
                f"{window.window_id}: {provider_details}"
            )
            for provider_failure in failures:
                try:
                    failure.add_note(
                        f"{type(provider_failure).__name__}: {provider_failure}"
                    )
                except AttributeError:  # Python 3.10
                    pass
            raise failure from exc

    def close(self) -> None:
        """Stop the owned stream, if it started."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        if self._stream is not None:
            self._stream.close()


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
            process = psutil.Process(pid.value)
            proc_name = process.name()
            process_start_time = float(process.create_time())
        except psutil.Error:
            return True
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
                process_start_time=process_start_time,
                coordinate_source="dwm-physical-pixels",
            )
        )
        return True

    user32.EnumWindows(_enum_cb, 0)
    if not matches:
        return None
    return max(matches, key=lambda m: m.bounds[2] * m.bounds[3])


def _window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """Return DWM physical bounds; never mix DPI-virtualized coordinates."""
    import ctypes
    import ctypes.wintypes as wintypes

    rect = wintypes.RECT()
    try:
        dwmapi = ctypes.windll.dwmapi
    except (AttributeError, OSError):  # pragma: no cover - supported Windows has DWM
        return None
    DWMWA_EXTENDED_FRAME_BOUNDS = 9
    res = dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd),
        ctypes.wintypes.DWORD(DWMWA_EXTENDED_FRAME_BOUNDS),
        ctypes.byref(rect),
        ctypes.sizeof(rect),
    )
    if res != 0:
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


def build_window_scope(
    owner: str | None,
    title: str | None,
    *,
    frame_rate: float | None = None,
) -> WindowCaptureScope | None:
    """Build a :class:`WindowCaptureScope` when a target is configured.

    Central place the recorder uses to turn (possibly-empty) config values
    into window mode: returns ``None`` when neither selector is set.
    """
    if not (owner or title):
        return None
    logger.info(
        "window-scoped capture configured: owner_selector={} title_selector={}",
        bool(owner),
        bool(title),
    )
    return WindowCaptureScope(
        WindowTarget(owner=owner, title=title),
        frame_rate=frame_rate,
    )

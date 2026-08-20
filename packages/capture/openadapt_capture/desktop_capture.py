"""Virtual-desktop coordinate contract for full-screen recordings."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from numbers import Integral
from typing import Any, Callable, Iterable, Mapping


class DesktopCaptureError(RuntimeError):
    """The virtual desktop geometry is absent or malformed."""


def _integer_geometry(monitor: Mapping[str, Any]) -> dict[str, int]:
    geometry: dict[str, int] = {}
    for field in ("left", "top", "width", "height"):
        value = monitor.get(field)
        if isinstance(value, bool) or not isinstance(value, Integral):
            raise DesktopCaptureError(f"virtual desktop {field} must be an integer")
        parsed = int(value)
        if field in {"width", "height"} and parsed <= 0:
            raise DesktopCaptureError(f"virtual desktop {field} must be positive")
        geometry[field] = parsed
    return geometry


@dataclass(frozen=True)
class DesktopCaptureScope:
    """Map global native input into the combined MSS frame coordinate space.

    MSS monitor zero is the bounding rectangle of all active monitors. Native
    input coordinates use the same global desktop origin on the supported
    platforms. Subtracting the combined rectangle's left/top therefore makes
    negative-origin and secondary-monitor input line up with the captured
    frame without a fabricated per-monitor scale.
    """

    left: int
    top: int
    width: int
    height: int
    monitors: tuple[dict[str, int], ...]
    _topology_reader: Callable[[], Iterable[Mapping[str, Any]]] | None = dataclass_field(
        default=None,
        compare=False,
        repr=False,
    )
    _topology_lock: threading.Lock = dataclass_field(
        default_factory=threading.Lock,
        compare=False,
        repr=False,
    )
    _last_topology_check: list[float] = dataclass_field(
        default_factory=lambda: [0.0],
        compare=False,
        repr=False,
    )

    @classmethod
    def from_monitors(
        cls,
        monitors: Iterable[Mapping[str, Any]],
        *,
        topology_reader: Callable[[], Iterable[Mapping[str, Any]]] | None = None,
    ) -> "DesktopCaptureScope":
        values = list(monitors)
        if len(values) < 2:
            raise DesktopCaptureError(
                "MSS did not report a combined desktop and a physical monitor"
            )
        combined = _integer_geometry(values[0])
        physical = tuple(_integer_geometry(monitor) for monitor in values[1:])
        combined_right = combined["left"] + combined["width"]
        combined_bottom = combined["top"] + combined["height"]
        if any(
            monitor["left"] < combined["left"]
            or monitor["top"] < combined["top"]
            or monitor["left"] + monitor["width"] > combined_right
            or monitor["top"] + monitor["height"] > combined_bottom
            for monitor in physical
        ):
            raise DesktopCaptureError(
                "a physical monitor falls outside the combined virtual desktop"
            )
        physical_bounds = (
            min(monitor["left"] for monitor in physical),
            min(monitor["top"] for monitor in physical),
            max(monitor["left"] + monitor["width"] for monitor in physical),
            max(monitor["top"] + monitor["height"] for monitor in physical),
        )
        if physical_bounds != (
            combined["left"],
            combined["top"],
            combined_right,
            combined_bottom,
        ):
            raise DesktopCaptureError("physical monitors do not span the combined virtual desktop")
        return cls(
            left=combined["left"],
            top=combined["top"],
            width=combined["width"],
            height=combined["height"],
            monitors=physical,
            _topology_reader=topology_reader,
        )

    @classmethod
    def current(cls) -> "DesktopCaptureScope":
        """Read and retain a live-check contract for the combined desktop."""

        return cls.from_monitors(
            _read_current_monitors(),
            topology_reader=_read_current_monitors,
        )

    def assert_current(self, *, force: bool = False) -> None:
        """Reject any display topology change after recording starts.

        A topology can change its origin or monitor layout without changing the
        combined frame size. A size-only video check cannot detect that case,
        and stale translation would bind input to the wrong pixels.
        """

        if self._topology_reader is None:
            return
        with self._topology_lock:
            now = time.monotonic()
            if not force and now - self._last_topology_check[0] < 0.05:
                return
            current = type(self).from_monitors(self._topology_reader())
            if current != self:
                raise DesktopCaptureError(
                    "virtual desktop topology changed during recording; "
                    f"expected {self.snapshot()}, got {current.snapshot()}"
                )
            self._last_topology_check[0] = now

    def translate(self, x: float, y: float) -> tuple[float, float]:
        """Translate global input to combined-frame pixels."""

        self.assert_current()
        return (x - self.left, y - self.top)

    def snapshot(self) -> dict[str, Any]:
        """Return privacy-safe topology metadata retained with the session."""

        return {
            "coordinate_space": "virtual_desktop_pixels",
            "origin": [self.left, self.top],
            "viewport": [self.width, self.height],
            "monitor_count": len(self.monitors),
            "monitors": [
                [
                    monitor["left"],
                    monitor["top"],
                    monitor["width"],
                    monitor["height"],
                ]
                for monitor in self.monitors
            ],
        }


def _read_current_monitors() -> list[Mapping[str, Any]]:
    """Read fresh MSS topology without reusing its cached monitor inventory."""

    # Lazy import preserves the headless-import contract. MSS caches its
    # ``monitors`` property for the lifetime of an instance, so a process-local
    # screenshot handle cannot detect a hot-plug or same-size rearrangement.
    import mss

    with mss.mss() as capture:
        return list(capture.monitors)

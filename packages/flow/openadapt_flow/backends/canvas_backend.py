"""No-DOM HTML5 <canvas> browser backend for VDI / Citrix Workspace-web / noVNC.

Promoted from ``benchmark/canvas_ladder/`` into ``openadapt_flow.backends`` so
canvas-class remote sessions are first-class backends selectable via
``backend.kind = "canvas"`` in :func:`openadapt_flow.backends.factory.build_backend`.

Implements only the base :class:`openadapt_flow.backend.Backend` protocol plus
the remote actuation lease protocols (:class:`RemoteActuationBackend` and
:class:`PreparedPointerActuationBackend`). It intentionally does NOT implement
``StructuralBackend``, ``IdentityBackend``, or ``StructuralActionBackend``
because an HTML5 ``<canvas>`` exposes no accessible DOM inside the remote
framebuffer.

Capabilities:
- Configurable ``<canvas>`` CSS selector and live bounding-box targeting.
- DPI and letterbox coordinate scaling between remote framebuffer pixels and
  displayed CSS bounding-box coordinates.
- Optional readiness probe and deployment markers checked before actuation.
- Mixed DOM-tab + canvas-tab routing via :class:`MixedCanvasDOMBackend`.
- Safe host-clipboard paste with exact readback and guaranteed scrub via
  :meth:`CanvasBrowserBackend.paste_and_verify`.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from typing import Any, Callable, Optional

from PIL import Image

from openadapt_flow.backends.clipboard_paste import (
    ClipboardAdapter,
    PasteVerifyResult,
    paste_and_verify,
)

__all__ = [
    "CanvasBrowserBackend",
    "CanvasGeometry",
    "MixedCanvasDOMBackend",
]

# Playwright press() token map for named keys / simple chords.
_PRESS_MAP = {
    "enter": "Enter",
    "tab": "Tab",
    "escape": "Escape",
    "esc": "Escape",
    "backspace": "Backspace",
    "delete": "Delete",
    "space": "Space",
    "home": "Home",
    "end": "End",
    "pageup": "PageUp",
    "pagedown": "PageDown",
    "up": "ArrowUp",
    "down": "ArrowDown",
    "left": "ArrowLeft",
    "right": "ArrowRight",
}


@dataclass(frozen=True)
class CanvasGeometry:
    """Resolved CSS bounding box and framebuffer mapping for a <canvas>."""

    box_x: float
    box_y: float
    box_width: float
    box_height: float
    content_x: float
    content_y: float
    content_width: float
    content_height: float
    scale_x: float
    scale_y: float

    def framebuffer_to_css(self, x: int, y: int) -> tuple[float, float]:
        """Map a framebuffer pixel coordinate into page CSS coordinates."""
        return (
            self.content_x + float(x) * self.scale_x,
            self.content_y + float(y) * self.scale_y,
        )


def _compute_canvas_geometry(
    bbox: tuple[float, float, float, float],
    fb_width: int,
    fb_height: int,
    *,
    letterbox: bool = True,
) -> CanvasGeometry:
    """Compute CSS content rect and scale factors from a canvas bounding box."""
    ox, oy, bw, bh = bbox
    if bw <= 0 or bh <= 0:
        raise RuntimeError(f"canvas bounding box has non-positive size: {bbox!r}")
    if fb_width <= 0 or fb_height <= 0:
        raise ValueError(f"invalid framebuffer size: {(fb_width, fb_height)!r}")

    if not letterbox:
        return CanvasGeometry(
            box_x=ox,
            box_y=oy,
            box_width=bw,
            box_height=bh,
            content_x=ox,
            content_y=oy,
            content_width=bw,
            content_height=bh,
            scale_x=bw / float(fb_width),
            scale_y=bh / float(fb_height),
        )

    scale = min(bw / float(fb_width), bh / float(fb_height))
    content_w = float(fb_width) * scale
    content_h = float(fb_height) * scale
    pad_x = (bw - content_w) / 2.0
    pad_y = (bh - content_h) / 2.0
    return CanvasGeometry(
        box_x=ox,
        box_y=oy,
        box_width=bw,
        box_height=bh,
        content_x=ox + pad_x,
        content_y=oy + pad_y,
        content_width=content_w,
        content_height=content_h,
        scale_x=scale,
        scale_y=scale,
    )


class CanvasBrowserBackend:
    """Backend over a no-DOM HTML5 ``<canvas>`` rendered inside a Playwright page.

    Implements ``Backend``, ``RemoteActuationBackend``, and
    ``PreparedPointerActuationBackend``.
    """

    def __init__(
        self,
        page: Any,
        *,
        width: int = 1280,
        height: int = 800,
        canvas_selector: str = "canvas",
        letterbox: bool = True,
        device_pixel_ratio: Optional[float] = None,
        readiness_probe: Optional[Callable[[bytes], bool]] = None,
        application_marker: Optional[str] = None,
        application_version_marker: Optional[str] = None,
        environment_marker: Optional[str] = None,
        session_marker: Optional[str] = None,
    ) -> None:
        self._page = page
        self._w, self._h = int(width), int(height)
        self._canvas_selector = canvas_selector
        self._canvas = page.locator(canvas_selector).first
        self._letterbox = bool(letterbox)
        self._device_pixel_ratio = device_pixel_ratio
        self._readiness_probe = readiness_probe
        self.application_marker = application_marker
        self.application_version_marker = application_version_marker
        self.environment_marker = environment_marker
        self.session_marker = session_marker
        self._actuation_digest: Optional[str] = None
        self._actuation_bbox: Optional[tuple[float, float, float, float]] = None
        self._prepared_pointer: Optional[tuple[int, int]] = None

    @property
    def page(self) -> Any:
        return self._page

    @property
    def canvas_selector(self) -> str:
        return self._canvas_selector

    def _bbox(self) -> tuple[float, float, float, float]:
        bb = self._canvas.bounding_box()
        if bb is None:
            raise RuntimeError(
                f"canvas element ({self._canvas_selector!r}) not found or not visible"
            )
        return float(bb["x"]), float(bb["y"]), float(bb["width"]), float(bb["height"])

    def geometry(self) -> CanvasGeometry:
        """Return the current CSS-to-framebuffer geometry for the target canvas."""
        return _compute_canvas_geometry(
            self._bbox(), self._w, self._h, letterbox=self._letterbox
        )

    def _map_point(self, x: int, y: int) -> tuple[float, float]:
        if not (0 <= int(x) < self._w and 0 <= int(y) < self._h):
            raise RuntimeError(
                f"canvas coordinate ({x}, {y}) is outside framebuffer {(self._w, self._h)}"
            )
        return self.geometry().framebuffer_to_css(int(x), int(y))

    @staticmethod
    def _frame_digest(png: bytes) -> str:
        """Hash decoded pixels, not PNG encoder metadata."""
        image = Image.open(io.BytesIO(png)).convert("RGB")
        return hashlib.sha256(
            image.width.to_bytes(4, "big")
            + image.height.to_bytes(4, "big")
            + image.tobytes()
        ).hexdigest()

    def check_readiness(self, png: Optional[bytes] = None) -> bool:
        """Verify that the canvas frame satisfies the configured readiness probe."""
        if self._readiness_probe is None:
            return True
        frame = png if png is not None else self.screenshot()
        if not self._readiness_probe(frame):
            raise RuntimeError(
                "canvas readiness probe failed (session locked, disconnected, or wrong screen)"
            )
        return True

    def _consume_actuation_lease(
        self,
        *,
        point: Optional[tuple[int, int]] = None,
        require_prepared_pointer: bool = False,
    ) -> None:
        """Recheck the exact canvas frame before the first input edge."""
        if self._actuation_digest is None or self._actuation_bbox is None:
            return
        try:
            if require_prepared_pointer and self._prepared_pointer != point:
                raise RuntimeError(
                    "consequential canvas click was not pre-positioned at the "
                    "freshly resolved target"
                )
            if self._bbox() != self._actuation_bbox:
                raise RuntimeError(
                    "canvas geometry changed after fresh-frame resolution"
                )
            current = self.screenshot()
            if self._frame_digest(current) != self._actuation_digest:
                raise RuntimeError(
                    "canvas content changed after fresh-frame resolution"
                )
            self.check_readiness(current)
        finally:
            # A consequential lease is one-shot even when it refuses.
            self._actuation_digest = None
            self._actuation_bbox = None

    # -- Backend protocol ----------------------------------------------------
    @property
    def viewport(self) -> tuple[int, int]:
        return (self._w, self._h)

    def screenshot(self) -> bytes:
        raw = self._canvas.screenshot()
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        if img.size != (self._w, self._h):
            # Handle letterbox cropping when the captured image aspect ratio
            # differs from the remote framebuffer, then resize for DPI scaling.
            if self._letterbox and abs(img.width - self._w) > 2 or abs(img.height - self._h) > 2:
                geom = _compute_canvas_geometry(
                    (0.0, 0.0, float(img.width), float(img.height)),
                    self._w,
                    self._h,
                    letterbox=self._letterbox,
                )
                left = int(round(geom.content_x))
                top = int(round(geom.content_y))
                right = min(img.width, int(round(geom.content_x + geom.content_width)))
                bottom = min(img.height, int(round(geom.content_y + geom.content_height)))
                if right > left and bottom > top:
                    img = img.crop((left, top, right, bottom))
                if img.size != (self._w, self._h):
                    resampling = getattr(Image, "Resampling", Image)
                    img = img.resize((self._w, self._h), resampling.LANCZOS)
            else:
                # Off-by-one sub-pixel bounds from Playwright element capture.
                img = img.crop((0, 0, self._w, self._h))
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()

    def prepare_pointer_actuation(self, x: int, y: int) -> None:
        """Move before the final frame so remote hover/cursor paint can settle."""
        self._actuation_digest = None
        self._actuation_bbox = None
        px, py = self._map_point(int(x), int(y))
        self._page.mouse.move(px, py)
        previous: Optional[str] = None
        stable = 0
        for _ in range(8):
            self._page.wait_for_timeout(100)
            digest = self._frame_digest(self.screenshot())
            if digest == previous:
                stable += 1
            else:
                previous = digest
                stable = 1
            if stable >= 2:
                self._prepared_pointer = (int(x), int(y))
                return
        self._prepared_pointer = None
        raise RuntimeError("canvas did not settle after pointer positioning")

    def acquire_actuation_frame(self) -> bytes:
        """Arm a one-shot exact-frame lease for consequential remote input."""
        png = self.screenshot()
        self.check_readiness(png)
        self._actuation_bbox = self._bbox()
        self._actuation_digest = self._frame_digest(png)
        return png

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        leased = self._actuation_digest is not None
        if leased:
            self._consume_actuation_lease(
                point=(int(x), int(y)), require_prepared_pointer=True
            )
            count = 2 if double else 1
            for index in range(count):
                self._page.mouse.down(button="left", click_count=index + 1)
                self._page.mouse.up(button="left", click_count=index + 1)
            self._prepared_pointer = None
        else:
            px, py = self._map_point(int(x), int(y))
            self._page.mouse.click(px, py, click_count=2 if double else 1)
        self._page.wait_for_timeout(120)

    def type_text(self, text: str) -> None:
        self._consume_actuation_lease()
        self._page.keyboard.type(text, delay=35)
        self._page.wait_for_timeout(120)

    def press(self, key: str) -> None:
        parts = [p.strip() for p in key.split("+") if p.strip()]
        mapped = []
        for p in parts:
            low = p.lower()
            if low in ("ctrl", "control", "controlormeta"):
                # Resolve the portable local-host shortcut token to the remote
                # session's Control modifier rather than host-OS Meta.
                mapped.append("Control")
            elif low in ("meta", "cmd", "super"):
                mapped.append("Meta")
            elif low in ("alt", "option"):
                mapped.append("Alt")
            elif low == "shift":
                mapped.append("Shift")
            else:
                mapped.append(_PRESS_MAP.get(low, p if len(p) > 1 else p))
        self._consume_actuation_lease()
        self._page.keyboard.press("+".join(mapped))
        self._page.wait_for_timeout(80)

    def scroll(self, dx: int, dy: int) -> None:
        self._page.mouse.wheel(int(dx), int(dy))
        self._page.wait_for_timeout(80)

    def paste_and_verify(
        self,
        text: str,
        *,
        clipboard: Optional[ClipboardAdapter] = None,
        verify_mode: str = "select_all_copy",
        ocr_reader: Optional[
            Callable[[bytes, Optional[tuple[int, int, int, int]]], str]
        ] = None,
        region: Optional[tuple[int, int, int, int]] = None,
        settle_s: float = 0.0,
    ) -> PasteVerifyResult:
        """Paste ``text`` into the remote canvas session and scrub the host clipboard."""
        return paste_and_verify(
            self,
            text,
            clipboard=clipboard,
            paste_chord="Control+v",
            select_all_chord="Control+a",
            copy_chord="Control+c",
            verify_mode=verify_mode,
            ocr_reader=ocr_reader,
            region=region,
            settle_s=settle_s,
        )


class MixedCanvasDOMBackend:
    """Router for runs that span both standard DOM tabs and ``<canvas>`` tabs.

    Holds a DOM backend (for normal web pages) and a :class:`CanvasBrowserBackend`
    (for no-DOM canvas VDI tabs), switching the active delegate explicitly via
    :meth:`use_dom_tab` / :meth:`use_canvas_tab` or automatically via
    :meth:`select_tab`.
    """

    def __init__(
        self,
        *,
        dom_backend: Any,
        canvas_backend: CanvasBrowserBackend,
        initial_mode: str = "dom",
    ) -> None:
        if initial_mode not in ("dom", "canvas"):
            raise ValueError(f"initial_mode must be 'dom' or 'canvas', got {initial_mode!r}")
        self._dom = dom_backend
        self._canvas = canvas_backend
        self._mode = initial_mode

    @property
    def active_mode(self) -> str:
        return self._mode

    @property
    def active_backend(self) -> Any:
        return self._canvas if self._mode == "canvas" else self._dom

    def use_dom_tab(self, dom_backend: Optional[Any] = None) -> None:
        if dom_backend is not None:
            self._dom = dom_backend
        self._mode = "dom"

    def use_canvas_tab(
        self, canvas_backend: Optional[CanvasBrowserBackend] = None
    ) -> None:
        if canvas_backend is not None:
            self._canvas = canvas_backend
        self._mode = "canvas"

    def select_tab(self, page: Any, *, canvas_selector: Optional[str] = None) -> str:
        """Inspect ``page`` and route to canvas mode if a visible canvas matches."""
        selector = canvas_selector or self._canvas.canvas_selector
        has_canvas = False
        try:
            locator = page.locator(selector)
            first = getattr(locator, "first", locator)
            bb = first.bounding_box()
            has_canvas = bool(bb and bb.get("width", 0) > 0 and bb.get("height", 0) > 0)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            has_canvas = False
        if has_canvas:
            if page is not self._canvas.page:
                self._canvas = CanvasBrowserBackend(
                    page,
                    width=self._canvas.viewport[0],
                    height=self._canvas.viewport[1],
                    canvas_selector=selector,
                    letterbox=self._canvas._letterbox,
                    readiness_probe=self._canvas._readiness_probe,
                )
            self._mode = "canvas"
        else:
            self._mode = "dom"
        return self._mode

    @property
    def viewport(self) -> tuple[int, int]:
        return self.active_backend.viewport

    def screenshot(self) -> bytes:
        return self.active_backend.screenshot()

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        self.active_backend.click(x, y, double=double)

    def type_text(self, text: str) -> None:
        self.active_backend.type_text(text)

    def press(self, key: str) -> None:
        self.active_backend.press(key)

    def scroll(self, dx: int, dy: int) -> None:
        self.active_backend.scroll(dx, dy)

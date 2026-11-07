"""A pixel-only backend over the scheduling fixture rendered by Chromium.

The replayer sees only screenshots and sends only clicks, keys and text, as
it would through a Citrix or RDP client window. The DOM is read by the
harness alone, after a run, to score which element each click landed on.

A :class:`Condition` sets how the same application is drawn:

- ``scale``: display scaling (Chromium's device scale factor). The layout is
  unchanged and every frame grows by the scale, as on a 125%/150%/200%
  Windows session or a HiDPI Citrix session.
- ``zoom``: application zoom. The frame keeps its size and the content grows
  and reflows inside it.
- ``font_delta_px``: a larger system font. Text grows; boxes around it grow
  with it and the layout reflows.
- ``dark``: a dark theme. Every colour changes, icons included.
- ``codec_quality``: each frame is JPEG-compressed with 4:2:0 chroma
  subsampling and decoded again, the way a VNC Tight or Citrix lossy codec
  delivers it.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from PIL import Image

FIXTURE = Path(__file__).resolve().parent / "fixture" / "emr.html"
CSS_VIEWPORT = (1280, 800)


@dataclass(frozen=True)
class Condition:
    name: str
    axis: str
    scale: float = 1.0
    zoom: Optional[float] = None
    font_delta_px: int = 0
    dark: bool = False
    codec_quality: Optional[int] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "axis": self.axis,
            "scale": self.scale,
            "zoom": self.zoom,
            "font_delta_px": self.font_delta_px,
            "dark": self.dark,
            "codec_quality": self.codec_quality,
        }


class FixtureSession:
    """One Chromium page showing the fixture under one condition."""

    def __init__(
        self,
        browser: Any,
        condition: Condition,
        *,
        order: Optional[list[int]] = None,
    ) -> None:
        self.condition = condition
        self.context = browser.new_context(
            viewport={"width": CSS_VIEWPORT[0], "height": CSS_VIEWPORT[1]},
            device_scale_factor=condition.scale,
        )
        self.page = self.context.new_page()
        url = FIXTURE.as_uri()
        if order is not None:
            url += "?order=" + ",".join(str(i) for i in order)
        self.page.goto(url)
        self.page.evaluate(
            "c => window.applyCondition(c)",
            {
                "dark": condition.dark,
                "font_delta_px": condition.font_delta_px,
                "zoom": condition.zoom,
            },
        )
        # Fonts and SVG symbols are local; one frame settles the layout.
        self.page.wait_for_timeout(50)

    def close(self) -> None:
        self.context.close()

    def clicks(self) -> list[Optional[str]]:
        return list(self.page.evaluate("window.__clicks"))

    def saved(self) -> list[dict[str, Any]]:
        return list(self.page.evaluate("window.__saved"))

    def drafts(self) -> list[dict[str, Any]]:
        return list(self.page.evaluate("window.__drafts"))

    def select_patient(self, index: int) -> None:
        self.page.evaluate("p => window.selectPatient(p)", index)

    def element_box(self, element_id: str) -> tuple[int, int, int, int]:
        """The element's box in screenshot pixels (scoring only)."""
        box = self.page.evaluate(
            """id => { const r = document.getElementById(id).getBoundingClientRect();
                       return [r.left, r.top, r.width, r.height]; }""",
            element_id,
        )
        s = self.condition.scale
        return tuple(int(round(v * s)) for v in box)  # type: ignore[return-value]


class PixelOnlyBackend:
    """The base ``Backend`` protocol and nothing else: no structural locate.

    Coordinates are screenshot pixels; they are divided by the display scale
    before Chromium receives them, as a remote-display client maps its window
    pixels to the session.
    """

    def __init__(
        self,
        session: FixtureSession,
        *,
        after_type: Optional[Callable[[FixtureSession, str], None]] = None,
    ) -> None:
        self.session = session
        self._after_type = after_type
        self._guarded: Optional[tuple[int, int]] = None
        self._last_png: Optional[bytes] = None

    @property
    def viewport(self) -> tuple[int, int]:
        s = self.session.condition.scale
        return (int(round(CSS_VIEWPORT[0] * s)), int(round(CSS_VIEWPORT[1] * s)))

    def screenshot(self) -> bytes:
        png = self.session.page.screenshot(type="png")
        quality = self.session.condition.codec_quality
        if quality is not None:
            png = codec_roundtrip(png, quality)
        self._last_png = png
        return png

    def _css(self, x: int, y: int) -> tuple[float, float]:
        s = self.session.condition.scale
        return (x / s, y / s)

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        cx, cy = self._css(int(x), int(y))
        self.session.page.mouse.click(cx, cy, click_count=2 if double else 1)

    def hover(self, x: int, y: int) -> None:
        cx, cy = self._css(int(x), int(y))
        self.session.page.mouse.move(cx, cy)

    def type_text(self, text: str) -> None:
        self.session.page.keyboard.type(text)
        if self._after_type is not None:
            self._after_type(self.session, text)

    def press(self, key: str) -> None:
        self.session.page.keyboard.press(key)

    def scroll(self, dx: int, dy: int) -> None:
        return None

    # -- guarded coordinate delivery (same contract as the kiosk backend) --
    def arm_guarded_coordinate(self, x: int, y: int) -> None:
        self._guarded = (int(x), int(y))

    def cancel_guarded_coordinate(self) -> None:
        self._guarded = None

    def act_guarded_coordinate(
        self,
        x: int,
        y: int,
        *,
        expected_frame_sha256: str,
        double: bool = False,
        button: str = "left",
    ) -> Any:
        from datetime import datetime, timezone

        from openadapt_flow.ir import ActionDeliveryReceipt

        if self._guarded != (int(x), int(y)):
            raise RuntimeError("guarded coordinate target was not armed")
        if hashlib.sha256(self.screenshot()).hexdigest() != expected_frame_sha256:
            raise RuntimeError("guarded coordinate frame changed")
        self._guarded = None
        self.click(x, y, double=double)
        return ActionDeliveryReceipt(
            receipt_id="fixture-guarded-click",
            operation="guarded_coordinate_click",
            native=False,
            delivered_at=datetime.now(timezone.utc).isoformat(),
        )


def codec_roundtrip(png: bytes, quality: int) -> bytes:
    """JPEG 4:2:0 at ``quality`` and back to PNG, as a lossy remote codec."""
    image = Image.open(io.BytesIO(png)).convert("RGB")
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=quality, subsampling=2)
    decoded = Image.open(io.BytesIO(buf.getvalue())).convert("RGB")
    out = io.BytesIO()
    decoded.save(out, format="PNG")
    return out.getvalue()

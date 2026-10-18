"""A pixel-only page driver, for desktop, RDP and Citrix surfaces.

:class:`~.agent.PageDriver` reads the browser's DOM. On a desktop app or a
remote session there is no DOM: only a window's pixels, and mouse and key
input at window coordinates. :class:`PixelDriver` gives the agent the same
observe-act interface over any Flow backend (``screenshot``, ``click``,
``type_text``): the screen is read with Flow's OCR, every recognised text
segment becomes a numbered control, and a click lands on its centre.

The scorer does not change with the surface. It reads the system of record,
so a rollout on RDP pixels is scored exactly like one on the DOM.

Surfaces in the box:

* ``dom``: :class:`~.agent.PageDriver` (the default);
* ``web_pixels``: Flow's :class:`~openadapt_flow.backends.playwright_backend.PlaywrightBackend`
  on the page, read through OCR only;
* any other backend: pass ``driver_factory=lambda page: PixelDriver(backend)``
  to the rollout, for example Flow's ``RemoteDisplayBackend`` over an RDP or
  Citrix client window.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Callable, Optional

from openadapt_flow.integrations.harness2.agent import Element, Observation

#: Seconds between frames while waiting for the screen to stop changing.
_FRAME_GAP_S = 0.25


class PixelDriver:
    """Observe through OCR, act at pixel coordinates, through a Flow backend."""

    def __init__(
        self,
        backend: Any,
        *,
        surface: str = "pixels",
        ocr: Optional[Callable[[bytes], list[Any]]] = None,
        max_settle_s: float = 6.0,
        min_settle_s: float = 0.5,
    ) -> None:
        self.backend = backend
        self.surface = surface
        self._ocr = ocr
        self.max_settle_s = max_settle_s
        self.min_settle_s = min_settle_s
        self._centers: dict[int, tuple[int, int]] = {}

    def observe(self) -> Observation:
        png = self.backend.screenshot()
        lines = [ln for ln in self._read(png) if str(ln.text).strip()]
        rows = _rows(lines)
        elements: list[Element] = []
        self._centers = {}
        text_rows: list[str] = []
        for row in rows:
            texts = [str(ln.text).strip() for ln in row]
            text_rows.append(" | ".join(texts))
            for i, ln in enumerate(row):
                n = len(elements) + 1
                x, y, w, h = ln.region
                self._centers[n] = (int(x + w / 2), int(y + h / 2))
                others = [t for j, t in enumerate(texts) if j != i]
                elements.append(
                    Element(
                        n=n,
                        tag="text",
                        id="",
                        label=texts[i],
                        context=" ".join(others) if len(row) > 1 else "",
                    )
                )
        return Observation("\n".join(text_rows), elements, where=self.surface)

    def act(self, action: dict[str, Any]) -> None:
        kind = action.get("action")
        if kind in ("click", "type"):
            n = int(action.get("element", -1))
            if n not in self._centers:
                raise ValueError(f"no control [{n}] on the last screen")
            self.backend.click(*self._centers[n])
            if kind == "type":
                # Clicking a field (or its label) focuses it; the text is typed
                # into it. Remote surfaces cannot be trusted to honour a
                # select-all chord, so this does not clear the field first.
                self.backend.type_text(str(action.get("text", "")))
        elif kind == "wait":
            time.sleep(1.5)
        else:
            raise ValueError(f"unknown action {kind!r}")
        self._settle()

    def _read(self, png: bytes) -> list[Any]:
        if self._ocr is not None:
            return self._ocr(png)
        from openadapt_flow.vision.ocr import ocr

        return ocr(png)

    def _settle(self) -> None:
        """Wait until two consecutive frames match, within ``max_settle_s``."""

        time.sleep(self.min_settle_s)
        deadline = time.monotonic() + self.max_settle_s
        last = hashlib.sha256(self.backend.screenshot()).digest()
        while time.monotonic() < deadline:
            time.sleep(_FRAME_GAP_S)
            now = hashlib.sha256(self.backend.screenshot()).digest()
            if now == last:
                return
            last = now


def _rows(lines: list[Any]) -> list[list[Any]]:
    """Group OCR segments into visual rows, top to bottom, left to right."""

    ordered = sorted(lines, key=lambda ln: (ln.region[1], ln.region[0]))
    rows: list[list[Any]] = []
    for ln in ordered:
        cy = ln.region[1] + ln.region[3] / 2
        if rows:
            ref = rows[-1][0]
            ref_cy = ref.region[1] + ref.region[3] / 2
            if abs(cy - ref_cy) <= 0.6 * min(ln.region[3], ref.region[3]):
                rows[-1].append(ln)
                continue
        rows.append([ln])
    return [sorted(row, key=lambda ln: ln.region[0]) for row in rows]


def web_pixels(page: Any) -> PixelDriver:
    """Flow's Playwright backend on ``page``, read through pixels only."""

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    return PixelDriver(
        PlaywrightBackend(page, screenshot_scale="css"), surface="web_pixels"
    )


__all__ = ["PixelDriver", "web_pixels"]

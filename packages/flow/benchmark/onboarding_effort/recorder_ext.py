"""The scripted recorder with ``record --identifier FIELD``.

``openadapt-flow record --identifier FIELD`` (interactive recorder) attaches
the rect of the first marked field present at each click as the click's
``identifier_region``; the compiler crops those pixels and arms the identity
check. The Python ``Recorder`` has no such option, so this subclass adds the
same field to click events, computed the same way, for a demonstration the
engineer marked.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from openadapt_flow.recorder import Recorder


class IdentifierRecorder(Recorder):
    def __init__(self, *args: Any, page: Any, identifiers: Sequence[str], **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._page = page
        self._identifiers = list(identifiers)

    def _identifier_rect(self) -> Optional[list[int]]:
        for selector in self._identifiers:
            loc = self._page.locator(selector).first
            try:
                if loc.count() and loc.is_visible():
                    box = loc.bounding_box()
                    if box:
                        return [int(box["x"]), int(box["y"]), int(box["width"]), int(box["height"])]
            except Exception:  # noqa: BLE001 - a field that just left the page
                continue
        return None

    def click(self, x: int, y: int) -> None:
        event: dict[str, Any] = {"kind": "click", "x": int(x), "y": int(y)}
        rect = self._identifier_rect()
        if rect:
            event["identifier_region"] = rect
        self._record(event, lambda: self._backend.click(int(x), int(y)))

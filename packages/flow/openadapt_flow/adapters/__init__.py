"""Adapters converting external demonstration sources into openadapt-flow
recordings (``meta.json`` + ``events.jsonl`` + ``frames/``).

Members are re-exported lazily so importing this package does not pull in cv2
(``convert_capture``) or the recorder until an adapter is actually used.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from openadapt_flow.adapters.capture import convert_capture
    from openadapt_flow.adapters.desktop_recorder import (
        record_desktop_demo,
        structural_armed_coverage,
    )

__all__ = [
    "convert_capture",
    "record_desktop_demo",
    "structural_armed_coverage",
]


def __getattr__(name: str) -> object:
    if name == "convert_capture":
        from openadapt_flow.adapters.capture import convert_capture

        return convert_capture
    if name in ("record_desktop_demo", "structural_armed_coverage"):
        from openadapt_flow.adapters import desktop_recorder

        return getattr(desktop_recorder, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

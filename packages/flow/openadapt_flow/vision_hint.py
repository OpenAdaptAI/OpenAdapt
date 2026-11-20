"""Name the install command when a CLI command needs the vision extra.

Since 3.0.0, NumPy, OpenCV, Pillow and RapidOCR are the ``vision`` extra. A
base install (openadapt-verify's dependency profile) has none of them, and the
record, compile and replay commands import them.
"""

from __future__ import annotations

import sys
from functools import wraps
from typing import Any, Callable

#: Top-level modules of the ``vision`` extra.
VISION_MODULES = frozenset({"cv2", "numpy", "PIL", "rapidocr_onnxruntime"})


def vision_hint(main: Callable[..., int]) -> Callable[..., int]:
    """Exit 2 with the install command when ``main`` needs a vision module."""

    @wraps(main)
    def entry(*args: Any, **kwargs: Any) -> int:
        try:
            return main(*args, **kwargs)
        except ModuleNotFoundError as exc:
            if (exc.name or "").split(".")[0] not in VISION_MODULES:
                raise
            print(
                "This command needs Flow's vision stack, which is not installed "
                f"(missing {exc.name}).\n"
                "Install it with: pip install 'openadapt-flow[vision]'",
                file=sys.stderr,
            )
            return 2

    return entry

"""Keep saved run frames from exposing credentials echoed by the page."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openadapt_flow.ir import Workflow


def persisted_run_frame(png: bytes, *, workflow: Workflow | None) -> bytes:
    """Return a same-size black PNG when this run handles secret parameters.

    The caller retains the original pixels for in-memory verification.
    This control does not depend on OCR or stable page layout.
    """
    if workflow is None or not workflow.secret_params:
        return png
    from PIL import Image

    with Image.open(io.BytesIO(png)) as frame:
        blank = Image.new("RGB", frame.size, (0, 0, 0))
        out = io.BytesIO()
        blank.save(out, format="PNG")
        return out.getvalue()

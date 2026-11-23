"""Keep saved run frames from exposing credentials echoed by the page."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openadapt_flow.ir import Workflow


def persisted_run_frame(png: bytes, *, workflow: Workflow | None) -> bytes:
    """Return a same-size black PNG when this run handles secret parameters
    or a one-time sign-in code.

    The caller retains the original pixels for in-memory verification.
    This control does not depend on OCR or stable page layout.
    """
    if workflow is None:
        return png
    if not workflow.secret_params:
        from openadapt_flow.second_factor import has_one_time_steps

        if not has_one_time_steps(workflow):
            return png
    from PIL import Image

    with Image.open(io.BytesIO(png)) as frame:
        blank = Image.new("RGB", frame.size, (0, 0, 0))
        out = io.BytesIO()
        blank.save(out, format="PNG")
        return out.getvalue()

"""Per-step model-call counts by placement, kept out of ``ir.py``.

:class:`~openadapt_flow.ir.StepResult` inherits these fields. The replayer's
egress recorder (:mod:`openadapt_flow.runtime.model_egress`) fills them:
identity-VLM calls that sent crops off the box, identity-VLM calls that ran in
process, and off-box grounder calls (abstentions and failed steps included).
Each field is omitted from the JSON when zero, so reports and receipts written
before these fields existed keep their bytes.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


def _count() -> Any:
    return Field(default=0, ge=0, exclude_if=lambda v: v == 0)


class ModelCallCounts(BaseModel):
    """Model calls one step made, split by where the frame went."""

    identity_vlm_calls: int = _count()
    local_identity_vlm_calls: int = _count()
    grounder_calls: int = _count()

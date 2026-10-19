"""Boundary interop between flow's compiler IR and ``openadapt-types``.

This subpackage lets a flow bundle speak the ecosystem's *canonical action
vocabulary* (``openadapt_types.Action`` / ``ActionType`` / ``ActionResult``)
at its boundaries (emit, benchmark/eval round-trip) WITHOUT dissolving flow's
internal :mod:`openadapt_flow.ir` schema. ``ir.py`` remains the source of
truth; this is an additive, optional shim (extra: ``openadapt-flow[interop]``).

See :mod:`openadapt_flow.interop.types` for the mapping details and the
compiler-only fields that are deliberately dropped at the boundary.
"""

from openadapt_flow.interop.types import (  # noqa: F401
    ACTION_KIND_TO_ACTION_TYPE,
    action_to_step,
    result_to_action_result,
    step_to_action,
)

__all__ = [
    "ACTION_KIND_TO_ACTION_TYPE",
    "action_to_step",
    "result_to_action_result",
    "step_to_action",
]

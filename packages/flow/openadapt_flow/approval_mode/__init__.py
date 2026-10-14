"""Approval modes for consequential saves.

Three modes decide whether a person approves an item before it is saved:

- ``each_item``: a person approves every item before its first save. This is
  the fail-safe value for a missing or unknown mode.
- ``problems_only``: "Only ask a person about problems". An item saves without
  a person only when every pre-save check passes, and it counts as done
  without a person only when the deployment's own system-of-record read-back
  confirms every save on the first read and the whole run used no repair,
  model call or rescue. Every other result goes to a person.
- ``not_gated``: no per-item pause. Callers that set it explicitly keep
  today's behavior, and the run report says so.

This package holds the runtime engine for those modes: the switch record and
its signed status, the runner's hash-chained switch log, the pre-save gate
(P1 to P5), the post-run checks (R1 and R2), the switch-off rules and the
requirements a workflow must meet before the switch can turn on.

The package root imports only the PHI-free data model so that ``ir`` can embed
the per-item evidence block without an import cycle. Import the runtime parts
from their modules: ``approval_mode.gate``, ``approval_mode.switch``,
``approval_mode.post``, ``approval_mode.coverage`` and
``approval_mode.requirements``.
"""

from openadapt_flow.approval_mode.model import (
    ApprovalMode,
    ApprovalModeItemEvidence,
    CheckOutcome,
    ItemContext,
    ItemDecisionKind,
    OffReason,
    SwitchThresholds,
    resolve_approval_mode,
)

__all__ = [
    "ApprovalMode",
    "ApprovalModeItemEvidence",
    "CheckOutcome",
    "ItemContext",
    "ItemDecisionKind",
    "OffReason",
    "SwitchThresholds",
    "resolve_approval_mode",
]

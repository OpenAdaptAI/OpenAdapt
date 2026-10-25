"""The governance-free core of step execution.

Code here decides HOW a step is carried out through a backend. It never
decides WHETHER it may be: authorization, approvals, qualification, profiles
and durable state are gates the caller passes in (see ``ActionContext``), so
the core can be tested against a backend alone and reused by any runner.

An import-linter contract (``packages/flow/.importlinter``) enforces the
boundary: nothing in this package may import ``runner``, ``hosted``,
``qualification*``, ``approval_mode``, ``jit`` or the replayer. See
docs/design/DECOMPOSITION.md, section 2.
"""

from openadapt_flow.runtime.core.actions import (
    ACTION_EXECUTORS,
    ActionContext,
    ActionExecutor,
)

__all__ = ["ACTION_EXECUTORS", "ActionContext", "ActionExecutor"]

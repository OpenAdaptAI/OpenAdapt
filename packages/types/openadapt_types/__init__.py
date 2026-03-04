"""openadapt-types: Canonical Pydantic schemas for computer-use agents.

This package provides the shared type definitions used across the OpenAdapt
ecosystem and designed for adoption by any computer-use agent project.

Quick start::

    from openadapt_types import ComputerState, Action, ActionType, UINode

    state = ComputerState(
        viewport=(1920, 1080),
        nodes=[
            UINode(node_id="n0", role="button", name="Submit"),
        ],
    )

    action = Action(
        type=ActionType.CLICK,
        target=ActionTarget(node_id="n0"),
    )
"""

from openadapt_types.action import (
    Action,
    ActionResult,
    ActionTarget,
    ActionType,
)
from openadapt_types.computer_state import (
    BoundingBox,
    ComputerState,
    ElementRole,
    ProcessInfo,
    UINode,
)
from openadapt_types.episode import Episode, Step
from openadapt_types.failure import FailureCategory, FailureRecord

__version__ = "0.1.0"

__all__ = [
    # computer_state
    "BoundingBox",
    "ComputerState",
    "ElementRole",
    "ProcessInfo",
    "UINode",
    # action
    "Action",
    "ActionResult",
    "ActionTarget",
    "ActionType",
    # episode
    "Episode",
    "Step",
    # failure
    "FailureCategory",
    "FailureRecord",
]

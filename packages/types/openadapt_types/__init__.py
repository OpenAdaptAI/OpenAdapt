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
from openadapt_types.benchmark import (
    BenchmarkAction,
    BenchmarkAgent,
    BenchmarkObservation,
    BenchmarkTask,
)
from openadapt_types.computer_state import (
    BoundingBox,
    ComputerState,
    ElementRole,
    ProcessInfo,
    UINode,
)
from openadapt_types.control_overlay import (
    CONTROL_OVERLAY_FRAME_SCHEMA,
    CONTROL_OVERLAY_STATE_ID_COMPONENTS,
    CONTROL_OVERLAY_STATUS_BY_PHASE,
    CONTROL_OVERLAY_TERMINAL_PHASES,
    CONTROL_OVERLAY_TIMELINE_SCHEMA,
    CONTROL_OVERLAY_WORKFLOW_LABEL_BY_MODE,
    ControlOverlayControlsV1,
    ControlOverlayDataClassification,
    ControlOverlayFrameV1,
    ControlOverlayMode,
    ControlOverlayPhase,
    ControlOverlayProfile,
    ControlOverlayStepV1,
    ControlOverlayTimelineBindingV1,
    ControlOverlayTimelineEventV1,
    ControlOverlayTimelineV1,
    ControlOverlayWorkflowLabel,
    build_control_overlay_timeline,
    control_overlay_state_id,
    is_terminal_control_overlay_phase,
)
from openadapt_types.episode import Episode, Step
from openadapt_types.failure import FailureCategory, FailureRecord
from openadapt_types.parsing import (
    from_benchmark_action,
    parse_action,
    parse_action_dsl,
    parse_action_json,
    to_benchmark_action_dict,
)

__version__ = "0.1.0"

__all__ = [
    # computer_state
    "BoundingBox",
    "ComputerState",
    "ElementRole",
    "ProcessInfo",
    "UINode",
    # control_overlay
    "CONTROL_OVERLAY_FRAME_SCHEMA",
    "CONTROL_OVERLAY_STATE_ID_COMPONENTS",
    "CONTROL_OVERLAY_STATUS_BY_PHASE",
    "CONTROL_OVERLAY_TERMINAL_PHASES",
    "CONTROL_OVERLAY_TIMELINE_SCHEMA",
    "CONTROL_OVERLAY_WORKFLOW_LABEL_BY_MODE",
    "ControlOverlayControlsV1",
    "ControlOverlayDataClassification",
    "ControlOverlayFrameV1",
    "ControlOverlayMode",
    "ControlOverlayPhase",
    "ControlOverlayProfile",
    "ControlOverlayStepV1",
    "ControlOverlayTimelineBindingV1",
    "ControlOverlayTimelineEventV1",
    "ControlOverlayTimelineV1",
    "ControlOverlayWorkflowLabel",
    "build_control_overlay_timeline",
    "control_overlay_state_id",
    "is_terminal_control_overlay_phase",
    # action
    "Action",
    "ActionResult",
    "ActionTarget",
    "ActionType",
    # benchmark
    "BenchmarkAction",
    "BenchmarkAgent",
    "BenchmarkObservation",
    "BenchmarkTask",
    # episode
    "Episode",
    "Step",
    # failure
    "FailureCategory",
    "FailureRecord",
    # parsing
    "from_benchmark_action",
    "parse_action",
    "parse_action_dsl",
    "parse_action_json",
    "to_benchmark_action_dict",
]

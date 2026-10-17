"""Action representation for GUI automation agents.

Converges three existing schemas:

- ``openadapt_ml.schema.episode.Action`` + ``ActionType``
  (24-type enum, Coordinates, element, raw)
- ``openadapt_evals.adapters.base.BenchmarkAction``
  (string-typed, flat x/y, target_node_id, target_bbox)
- ``omnimcp.types.LLMActionPlan`` / ``ActionDecision``
  (element_id-based, parameters dict)

Key design decisions:

- :class:`ActionTarget` separates *where to act* from *what to do*.
  Agents can specify ``node_id``, ``description``, or ``(x, y)`` coordinates.
  The runtime resolves to coordinates for execution.
- :class:`ActionResult` makes outcomes explicit — currently implicit
  (the next screenshot IS the result).

Schema version: 0.1.0
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class ActionType(str, Enum):
    """Typed action space for GUI automation.

    Superset of action types from openadapt-ml (24 types),
    openadapt-evals (string-typed), and omnimcp (5 types).
    """

    # Pointer
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK = "right_click"
    DRAG = "drag"
    SCROLL = "scroll"
    HOVER = "hover"

    # Keyboard
    TYPE = "type"
    KEY = "key"
    HOTKEY = "hotkey"

    # Navigation (web)
    GOTO = "goto"
    BACK = "back"
    FORWARD = "forward"
    REFRESH = "refresh"

    # System
    OPEN_APP = "open_app"
    CLOSE_APP = "close_app"
    WINDOW_FOCUS = "window_focus"
    WAIT = "wait"
    SCREENSHOT = "screenshot"

    # Meta
    DONE = "done"
    FAIL = "fail"
    ANSWER = "answer"


class ActionTarget(BaseModel):
    """Target specification for an action.

    Supports three grounding strategies (in priority order):

    1. ``node_id`` — element-based (preferred, most robust)
    2. ``description`` — natural language (resolved by grounding module)
    3. ``(x, y)`` — pixel coordinates (fallback)

    Agents SHOULD produce ``node_id`` or ``description``.
    The runtime resolves to coordinates for execution.
    """

    # Element-based (preferred)
    node_id: Optional[str] = Field(
        None, description="Target element node_id from ComputerState.nodes"
    )

    # Description-based (resolved by grounding module)
    description: Optional[str] = Field(
        None, description="Natural language target (e.g., 'the submit button')"
    )

    # Coordinate-based (fallback or resolved from above)
    x: Optional[float] = Field(None, description="X coordinate")
    y: Optional[float] = Field(None, description="Y coordinate")
    is_normalized: bool = Field(
        False, description="True if x/y are in [0, 1] range, False if pixels"
    )


class Action(BaseModel):
    """A single agent action.

    Converges:
    - ``openadapt_ml.schema.episode.Action``
    - ``openadapt_evals.adapters.base.BenchmarkAction``
    - ``omnimcp.types.ActionDecision``
    """

    type: ActionType
    target: Optional[ActionTarget] = Field(None, description="Where to act")

    # Keyboard parameters
    text: Optional[str] = Field(None, description="Text to type")
    key: Optional[str] = Field(None, description="Key to press (e.g., 'enter')")
    modifiers: Optional[list[str]] = Field(
        None, description="Modifier keys (e.g., ['ctrl', 'shift'])"
    )

    # Scroll parameters
    scroll_direction: Optional[Literal["up", "down", "left", "right"]] = None
    scroll_amount: Optional[int] = Field(None, description="Scroll amount in pixels")

    # Drag parameters
    drag_end: Optional[ActionTarget] = Field(None, description="Drag destination")

    # Navigation
    url: Optional[str] = None
    app_name: Optional[str] = None

    # Wait
    duration_seconds: Optional[float] = None

    # Answer (benchmarks that score by answer)
    answer: Optional[str] = None

    # Agent reasoning (preserved for training data)
    reasoning: Optional[str] = Field(None, description="Agent chain-of-thought")

    # Raw / original format (lossless)
    raw: Optional[dict[str, Any]] = Field(None, description="Source-specific raw data")

    @model_validator(mode="after")
    def _validate_params(self) -> "Action":
        if self.type == ActionType.TYPE and not self.text:
            raise ValueError("TYPE action requires 'text'")
        if self.type == ActionType.KEY and not self.key:
            raise ValueError("KEY action requires 'key'")
        if self.type == ActionType.GOTO and not self.url:
            raise ValueError("GOTO action requires 'url'")
        if self.type == ActionType.DRAG and not self.drag_end:
            raise ValueError("DRAG action requires 'drag_end'")
        return self


class ActionResult(BaseModel):
    """Result of executing an action.

    New schema — nothing equivalent exists in the current codebase.
    Currently action results are implicit (the next observation IS the result).
    Making results explicit enables error taxonomy, state deltas, and audit trails.
    """

    success: bool = Field(description="Whether the action executed without error")
    error: Optional[str] = Field(None, description="Error message if failed")
    error_type: Optional[Literal[
        "grounding_error",
        "execution_error",
        "state_mismatch",
        "timeout",
        "permission_denied",
        "infrastructure_error",
    ]] = None
    duration_ms: Optional[int] = Field(None, description="Execution time in ms")

    # What changed
    changed_node_ids: list[str] = Field(
        default_factory=list, description="Node IDs that changed after execution"
    )

    # Resolved coordinates (for auditing element-based actions)
    resolved_coordinates: Optional[tuple[int, int]] = Field(
        None, description="Final pixel coordinates used for execution"
    )

    raw: Optional[dict[str, Any]] = None

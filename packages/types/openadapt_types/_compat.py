"""Backward-compatibility converters for existing schemas.

These functions convert from the three existing schema formats to
the canonical ``openadapt-types`` models. They allow gradual migration:
old code keeps working, new code uses ``openadapt-types`` directly.

Usage::

    from openadapt_types._compat import (
        from_benchmark_observation,
        from_benchmark_action,
        from_ml_observation,
        from_ml_action,
        from_omnimcp_screen_state,
        from_omnimcp_action_decision,
    )

These converters work with plain dicts (``.__dict__`` or ``asdict()``),
not imported types, to avoid circular dependencies.
"""

from __future__ import annotations

from typing import Any, Optional

from .action import Action, ActionTarget, ActionType
from .computer_state import BoundingBox, ComputerState, UINode


# =====================================================================
# From openadapt_evals.adapters.base (dataclass dicts)
# =====================================================================


def from_benchmark_observation(obs: dict[str, Any]) -> ComputerState:
    """Convert a ``BenchmarkObservation.__dict__`` to :class:`ComputerState`.

    Maps:
    - ``screenshot`` → ``screenshot_png``
    - ``screenshot_path`` → ``screenshot_path``
    - ``viewport`` → ``viewport``
    - ``accessibility_tree`` → ``accessibility_tree_raw``
    - ``dom_html`` → ``dom_html``
    - ``url``, ``window_title``, ``app_name`` → context fields
    - ``focused_element`` → ``focused_node_id`` (if dict with node_id)
    - ``raw_observation`` → ``raw``
    """
    focused = obs.get("focused_element")
    focused_id = None
    if isinstance(focused, dict):
        focused_id = focused.get("node_id")

    return ComputerState(
        screenshot_png=obs.get("screenshot"),
        screenshot_path=obs.get("screenshot_path"),
        viewport=obs.get("viewport"),
        accessibility_tree_raw=obs.get("accessibility_tree"),
        dom_html=obs.get("dom_html"),
        url=obs.get("url"),
        focused_node_id=focused_id,
        raw=obs.get("raw_observation"),
    )


def from_benchmark_action(act: dict[str, Any]) -> Action:
    """Convert a ``BenchmarkAction.__dict__`` to :class:`Action`.

    Maps:
    - ``type`` → ``ActionType`` (string to enum)
    - ``x``, ``y`` → ``ActionTarget.x``, ``ActionTarget.y``
    - ``target_node_id`` → ``ActionTarget.node_id``
    - ``text``, ``key``, ``modifiers`` → keyboard params
    - ``scroll_direction``, ``scroll_amount`` → scroll params
    - ``end_x``, ``end_y`` → ``drag_end``
    - ``answer`` → ``answer``
    - ``raw_action`` → ``raw``
    """
    action_type_str = act.get("type", "done")
    try:
        action_type = ActionType(action_type_str)
    except ValueError:
        action_type = ActionType.DONE

    # Build target
    target = None
    x = act.get("x")
    y = act.get("y")
    node_id = act.get("target_node_id")
    if x is not None or y is not None or node_id is not None:
        target = ActionTarget(node_id=node_id, x=x, y=y)

    # Build drag end
    drag_end = None
    end_x = act.get("end_x")
    end_y = act.get("end_y")
    if end_x is not None or end_y is not None:
        drag_end = ActionTarget(x=end_x, y=end_y)

    return Action(
        type=action_type,
        target=target,
        text=act.get("text"),
        key=act.get("key"),
        modifiers=act.get("modifiers"),
        scroll_direction=act.get("scroll_direction"),
        scroll_amount=int(act["scroll_amount"]) if act.get("scroll_amount") is not None else None,
        drag_end=drag_end,
        answer=act.get("answer"),
        raw=act.get("raw_action"),
    )


# =====================================================================
# From openadapt_ml.schema.episode (Pydantic dicts)
# =====================================================================


def from_ml_observation(obs: dict[str, Any]) -> ComputerState:
    """Convert an ``openadapt_ml.schema.episode.Observation.model_dump()``
    to :class:`ComputerState`."""
    return ComputerState(
        screenshot_path=obs.get("screenshot_path"),
        screenshot_base64=obs.get("screenshot_base64"),
        viewport=obs.get("screen_size"),
        accessibility_tree_raw=obs.get("a11y_tree"),
        dom_html=obs.get("dom"),
        url=obs.get("url"),
        timestamp=obs.get("timestamp"),
        raw=obs.get("raw"),
    )


def from_ml_action(act: dict[str, Any]) -> Action:
    """Convert an ``openadapt_ml.schema.episode.Action.model_dump()``
    to :class:`Action`."""
    action_type_str = act.get("type", "done")
    try:
        action_type = ActionType(action_type_str)
    except ValueError:
        action_type = ActionType.DONE

    # Coordinates
    target = None
    coords = act.get("coordinates")
    norm = act.get("normalized_coordinates")
    element = act.get("element")

    if coords:
        target = ActionTarget(x=float(coords["x"]), y=float(coords["y"]))
    elif norm:
        target = ActionTarget(x=norm[0], y=norm[1], is_normalized=True)
    elif element and element.get("element_id"):
        target = ActionTarget(node_id=element["element_id"])

    # Drag
    drag_end = None
    end_coords = act.get("end_coordinates")
    norm_end = act.get("normalized_end")
    if end_coords:
        drag_end = ActionTarget(x=float(end_coords["x"]), y=float(end_coords["y"]))
    elif norm_end:
        drag_end = ActionTarget(x=norm_end[0], y=norm_end[1], is_normalized=True)

    return Action(
        type=action_type,
        target=target,
        text=act.get("text"),
        key=act.get("key"),
        modifiers=act.get("modifiers"),
        scroll_direction=act.get("scroll_direction"),
        scroll_amount=act.get("scroll_amount"),
        drag_end=drag_end,
        url=act.get("url"),
        app_name=act.get("app_name"),
        duration_seconds=act.get("duration"),
        raw=act.get("raw"),
    )


# =====================================================================
# From omnimcp.types (dataclass dicts)
# =====================================================================


def from_omnimcp_screen_state(state: dict[str, Any]) -> ComputerState:
    """Convert an ``omnimcp.types.ScreenState`` (as dict) to :class:`ComputerState`.

    Maps:
    - ``elements`` → ``nodes`` (UIElement → UINode)
    - ``dimensions`` → ``viewport``
    - ``timestamp`` → ``timestamp``
    """
    nodes: list[UINode] = []
    for elem in state.get("elements", []):
        bbox = None
        bounds = elem.get("bounds")
        dims = state.get("dimensions", (1, 1))
        if bounds and len(bounds) == 4:
            # omnimcp bounds are normalized (x, y, w, h)
            vw, vh = dims
            bbox = BoundingBox(
                x=int(bounds[0] * vw),
                y=int(bounds[1] * vh),
                width=int(bounds[2] * vw),
                height=int(bounds[3] * vh),
            )
        nodes.append(UINode(
            node_id=str(elem.get("id", "")),
            role=elem.get("type", "unknown"),
            text=elem.get("content"),
            bbox=bbox,
            confidence=elem.get("confidence", 1.0),
            attributes=elem.get("attributes", {}),
        ))

    return ComputerState(
        nodes=nodes,
        viewport=state.get("dimensions"),
        timestamp=state.get("timestamp"),
        source="omnimcp",
    )


def from_omnimcp_action_decision(decision: dict[str, Any]) -> Action:
    """Convert an ``omnimcp.types.ActionDecision`` (as dict) to :class:`Action`.

    Maps:
    - ``action_type`` → ``ActionType``
    - ``target_element_id`` → ``ActionTarget.node_id``
    - ``parameters`` → keyboard/scroll/wait params
    - ``analysis_reasoning`` → ``reasoning``
    """
    action_type_str = decision.get("action_type", "done")
    type_map = {
        "click": ActionType.CLICK,
        "type": ActionType.TYPE,
        "scroll": ActionType.SCROLL,
        "press_key": ActionType.KEY,
        "wait": ActionType.WAIT,
        "finish": ActionType.DONE,
        "launch_app": ActionType.OPEN_APP,
    }
    action_type = type_map.get(action_type_str, ActionType.DONE)

    target = None
    elem_id = decision.get("target_element_id")
    if elem_id is not None:
        target = ActionTarget(node_id=str(elem_id))

    params = decision.get("parameters", {})

    return Action(
        type=action_type,
        target=target,
        text=params.get("text_to_type"),
        key=params.get("key_info"),
        scroll_direction=params.get("scroll_direction"),
        scroll_amount=params.get("scroll_steps"),
        app_name=params.get("app_name"),
        duration_seconds=params.get("wait_duration_s"),
        reasoning=decision.get("analysis_reasoning"),
    )

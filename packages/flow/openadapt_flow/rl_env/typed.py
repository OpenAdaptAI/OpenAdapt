"""OpenAdapt's shared types and MCP tools for the RL environment.

Observations also carry an ``openadapt_types.ComputerState`` (the accessibility
nodes, the URL and the viewport) and each step's ``ActionResult``. An action may
be an ``openadapt_types.Action`` instead of the native fields, so an agent built
on those types needs no adapter.

The same action set is offered as MCP tools, following OpenEnv RFC 003:
``ListToolsAction`` lists them and ``CallToolAction`` runs one through
``step()``, so the episode's reward still comes back on the observation.
"""

from __future__ import annotations

from typing import Any, Optional

from openadapt_types.action import Action as ComputerAction
from openadapt_types.action import ActionResult, ActionType
from openadapt_types.computer_state import BoundingBox, ComputerState, UINode

#: The scroll step for a ``scroll`` action that names a direction but no amount.
SCROLL_STEP = 400


def computer_state(
    url: str, elements: list[dict[str, Any]], viewport: tuple[int, int]
) -> dict[str, Any]:
    """The observed elements as a ``ComputerState`` (node ids ``n0``, ``n1``...)."""
    nodes = []
    for i, el in enumerate(elements):
        w, h = int(el.get("w", 0)), int(el.get("h", 0))
        nodes.append(
            UINode.model_validate(
                {
                    "node_id": f"n{i}",
                    "role": str(el.get("type") or el.get("tag") or "generic"),
                    "name": el.get("label") or None,
                    "css_selector": el.get("selector"),
                    "bbox": BoundingBox(
                        x=max(0, int(el["x"] - w / 2)),
                        y=max(0, int(el["y"] - h / 2)),
                        width=w,
                        height=h,
                    ),
                    "is_editable": el.get("tag") in ("input", "textarea", "select"),
                }
            )
        )
    state = ComputerState.model_validate(
        {"url": url, "viewport": viewport, "nodes": nodes}
    )
    return state.model_dump(mode="json", exclude_none=True)


def native_action(
    raw: dict[str, Any], elements: list[dict[str, Any]], viewport: tuple[int, int]
) -> dict[str, Any]:
    """Translate an ``openadapt_types.Action`` into this environment's fields.

    A target names a node from the last observation (``n3``) or a point;
    normalized points are scaled to the viewport. ``done`` claims success and
    ``fail`` claims failure. Anything this environment cannot do raises
    ``ValueError``, which the agent sees as the step's error.
    """
    action = ComputerAction.model_validate(raw)
    out: dict[str, Any] = {}
    target = action.target
    if target is not None and target.node_id is not None:
        index = int(target.node_id.lstrip("n")) if target.node_id[1:].isdigit() else -1
        if not 0 <= index < len(elements) or not elements[index].get("selector"):
            raise ValueError(
                f"no addressable node {target.node_id!r} in the last observation"
            )
        out["selector"] = elements[index]["selector"]
    elif target is not None and target.x is not None and target.y is not None:
        x, y = target.x, target.y
        if target.is_normalized:
            x, y = x * viewport[0], y * viewport[1]
        out["x"], out["y"] = int(x), int(y)
    kind = action.type
    # A double click is not a click: refuse it rather than send one click.
    if kind == ActionType.CLICK:
        return {"kind": "click", **out}
    if kind == ActionType.TYPE:
        if "selector" in out:
            return {
                "kind": "fill",
                "selector": out["selector"],
                "text": action.text or "",
            }
        return {"kind": "type", "text": action.text or ""}
    if kind in (ActionType.KEY, ActionType.HOTKEY):
        keys = [*(action.modifiers or []), action.key or "Enter"]
        return {"kind": "press", "text": "+".join(keys)}
    if kind == ActionType.SCROLL:
        if action.scroll_direction not in (None, "up", "down"):
            raise ValueError(
                f"scroll direction {action.scroll_direction!r} is not available"
                " in this environment"
            )
        amount = action.scroll_amount or SCROLL_STEP
        return {
            "kind": "scroll",
            "dy": -amount if action.scroll_direction == "up" else amount,
        }
    if kind == ActionType.GOTO:
        return {"kind": "goto", "path": action.url or "/"}
    if kind == ActionType.WAIT:
        return {"kind": "wait", "seconds": action.duration_seconds or 1.0}
    if kind == ActionType.DONE:
        return {"kind": "done", "claim": "success"}
    if kind == ActionType.FAIL:
        return {"kind": "done", "claim": "failure"}
    raise ValueError(f"action type {kind.value!r} is not available in this environment")


def action_result(error: Optional[str], duration_ms: int) -> dict[str, Any]:
    return ActionResult.model_validate(
        {"success": error is None, "error": error, "duration_ms": duration_ms}
    ).model_dump(mode="json", exclude_none=True)


def _schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


_SELECTOR = {"type": "string", "description": "CSS selector from an observed element"}
_POINT = {"type": "integer", "description": "viewport pixels (1280x800)"}

#: The MCP tools: one per action kind, plus ``observe``.
TOOLS: list[dict[str, Any]] = [
    {
        "name": "click",
        "description": "Click an element by selector, or a point by x and y.",
        "input_schema": _schema({"selector": _SELECTOR, "x": _POINT, "y": _POINT}, []),
    },
    {
        "name": "fill",
        "description": "Replace the contents of an input with text.",
        "input_schema": _schema(
            {"selector": _SELECTOR, "text": {"type": "string"}}, ["selector", "text"]
        ),
    },
    {
        "name": "type",
        "description": "Type text at the focused element.",
        "input_schema": _schema({"text": {"type": "string"}}, ["text"]),
    },
    {
        "name": "press",
        "description": "Press a key, e.g. Enter or Control+a.",
        "input_schema": _schema({"text": {"type": "string"}}, ["text"]),
    },
    {
        "name": "select",
        "description": "Choose an option of a select element by value.",
        "input_schema": _schema(
            {"selector": _SELECTOR, "value": {"type": "string"}}, ["selector", "value"]
        ),
    },
    {
        "name": "scroll",
        "description": "Scroll the page by dy pixels (negative is up).",
        "input_schema": _schema({"dy": {"type": "integer"}}, ["dy"]),
    },
    {
        "name": "goto",
        "description": "Open a path or #fragment on the app's origin.",
        "input_schema": _schema({"path": {"type": "string"}}, ["path"]),
    },
    {
        "name": "wait",
        "description": "Wait up to 5 seconds.",
        "input_schema": _schema({"seconds": {"type": "number"}}, []),
    },
    {
        "name": "done",
        "description": (
            "End the episode. claim is success if the task's change is in the "
            "record exactly as asked, otherwise failure."
        ),
        "input_schema": _schema(
            {"claim": {"type": "string", "enum": ["success", "failure"]}}, ["claim"]
        ),
    },
    {
        "name": "observe",
        "description": "Return the current observation without acting.",
        "input_schema": _schema({}, []),
    },
]
TOOL_NAMES = frozenset(tool["name"] for tool in TOOLS)

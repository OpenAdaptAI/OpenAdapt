"""Universal action parser for DSL strings, JSON strings, and BenchmarkAction dicts.

Converts multiple input formats into the canonical :class:`openadapt_types.Action`
model.  Every public function is safe to call with arbitrary input -- malformed
data yields ``Action(type=ActionType.DONE)`` with a logged warning instead of
raising.

Supported input formats
-----------------------

1. **DSL** -- ``CLICK(x=0.50, y=0.30)``, optionally with ``Thought:`` / ``Action:``
   prefixes.
2. **JSON** -- ``{"type": "click", "target": {"x": 0.5, "y": 0.3}}`` and legacy
   flat / ``coordinate`` variants.
3. **BenchmarkAction dict** -- the flat dict format used by ``openadapt-evals``.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from pydantic import ValidationError

from openadapt_types.action import Action, ActionTarget, ActionType

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_ACTION_TYPE_MAP: dict[str, ActionType] = {member.value: member for member in ActionType}
# Also map upper-case names so "CLICK" -> ActionType.CLICK
_ACTION_TYPE_MAP.update({member.name: member for member in ActionType})


def _resolve_action_type(raw: str) -> Optional[ActionType]:
    """Resolve a string to an ActionType, case-insensitively."""
    key = raw.strip().lower()
    # Try value first ("click"), then name ("CLICK")
    result = _ACTION_TYPE_MAP.get(key) or _ACTION_TYPE_MAP.get(raw.strip().upper())
    return result


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _is_normalized(x: Optional[float], y: Optional[float]) -> bool:
    """Return True if both coordinates are in [0, 1]."""
    if x is None or y is None:
        return False
    return 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0


def _make_target(
    x: Optional[float],
    y: Optional[float],
    node_id: Optional[str] = None,
    description: Optional[str] = None,
    clamp: bool = True,
) -> Optional[ActionTarget]:
    """Build an ActionTarget from coordinates, clamping if requested."""
    if x is None and y is None and node_id is None and description is None:
        return None
    normalized = _is_normalized(x, y)
    if clamp and x is not None and y is not None:
        if normalized:
            x = _clamp(x, 0.0, 1.0)
            y = _clamp(y, 0.0, 1.0)
        else:
            x = max(0.0, x)
            y = max(0.0, y)
    return ActionTarget(
        x=x,
        y=y,
        is_normalized=normalized,
        node_id=node_id,
        description=description,
    )


def _done(reason: str) -> Action:
    """Return a DONE action and log a warning."""
    logger.warning("Falling back to DONE: %s", reason)
    return Action(type=ActionType.DONE)


def _safe_float(value: Any) -> Optional[float]:
    """Convert *value* to float or return None on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# DSL parser
# ---------------------------------------------------------------------------

# Regex: optional "Thought: ...\n" then optional "Action: " then NAME(args)
_THOUGHT_RE = re.compile(
    r"(?:Thought:\s*(?P<thought>.+?)(?:\n|$))?\s*(?:Action:\s*)?(?P<action_call>\w+\(.*\))\s*$",
    re.DOTALL | re.IGNORECASE,
)

# Fallback: just NAME(args) anywhere in the string
_ACTION_CALL_RE = re.compile(r"(?P<action_call>\w+\(.*\))\s*$", re.DOTALL | re.IGNORECASE)


def _parse_dsl_args(args_str: str) -> dict[str, str]:
    """Parse 'x=0.5, y=0.3, text="hello world"' into a dict.

    Handles quoted values (including escaped quotes) and bare values.
    """
    result: dict[str, str] = {}
    # Match key=value pairs where value is either quoted or bare
    pattern = re.compile(
        r"""(\w+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)'|([^,\)]+))""",
    )
    for m in pattern.finditer(args_str):
        key = m.group(1)
        # Prefer quoted groups, then bare
        value = m.group(2) if m.group(2) is not None else (
            m.group(3) if m.group(3) is not None else m.group(4).strip()
        )
        # Unescape
        value = value.replace('\\"', '"').replace("\\'", "'")
        result[key] = value
    return result


def parse_action_dsl(text: str) -> Action:
    """Parse a DSL-format action string into an :class:`Action`.

    Examples::

        CLICK(x=0.50, y=0.30)
        TYPE(text="hello world")
        Thought: I need to click the button\\nAction: CLICK(x=0.5, y=0.3)

    Parameters
    ----------
    text:
        Raw text potentially containing a DSL action call.

    Returns
    -------
    Action
        Parsed action, or ``Action(type=ActionType.DONE)`` on failure.
    """
    text = text.strip()
    if not text:
        return _done("empty DSL input")

    # Extract thought and action call
    thought: Optional[str] = None
    action_call: Optional[str] = None

    m = _THOUGHT_RE.search(text)
    if m:
        thought = m.group("thought")
        action_call = m.group("action_call")
    else:
        m2 = _ACTION_CALL_RE.search(text)
        if m2:
            action_call = m2.group("action_call")

    if not action_call:
        return _done(f"no DSL action call found in: {text!r}")

    # Split name and args
    paren_idx = action_call.index("(")
    action_name = action_call[:paren_idx].strip()
    args_str = action_call[paren_idx + 1 :].rstrip().rstrip(")")

    action_type = _resolve_action_type(action_name)
    if action_type is None:
        return _done(f"unknown action type: {action_name!r}")

    args = _parse_dsl_args(args_str)

    # Strip thought whitespace
    if thought:
        thought = thought.strip()

    try:
        return _build_action_from_parsed(action_type, args, reasoning=thought)
    except Exception as exc:
        return _done(f"failed to build action: {exc}")


def _build_action_from_parsed(
    action_type: ActionType,
    args: dict[str, str],
    reasoning: Optional[str] = None,
) -> Action:
    """Build an Action from a parsed action type and string arguments."""
    x = _safe_float(args.get("x"))
    y = _safe_float(args.get("y"))

    # Validate coordinates -- if they were supposed to be numbers but aren't
    if ("x" in args and x is None) or ("y" in args and y is None):
        return _done(f"malformed coordinates: x={args.get('x')!r}, y={args.get('y')!r}")

    kwargs: dict[str, Any] = {"type": action_type, "reasoning": reasoning}

    if action_type == ActionType.DRAG:
        end_x = _safe_float(args.get("end_x"))
        end_y = _safe_float(args.get("end_y"))
        if end_x is None or end_y is None:
            return _done("DRAG requires end_x and end_y")
        kwargs["target"] = _make_target(x, y)
        kwargs["drag_end"] = _make_target(end_x, end_y)
    elif x is not None and y is not None:
        kwargs["target"] = _make_target(x, y)

    if "text" in args:
        kwargs["text"] = args["text"]
    if "key" in args:
        kwargs["key"] = args["key"]
    if "direction" in args:
        kwargs["scroll_direction"] = args["direction"]
    if "url" in args:
        kwargs["url"] = args["url"]
    if "modifiers" in args:
        # "ctrl,shift" -> ["ctrl", "shift"]
        kwargs["modifiers"] = [m.strip() for m in args["modifiers"].split(",")]

    try:
        return Action(**kwargs)
    except ValidationError as exc:
        return _done(f"validation error: {exc}")


# ---------------------------------------------------------------------------
# JSON parser
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_JSON_OBJECT_RE = re.compile(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", re.DOTALL)


def parse_action_json(text: str) -> Action:
    """Parse a JSON-format action string into an :class:`Action`.

    Supports three JSON conventions:

    1. **Canonical**: ``{"type": "click", "target": {"x": 0.5, "y": 0.3}}``
    2. **Legacy flat**: ``{"type": "click", "x": 0.5, "y": 0.3}``
    3. **Legacy openadapt-ml**: ``{"action_type": "click", "coordinate": [0.5, 0.3]}``

    Also strips markdown fences and locates the first ``{...}`` object.

    Parameters
    ----------
    text:
        Raw text potentially containing JSON.

    Returns
    -------
    Action
        Parsed action, or ``Action(type=ActionType.DONE)`` on failure.
    """
    text = text.strip()
    if not text:
        return _done("empty JSON input")

    # Strip markdown fences
    fence_match = _FENCE_RE.search(text)
    if fence_match:
        text = fence_match.group(1)

    # Find first JSON object
    json_match = _JSON_OBJECT_RE.search(text)
    if not json_match:
        return _done(f"no JSON object found in: {text[:100]!r}")

    json_str = json_match.group(0)

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as exc:
        return _done(f"invalid JSON: {exc}")

    if not isinstance(data, dict):
        return _done(f"JSON is not an object: {type(data)}")

    return _parse_json_dict(data)


def _parse_json_dict(data: dict[str, Any]) -> Action:
    """Convert a parsed JSON dict into an Action."""
    # Normalize type field
    raw_type = data.get("type") or data.get("action_type") or data.get("action")
    if raw_type is None:
        return _done("JSON missing 'type' or 'action_type' field")

    action_type = _resolve_action_type(str(raw_type))
    if action_type is None:
        return _done(f"unknown action type in JSON: {raw_type!r}")

    # Extract reasoning from various field names
    reasoning = (
        data.get("reasoning")
        or data.get("thought")
        or data.get("thinking")
    )

    # Build target
    target: Optional[ActionTarget] = None
    drag_end: Optional[ActionTarget] = None

    # Canonical: nested target dict
    if "target" in data and isinstance(data["target"], dict):
        td = data["target"]
        target = _make_target(
            x=_safe_float(td.get("x")),
            y=_safe_float(td.get("y")),
            node_id=td.get("node_id"),
            description=td.get("description"),
        )
    # Legacy flat: x/y at top level
    elif "x" in data and "y" in data:
        target = _make_target(
            x=_safe_float(data.get("x")),
            y=_safe_float(data.get("y")),
        )
    # Legacy openadapt-ml: coordinate list
    elif "coordinate" in data and isinstance(data["coordinate"], (list, tuple)):
        coord = data["coordinate"]
        if len(coord) >= 2:
            target = _make_target(
                x=_safe_float(coord[0]),
                y=_safe_float(coord[1]),
            )

    # Drag end
    if "drag_end" in data and isinstance(data["drag_end"], dict):
        de = data["drag_end"]
        drag_end = _make_target(
            x=_safe_float(de.get("x")),
            y=_safe_float(de.get("y")),
        )
    elif "end_x" in data and "end_y" in data:
        drag_end = _make_target(
            x=_safe_float(data.get("end_x")),
            y=_safe_float(data.get("end_y")),
        )

    kwargs: dict[str, Any] = {
        "type": action_type,
        "reasoning": reasoning,
        "target": target,
    }

    if drag_end is not None:
        kwargs["drag_end"] = drag_end
    if "text" in data:
        kwargs["text"] = data["text"]
    if "key" in data:
        kwargs["key"] = data["key"]
    if "modifiers" in data:
        kwargs["modifiers"] = data["modifiers"]
    if "scroll_direction" in data or "direction" in data:
        kwargs["scroll_direction"] = data.get("scroll_direction") or data.get("direction")
    if "scroll_amount" in data:
        kwargs["scroll_amount"] = data["scroll_amount"]
    if "url" in data:
        kwargs["url"] = data["url"]
    if "app_name" in data:
        kwargs["app_name"] = data["app_name"]
    if "duration_seconds" in data:
        kwargs["duration_seconds"] = data["duration_seconds"]
    if "answer" in data:
        kwargs["answer"] = data["answer"]
    if "raw" in data:
        kwargs["raw"] = data["raw"]

    try:
        return Action(**kwargs)
    except ValidationError as exc:
        return _done(f"validation error: {exc}")


# ---------------------------------------------------------------------------
# Auto-detect parser
# ---------------------------------------------------------------------------


def parse_action(text: str) -> Action:
    """Auto-detect format and parse *text* into an :class:`Action`.

    Tries JSON first if the text looks like it contains JSON, otherwise
    tries DSL.  Falls back to ``Action(type=ActionType.DONE)`` on failure.

    Parameters
    ----------
    text:
        Raw text in DSL or JSON format.

    Returns
    -------
    Action
        Parsed action.
    """
    text = text.strip()
    if not text:
        return _done("empty input")

    # Heuristic: if it looks like JSON, try JSON first
    if "{" in text:
        result = parse_action_json(text)
        if result.type != ActionType.DONE:
            return result
        # JSON parse returned DONE -- might be DSL with a brace in argument
        # Try DSL as well
        dsl_result = parse_action_dsl(text)
        if dsl_result.type != ActionType.DONE:
            return dsl_result
        # Both failed, return the JSON result (first attempt)
        return result

    # No brace -- try DSL
    return parse_action_dsl(text)


# ---------------------------------------------------------------------------
# BenchmarkAction conversion
# ---------------------------------------------------------------------------


def from_benchmark_action(data: dict[str, Any]) -> Action:
    """Convert a BenchmarkAction-style dict into an :class:`Action`.

    BenchmarkAction fields: type, x, y, text, key, modifiers,
    scroll_direction, scroll_amount, end_x, end_y, target_node_id,
    target_bbox, target_role, target_name, answer, raw_action.

    Parameters
    ----------
    data:
        A dict with BenchmarkAction fields.

    Returns
    -------
    Action
        Converted action, or ``Action(type=ActionType.DONE)`` on failure.
    """
    if not isinstance(data, dict):
        return _done(f"expected dict, got {type(data)}")

    raw_type = data.get("type") or data.get("action_type")
    if raw_type is None:
        return _done("BenchmarkAction missing 'type' field")

    action_type = _resolve_action_type(str(raw_type))
    if action_type is None:
        return _done(f"unknown action type: {raw_type!r}")

    # Build target
    x = _safe_float(data.get("x"))
    y = _safe_float(data.get("y"))
    node_id = data.get("target_node_id")
    # Combine target_role and target_name into description
    description_parts = []
    if data.get("target_role"):
        description_parts.append(str(data["target_role"]))
    if data.get("target_name"):
        description_parts.append(str(data["target_name"]))
    description = " ".join(description_parts) if description_parts else None

    target = _make_target(x=x, y=y, node_id=node_id, description=description)

    # Build drag end
    drag_end: Optional[ActionTarget] = None
    end_x = _safe_float(data.get("end_x"))
    end_y = _safe_float(data.get("end_y"))
    if end_x is not None and end_y is not None:
        drag_end = _make_target(x=end_x, y=end_y)

    kwargs: dict[str, Any] = {
        "type": action_type,
        "target": target,
    }

    if drag_end is not None:
        kwargs["drag_end"] = drag_end
    if "text" in data and data["text"] is not None:
        kwargs["text"] = data["text"]
    if "key" in data and data["key"] is not None:
        kwargs["key"] = data["key"]
    if "modifiers" in data and data["modifiers"] is not None:
        kwargs["modifiers"] = data["modifiers"]
    if "scroll_direction" in data and data["scroll_direction"] is not None:
        kwargs["scroll_direction"] = data["scroll_direction"]
    if "scroll_amount" in data and data["scroll_amount"] is not None:
        kwargs["scroll_amount"] = data["scroll_amount"]
    if "answer" in data and data["answer"] is not None:
        kwargs["answer"] = data["answer"]
    if "raw_action" in data and data["raw_action"] is not None:
        kwargs["raw"] = data["raw_action"]

    try:
        return Action(**kwargs)
    except ValidationError as exc:
        return _done(f"validation error from BenchmarkAction: {exc}")


def to_benchmark_action_dict(action: Action) -> dict[str, Any]:
    """Convert an :class:`Action` back to a BenchmarkAction-style dict.

    Parameters
    ----------
    action:
        The canonical Action to convert.

    Returns
    -------
    dict
        A flat dict with BenchmarkAction field names.
    """
    result: dict[str, Any] = {"type": action.type.value}

    # Flatten target coordinates
    if action.target is not None:
        if action.target.x is not None:
            result["x"] = action.target.x
        if action.target.y is not None:
            result["y"] = action.target.y
        if action.target.node_id is not None:
            result["target_node_id"] = action.target.node_id
        if action.target.description is not None:
            # We store description, though round-trip from target_role+target_name
            # may lose the separation
            result["target_description"] = action.target.description

    # Flatten drag end
    if action.drag_end is not None:
        if action.drag_end.x is not None:
            result["end_x"] = action.drag_end.x
        if action.drag_end.y is not None:
            result["end_y"] = action.drag_end.y

    # Copy scalar fields
    if action.text is not None:
        result["text"] = action.text
    if action.key is not None:
        result["key"] = action.key
    if action.modifiers is not None:
        result["modifiers"] = action.modifiers
    if action.scroll_direction is not None:
        result["scroll_direction"] = action.scroll_direction
    if action.scroll_amount is not None:
        result["scroll_amount"] = action.scroll_amount
    if action.answer is not None:
        result["answer"] = action.answer
    if action.raw is not None:
        result["raw_action"] = action.raw

    return result

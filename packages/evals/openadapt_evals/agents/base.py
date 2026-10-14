"""Base agent interface for benchmark evaluation.

This module provides the BenchmarkAgent abstract base class that agents
must implement to be evaluated on benchmarks.

Example:
    from openadapt_evals.agents import BenchmarkAgent

    class MyAgent(BenchmarkAgent):
        def act(self, observation, task, history=None):
            # Your agent logic here
            return BenchmarkAction(type="click", x=0.5, y=0.5)
"""

from __future__ import annotations

import re
from typing import Any

from openadapt_types import BenchmarkAgent

from openadapt_evals.adapters.base import (
    BenchmarkAction,
    BenchmarkObservation,
)

# The BenchmarkAgent interface and the data classes come from the canonical
# schema package (openadapt-types); the data classes reach this module via
# openadapt_evals.adapters.base.
#
# BenchmarkAgent is re-exported here, not used, for backward compatibility
# with the fourteen call sites that still do ``from
# openadapt_evals.agents.base import BenchmarkAgent``. `__all__` states that
# intent explicitly instead of leaving the import looking unused.
__all__ = [
    "BenchmarkAgent",
    "action_to_string",
    "format_accessibility_tree",
    "parse_action_response",
]


def format_accessibility_tree(tree: dict, indent: int = 0) -> str:
    """Format accessibility tree for prompt.

    Args:
        tree: Accessibility tree dict.
        indent: Current indentation level.

    Returns:
        Formatted string representation.
    """
    lines = []
    prefix = "  " * indent

    role = tree.get("role", "unknown")
    name = tree.get("name", "")
    node_id = tree.get("id", tree.get("node_id", ""))

    line = f"{prefix}[{node_id}] {role}"
    if name:
        line += f": {name}"
    lines.append(line)

    for child in tree.get("children", []):
        lines.append(format_accessibility_tree(child, indent + 1))

    return "\n".join(lines)


def action_to_string(action: BenchmarkAction) -> str:
    """Convert BenchmarkAction to string representation.

    Args:
        action: Action to convert.

    Returns:
        String representation.
    """
    if action.type == "click":
        if action.target_node_id:
            return f"CLICK([{action.target_node_id}])"
        if action.target_name:
            return f"CLICK({action.target_name})"
        if action.x is not None and action.y is not None:
            try:
                return f"CLICK({float(action.x):.3f}, {float(action.y):.3f})"
            except (ValueError, TypeError):
                return f"CLICK({action.x}, {action.y})"
        return "CLICK()"
    elif action.type == "type":
        return f"TYPE({action.text!r})"
    elif action.type == "key":
        mods = "+".join(action.modifiers or [])
        key = action.key
        if mods:
            return f"KEY({mods}+{key})"
        return f"KEY({key})"
    elif action.type == "scroll":
        return f"SCROLL({action.scroll_direction})"
    elif action.type == "drag":
        if action.x is not None and action.y is not None and action.end_x is not None and action.end_y is not None:
            return f"DRAG({action.x:.3f}, {action.y:.3f}, {action.end_x:.3f}, {action.end_y:.3f})"
        return "DRAG()"
    elif action.type == "done":
        return "DONE()"
    elif action.type == "answer":
        return f"ANSWER({action.answer!r})"
    else:
        return f"{action.type.upper()}()"


def parse_action_response(
    response: str, observation: BenchmarkObservation | None = None
) -> BenchmarkAction:
    """Parse VLM response into BenchmarkAction.

    Handles various response formats:
    - ACTION: CLICK(0.5, 0.3)
    - CLICK(0.5, 0.3)
    - I'll click at coordinates (0.5, 0.3) -> CLICK(0.5, 0.3)

    Args:
        response: Raw VLM response text.
        observation: Current observation (used for coordinate normalization).

    Returns:
        Parsed BenchmarkAction.
    """
    # Store raw response for debugging
    raw_action: dict[str, Any] = {"response": response}

    # Extract action line (look for ACTION: prefix or action pattern)
    action_line = None

    # Try to find ACTION: prefix
    action_match = re.search(r"ACTION:\s*(.+)", response, re.IGNORECASE)
    if action_match:
        action_line = action_match.group(1).strip()
    else:
        # Look for action pattern anywhere in response
        patterns = [
            r"(CLICK\s*\([^)]+\))",
            r"(TYPE\s*\([^)]+\))",
            r"(KEY\s*\([^)]+\))",
            r"(SCROLL\s*\([^)]+\))",
            r"(DRAG\s*\([^)]+\))",
            r"(DONE\s*\(\s*\))",
            r"(ANSWER\s*\([^)]+\))",
        ]
        for pattern in patterns:
            match = re.search(pattern, response, re.IGNORECASE)
            if match:
                action_line = match.group(1).strip()
                break

    if not action_line:
        # Parsing did not produce an agent decision. Keep that distinct from
        # the model explicitly issuing DONE().
        raw_action["parse_error"] = "No action pattern found"
        return BenchmarkAction(type="error", raw_action=raw_action)

    # Parse CLICK action
    click_match = re.match(
        r"CLICK\s*\(\s*\[?(\d+)\]?\s*\)", action_line, re.IGNORECASE
    )
    if click_match:
        # CLICK([id]) - element ID
        node_id = click_match.group(1)
        return BenchmarkAction(
            type="click",
            target_node_id=node_id,
            raw_action=raw_action,
        )

    click_coords = re.match(
        r"CLICK\s*\(\s*([\d.]+)\s*,\s*([\d.]+)\s*\)", action_line, re.IGNORECASE
    )
    if click_coords:
        # CLICK(x, y) - coordinates
        x = float(click_coords.group(1))
        y = float(click_coords.group(2))

        # Normalize coordinates if they appear to be pixel values
        # If x or y > 1.0, assume pixel coordinates and normalize using viewport
        if observation and observation.viewport and (x > 1.0 or y > 1.0):
            width, height = observation.viewport
            x_norm = x / width
            y_norm = y / height
            raw_action["original_coords"] = {"x": x, "y": y}
            raw_action["normalized"] = True
            x = x_norm
            y = y_norm

        return BenchmarkAction(
            type="click",
            x=x,
            y=y,
            raw_action=raw_action,
        )

    # Parse TYPE action
    type_match = re.match(
        r"TYPE\s*\(\s*[\"'](.+?)[\"']\s*\)", action_line, re.IGNORECASE
    )
    if type_match:
        text = type_match.group(1)
        return BenchmarkAction(
            type="type",
            text=text,
            raw_action=raw_action,
        )

    # Parse KEY action
    key_match = re.match(r"KEY\s*\(\s*(.+?)\s*\)", action_line, re.IGNORECASE)
    if key_match:
        key_str = key_match.group(1)
        # Handle modifier+key format
        if "+" in key_str:
            parts = key_str.split("+")
            key = parts[-1]
            modifiers = parts[:-1]
            return BenchmarkAction(
                type="key",
                key=key,
                modifiers=modifiers,
                raw_action=raw_action,
            )
        return BenchmarkAction(
            type="key",
            key=key_str,
            raw_action=raw_action,
        )

    # Parse SCROLL action
    scroll_match = re.match(
        r"SCROLL\s*\(\s*(up|down)\s*\)", action_line, re.IGNORECASE
    )
    if scroll_match:
        direction = scroll_match.group(1).lower()
        return BenchmarkAction(
            type="scroll",
            scroll_direction=direction,
            raw_action=raw_action,
        )

    # Parse DRAG action
    drag_match = re.match(
        r"DRAG\s*\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*\)",
        action_line,
        re.IGNORECASE,
    )
    if drag_match:
        x = float(drag_match.group(1))
        y = float(drag_match.group(2))
        end_x = float(drag_match.group(3))
        end_y = float(drag_match.group(4))

        # Normalize coordinates if they appear to be pixel values
        if observation and observation.viewport and (x > 1.0 or y > 1.0 or end_x > 1.0 or end_y > 1.0):
            width, height = observation.viewport
            raw_action["original_coords"] = {"x": x, "y": y, "end_x": end_x, "end_y": end_y}
            raw_action["normalized"] = True
            x = x / width
            y = y / height
            end_x = end_x / width
            end_y = end_y / height

        return BenchmarkAction(
            type="drag",
            x=x,
            y=y,
            end_x=end_x,
            end_y=end_y,
            raw_action=raw_action,
        )

    # Parse DONE action
    if re.match(r"DONE\s*\(\s*\)", action_line, re.IGNORECASE):
        return BenchmarkAction(type="done", raw_action=raw_action)

    # Parse ANSWER action
    answer_match = re.match(
        r"ANSWER\s*\(\s*[\"'](.+?)[\"']\s*\)", action_line, re.IGNORECASE
    )
    if answer_match:
        answer = answer_match.group(1)
        return BenchmarkAction(
            type="answer",
            answer=answer,
            raw_action=raw_action,
        )

    # Unknown action format
    raw_action["parse_error"] = f"Unknown action format: {action_line}"
    return BenchmarkAction(type="error", raw_action=raw_action)

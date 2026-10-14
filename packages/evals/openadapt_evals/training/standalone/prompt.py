"""Prompt construction and VLM output parsing for GRPO training.

Copies SYSTEM_PROMPT from openadapt-ml next_action.py so GRPO
operates in the same prompt distribution as SFT. NO openadapt-ml imports.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

from openadapt_evals.action_envelope import (
    parse_single_dsl_action,
    parse_single_json_object,
    require_exact_fields,
)
from openadapt_evals.errors import ActionParseError

logger = logging.getLogger(__name__)
DEFAULT_SCREEN_SIZE: tuple[int, int] = (1920, 1080)

# Copied from openadapt_ml.datasets.next_action.SYSTEM_PROMPT
SYSTEM_PROMPT = (
    "You are a GUI automation agent. Given a screenshot and a user goal, "
    "predict the single next action.\n\n"
    "COORDINATE SYSTEM:\n"
    "- x=0.0 is the LEFT edge, x=1.0 is the RIGHT edge\n"
    "- y=0.0 is the TOP edge, y=1.0 is the BOTTOM edge\n"
    "- To click the CENTER of an element, estimate its center position "
    "as a fraction of screen width/height\n"
    "- Example: An element in the middle of the screen would be "
    "approximately x=0.5, y=0.5\n\n"
    "ALLOWED ACTIONS (use exactly this format):\n"
    "- CLICK(x=0.XX, y=0.XX)  \u2192 click at normalized coordinates\n"
    '- TYPE(text="...")     \u2192 type text into the currently focused field\n'
    "- WAIT()                 \u2192 wait for UI to update\n"
    "- DONE()                 \u2192 task is complete\n\n"
    "RESPONSE FORMAT (required):\n"
    "Thought: [Brief reasoning: what element to interact with and why]\n"
    "Action: [Exactly one action, e.g., CLICK(x=0.35, y=0.42)]\n\n"
    "IMPORTANT: Output coordinates with 2 decimal places. "
    "Estimate the center of target elements."
)


@dataclass
class SimpleAction:
    """Lightweight action (no openadapt-ml dependency)."""

    type: str = "done"
    x: float | None = None
    y: float | None = None
    text: str | None = None
    key: str | None = None


def _normalized_point_to_pixels(
    x: float,
    y: float,
    screen_size: tuple[int, int],
) -> tuple[int, int]:
    """Map inclusive normalized coordinates to valid viewport indices."""
    width, height = screen_size
    if width <= 0 or height <= 0:
        raise ActionParseError("Screen dimensions must be positive")
    return min(int(x * width), width - 1), min(int(y * height), height - 1)


def build_agent_messages(
    instruction: str,
    *,
    include_image: bool = False,
    action_history: str = "",
) -> list[dict]:
    """Build chat messages matching the SFT prompt format."""
    history_text = f"{action_history}\n" if action_history else ""
    text_content = (
        f"Goal: {instruction}\n\n{history_text}"
        "Look at the screenshot and determine the NEXT action.\n\n"
        "Thought: [what element to interact with and why]\n"
        'Action: [CLICK(x=..., y=...) or TYPE(text="...") or WAIT() or DONE()]'
    )
    if include_image:
        user_content: Any = [
            {"type": "image"},
            {"type": "text", "text": text_content},
        ]
    else:
        user_content = text_content
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def parse_vlm_output_to_action(
    text: str,
    screen_size: tuple[int, int] = DEFAULT_SCREEN_SIZE,
) -> SimpleAction:
    """Parse VLM output to SimpleAction. Supports Thought/Action, bare DSL, and JSON."""
    text = text.strip()
    width, height = screen_size
    logger.debug("Parsing VLM output (%d chars): %.200s", len(text), text)

    # JSON: {"action_type": "click", "coordinate": [x, y]}
    data = parse_single_json_object(text)
    if data is not None:
        atype = data.get("action_type", "")
        if not isinstance(atype, str):
            raise ActionParseError("JSON action_type must be a string")
        atype = atype.lower()
        if atype == "click":
            coordinate_fields = {field for field in ("coordinate", "coords") if field in data}
            if len(coordinate_fields) != 1:
                raise ActionParseError("JSON click requires exactly one coordinate field")
            field = next(iter(coordinate_fields))
            _require_json_fields(data, {"action_type", field})
            coord = data[field]
            if not isinstance(coord, (list, tuple)) or len(coord) != 2:
                raise ActionParseError("JSON click requires exactly two coordinates")
            if any(isinstance(value, bool) for value in coord):
                raise ActionParseError("JSON click coordinates must be numbers, not booleans")
            try:
                xv, yv = float(coord[0]), float(coord[1])
            except (TypeError, ValueError) as exc:
                raise ActionParseError("Malformed JSON click coordinates") from exc
            if not math.isfinite(xv) or not math.isfinite(yv):
                raise ActionParseError("JSON click coordinates must be finite")
            normalized = 0.0 <= xv <= 1.0 and 0.0 <= yv <= 1.0
            pixels = 0.0 <= xv < width and 0.0 <= yv < height
            if normalized:
                x_px, y_px = _normalized_point_to_pixels(xv, yv, screen_size)
                return SimpleAction(type="click", x=x_px, y=y_px)
            if not all(isinstance(value, int) and not isinstance(value, bool) for value in coord):
                raise ActionParseError(
                    "Pixel-space JSON click coordinates must be explicit integers"
                )
            if not pixels:
                raise ActionParseError("JSON click coordinates are outside the viewport")
            return SimpleAction(type="click", x=coord[0], y=coord[1])
        if atype == "type":
            _require_json_fields(data, {"action_type", "text"})
            if not isinstance(data.get("text"), str):
                raise ActionParseError("JSON type requires string text")
            return SimpleAction(type="type", text=data["text"])
        if atype in ("done", "wait"):
            _require_json_fields(data, {"action_type"})
            return SimpleAction(type=atype)
        raise ActionParseError(f"Unsupported JSON action type: {atype!r}")

    command, arguments = parse_single_dsl_action(
        text,
        allowed_commands={"CLICK", "TYPE", "WAIT", "DONE"},
    )
    if command == "CLICK":
        require_exact_fields(arguments, {"x", "y"}, command)
        try:
            xf = float(arguments["x"])
            yf = float(arguments["y"])
            if not all(math.isfinite(v) and 0.0 <= v <= 1.0 for v in (xf, yf)):
                raise ActionParseError("CLICK coordinates must be between 0 and 1")
            x_px, y_px = _normalized_point_to_pixels(xf, yf, screen_size)
            return SimpleAction(type="click", x=x_px, y=y_px)
        except (ValueError, TypeError, OverflowError):
            raise ActionParseError(
                f"Malformed CLICK coordinates: "
                f"x={arguments['x']!r}, y={arguments['y']!r}"
            )
    if command == "TYPE":
        require_exact_fields(arguments, {"text"}, command)
        value = arguments["text"]
        value = value.replace("\\\\", "\\").replace('\\"', '"').replace("\\'", "'")
        return SimpleAction(type="type", text=value)
    if command == "WAIT":
        require_exact_fields(arguments, set(), command)
        return SimpleAction(type="wait")
    if command == "DONE":
        require_exact_fields(arguments, set(), command)
        return SimpleAction(type="done")

    raise ActionParseError(f"Unsupported action command: {command!r}")


def _require_json_fields(data: dict[str, Any], required: set[str]) -> None:
    """Reject missing and unrelated standalone JSON action fields."""
    fields = set(data) - {"reasoning"}
    missing = required - fields
    extra = fields - required
    if missing:
        raise ActionParseError(
            f"JSON action requires {', '.join(sorted(missing))}"
        )
    if extra:
        raise ActionParseError(
            f"JSON action has unsupported fields: {', '.join(sorted(extra))}"
        )


def format_action_as_text(
    action: SimpleAction,
    screen_size: tuple[int, int] = DEFAULT_SCREEN_SIZE,
) -> str:
    """Convert SimpleAction to DSL text for log-prob computation."""
    width, height = screen_size
    if action.type == "click":
        if width <= 0 or height <= 0:
            raise ActionParseError("Screen dimensions must be positive")
        if action.x is None or action.y is None:
            raise ActionParseError("Cannot format CLICK without x and y")
        if not (
            math.isfinite(action.x)
            and math.isfinite(action.y)
            and 0 <= action.x < width
            and 0 <= action.y < height
        ):
            raise ActionParseError("Cannot format CLICK outside the viewport")
        xf = action.x / width
        yf = action.y / height
        return f"CLICK(x={xf:.2f}, y={yf:.2f})"
    if action.type == "type":
        if not isinstance(action.text, str):
            raise ActionParseError("Cannot format TYPE without explicit string text")
        escaped = action.text.replace("\\", "\\\\").replace('"', '\\"')
        return f'TYPE(text="{escaped}")'
    if action.type == "wait":
        return "WAIT()"
    if action.type == "done":
        return "DONE()"
    raise ActionParseError(f"Cannot format unsupported action type: {action.type!r}")

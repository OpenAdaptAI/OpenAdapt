"""Action display component for formatting action information.

This component displays actions with:
- Action type badge (CLICK, TYPE, SCROLL, KEY)
- Action details (coordinates, text, direction)
- Optional reasoning/context
"""

from __future__ import annotations

import json
from typing import Any


def action_display(
    action_type: str | None = None,
    action_details: dict[str, Any] | None = None,
    show_badge: bool = True,
    show_details: bool = True,
    show_reasoning: bool = False,
    reasoning: str | None = None,
    class_name: str = "",
) -> str:
    """Render an action display with badge and details.

    Args:
        action_type: Type of action (click, type, scroll, key, etc.)
        action_details: Dictionary of action parameters
        show_badge: Whether to show the action type badge
        show_details: Whether to show action details
        show_reasoning: Whether to show reasoning section
        reasoning: Optional reasoning/explanation text
        class_name: Additional CSS classes

    Returns:
        HTML string for the action display
    """
    if action_type is None and action_details is None:
        return '<div class="oa-action"><span class="oa-text-muted">No action</span></div>'

    extra_class = f" {class_name}" if class_name else ""
    action_type = action_type or "unknown"
    action_details = action_details or {}

    # Badge
    badge_html = ""
    if show_badge:
        badge_class = f"oa-action-{action_type.lower()}"
        badge_html = f'<span class="oa-action-badge {badge_class}">{action_type.upper()}</span>'

    # Details
    details_html = ""
    if show_details and action_details:
        details_str = _format_action_details(action_type, action_details)
        details_html = f'<span class="oa-action-details">{details_str}</span>'

    # Reasoning
    reasoning_html = ""
    if show_reasoning and reasoning:
        reasoning_html = f'''
        <div class="oa-action-reasoning" style="margin-top: 8px; padding: 8px; background: var(--oa-bg-tertiary); border-radius: 4px; font-size: 0.85rem; color: var(--oa-text-secondary);">
            <strong>Reasoning:</strong> {reasoning}
        </div>
        '''

    return f'''<div class="oa-action{extra_class}">
    {badge_html}
    {details_html}
    {reasoning_html}
</div>'''


def _format_action_details(action_type: str, details: dict[str, Any]) -> str:
    """Format action details based on action type."""
    action_type = action_type.lower()

    if action_type == "click":
        x = details.get("x")
        y = details.get("y")
        if x is not None and y is not None:
            # Format as percentage or pixel values
            if isinstance(x, float) and 0 <= x <= 1:
                return f"({x:.1%}, {y:.1%})"
            else:
                return f"({x}, {y})"
        return ""

    elif action_type == "type":
        text = details.get("text", "")
        if len(text) > 50:
            text = text[:47] + "..."
        return f'"{text}"'

    elif action_type == "scroll":
        direction = details.get("direction", details.get("scroll_direction", ""))
        amount = details.get("amount", details.get("scroll_amount", ""))
        if direction and amount:
            return f"{direction} ({amount})"
        elif direction:
            return direction
        return ""

    elif action_type == "key":
        key = details.get("key", "")
        modifiers = details.get("modifiers", [])
        if modifiers:
            mod_str = "+".join(modifiers)
            return f"{mod_str}+{key}"
        return key

    elif action_type == "done":
        answer = details.get("answer", "")
        if answer:
            return f'answer="{answer}"'
        return "(task complete)"

    else:
        # Generic JSON formatting for unknown types
        if details:
            try:
                return json.dumps(details, default=str)
            except (TypeError, ValueError):
                return str(details)
        return ""


def action_comparison(
    human_action: dict[str, Any] | None = None,
    predicted_action: dict[str, Any] | None = None,
    show_match: bool = True,
    class_name: str = "",
) -> str:
    """Render a side-by-side comparison of human and predicted actions.

    Args:
        human_action: Human action dictionary with type and details
        predicted_action: Predicted action dictionary with type and details
        show_match: Whether to show match/mismatch indicator
        class_name: Additional CSS classes

    Returns:
        HTML string for the action comparison
    """
    extra_class = f" {class_name}" if class_name else ""

    # Human action
    human_html = '<span class="oa-text-muted">No human action</span>'
    if human_action:
        action_type = human_action.get("type", "unknown")
        details = {k: v for k, v in human_action.items() if k != "type"}
        human_html = action_display(action_type, details, show_badge=True, show_details=True)

    # Predicted action
    predicted_html = '<span class="oa-text-muted">No prediction</span>'
    if predicted_action:
        action_type = predicted_action.get("type", "unknown")
        details = {k: v for k, v in predicted_action.items() if k != "type"}
        predicted_html = action_display(action_type, details, show_badge=True, show_details=True)

    # Match indicator
    match_html = ""
    if show_match and human_action and predicted_action:
        is_match = _actions_match(human_action, predicted_action)
        if is_match:
            match_html = '<span class="oa-badge oa-badge-success">Match</span>'
        else:
            match_html = '<span class="oa-badge oa-badge-error">Mismatch</span>'

    return f'''<div class="oa-action-comparison{extra_class}" style="display: flex; gap: 16px; align-items: center;">
    <div style="flex: 1;">
        <div style="font-size: 0.75rem; color: var(--oa-text-muted); margin-bottom: 4px;">Human</div>
        {human_html}
    </div>
    <div style="flex: 1;">
        <div style="font-size: 0.75rem; color: var(--oa-text-muted); margin-bottom: 4px;">Predicted</div>
        {predicted_html}
    </div>
    {match_html}
</div>'''


def _actions_match(human: dict[str, Any], predicted: dict[str, Any], tolerance: float = 0.05) -> bool:
    """Check if two actions match within tolerance."""
    human_type = human.get("type", "").lower()
    pred_type = predicted.get("type", "").lower()

    if human_type != pred_type:
        return False

    if human_type == "click":
        hx, hy = human.get("x", 0), human.get("y", 0)
        px, py = predicted.get("x", 0), predicted.get("y", 0)
        # Check if within tolerance (for normalized coordinates)
        return abs(hx - px) <= tolerance and abs(hy - py) <= tolerance

    elif human_type == "type":
        return human.get("text", "") == predicted.get("text", "")

    elif human_type == "key":
        return human.get("key", "") == predicted.get("key", "")

    elif human_type == "scroll":
        return human.get("direction", "") == predicted.get("direction", "")

    return True  # For unknown types, just match on type

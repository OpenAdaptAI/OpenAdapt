"""Badge component for status indicators.

This module provides:
- badge: Simple status badge (pass/fail, etc.)
"""

from __future__ import annotations


def badge(
    text: str,
    color: str = "info",
    size: str = "md",
    class_name: str = "",
) -> str:
    """Render a status badge.

    Args:
        text: Badge text
        color: Color variant (success, error, warning, info)
        size: Size variant (sm, md, lg)
        class_name: Additional CSS classes

    Returns:
        HTML string for the badge
    """
    extra_class = f" {class_name}" if class_name else ""

    # Size-based padding
    padding = {
        "sm": "2px 6px",
        "md": "4px 10px",
        "lg": "6px 14px",
    }.get(size, "4px 10px")

    font_size = {
        "sm": "0.65rem",
        "md": "0.75rem",
        "lg": "0.85rem",
    }.get(size, "0.75rem")

    return f'''<span class="oa-badge oa-badge-{color}{extra_class}" style="padding: {padding}; font-size: {font_size};">
    {text}
</span>'''


def pass_fail_badge(
    success: bool,
    pass_text: str = "Pass",
    fail_text: str = "Fail",
    size: str = "md",
    class_name: str = "",
) -> str:
    """Render a pass/fail badge.

    Args:
        success: Whether the status is passing
        pass_text: Text to show for pass
        fail_text: Text to show for fail
        size: Size variant (sm, md, lg)
        class_name: Additional CSS classes

    Returns:
        HTML string for the pass/fail badge
    """
    return badge(
        text=pass_text if success else fail_text,
        color="success" if success else "error",
        size=size,
        class_name=class_name,
    )


def status_dot(
    status: str,
    size: int = 12,
    class_name: str = "",
) -> str:
    """Render a status indicator dot.

    Args:
        status: Status type (running, completed, failed, pending)
        size: Dot size in pixels
        class_name: Additional CSS classes

    Returns:
        HTML string for the status dot
    """
    extra_class = f" {class_name}" if class_name else ""

    colors = {
        "running": "var(--oa-info)",
        "completed": "var(--oa-success)",
        "failed": "var(--oa-error)",
        "pending": "var(--oa-warning)",
    }
    color = colors.get(status, "var(--oa-text-muted)")

    animation = ""
    if status == "running":
        animation = "animation: oa-pulse 2s infinite;"

    return f'''<span class="oa-status-dot{extra_class}" style="
    display: inline-block;
    width: {size}px;
    height: {size}px;
    border-radius: 50%;
    background: {color};
    {animation}
"></span>'''

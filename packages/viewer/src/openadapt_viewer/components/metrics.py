"""Metrics display components for statistics and KPIs.

This module provides:
- metrics_card: Single statistic card
- metrics_grid: Grid of multiple cards
"""

from __future__ import annotations

from typing import Any


def metrics_card(
    label: str,
    value: str | float,
    change: float | None = None,
    color: str = "default",
    icon: str | None = None,
    class_name: str = "",
) -> str:
    """Render a single metrics card.

    Args:
        label: Label/title for the metric
        value: The metric value to display
        change: Optional percentage change (+5.2 or -3.1)
        color: Value color variant (default, success, error, warning, accent)
        icon: Optional SVG icon HTML
        class_name: Additional CSS classes

    Returns:
        HTML string for the metrics card
    """
    extra_class = f" {class_name}" if class_name else ""

    # Format value
    if isinstance(value, float):
        if value == int(value):
            value_str = str(int(value))
        else:
            value_str = f"{value:.1f}"
    else:
        value_str = str(value)

    # Value color class
    color_class = ""
    if color in ("success", "error", "warning", "accent"):
        color_class = f" oa-metrics-{color}"

    # Change indicator
    change_html = ""
    if change is not None:
        change_class = "positive" if change >= 0 else "negative"
        change_sign = "+" if change >= 0 else ""
        change_html = f'''
        <div class="oa-metrics-card-change {change_class}">
            {change_sign}{change:.1f}%
        </div>
        '''

    # Icon
    icon_html = ""
    if icon:
        icon_html = f'<div class="oa-metrics-card-icon">{icon}</div>'

    return f'''<div class="oa-metrics-card{extra_class}">
    {icon_html}
    <div class="oa-metrics-card-label">{label}</div>
    <div class="oa-metrics-card-value{color_class}">{value_str}</div>
    {change_html}
</div>'''


def metrics_grid(
    cards: list[dict[str, Any]],
    columns: int = 4,
    class_name: str = "",
) -> str:
    """Render a grid of metrics cards.

    Args:
        cards: List of card configurations, each with:
            - label: str (required)
            - value: str | int | float (required)
            - change: float (optional)
            - color: str (optional)
            - icon: str (optional)
        columns: Number of columns in the grid
        class_name: Additional CSS classes

    Returns:
        HTML string for the metrics grid
    """
    extra_class = f" {class_name}" if class_name else ""

    cards_html = "\n".join(
        metrics_card(
            label=card["label"],
            value=card["value"],
            change=card.get("change"),
            color=card.get("color", "default"),
            icon=card.get("icon"),
        )
        for card in cards
    )

    return f'''<div class="oa-metrics-grid{extra_class}" style="grid-template-columns: repeat({columns}, 1fr);">
    {cards_html}
</div>'''


def domain_stats_grid(
    domain_stats: dict[str, dict[str, int]],
    class_name: str = "",
) -> str:
    """Render a grid of domain statistics.

    Args:
        domain_stats: Dictionary mapping domain name to stats:
            {
                "office": {"passed": 5, "failed": 2, "total": 7},
                "browser": {"passed": 3, "failed": 1, "total": 4},
            }
        class_name: Additional CSS classes

    Returns:
        HTML string for domain stats grid
    """
    extra_class = f" {class_name}" if class_name else ""

    items_html_parts = []
    for domain, stats in domain_stats.items():
        passed = stats.get("passed", 0)
        total = stats.get("total", 0)
        rate = (passed / total * 100) if total > 0 else 0

        items_html_parts.append(f'''
        <div class="oa-domain-stat-item" style="padding: 12px; background: var(--oa-bg-tertiary); border-radius: 8px;">
            <div style="font-weight: 500; text-transform: capitalize; margin-bottom: 4px;">{domain}</div>
            <div style="display: flex; align-items: center; gap: 8px;">
                <span style="color: var(--oa-success);">{passed}</span>
                <span style="color: var(--oa-text-muted);">/</span>
                <span style="color: var(--oa-text-secondary);">{total}</span>
                <span style="font-size: 0.75rem; color: var(--oa-text-muted);">({rate:.0f}%)</span>
            </div>
        </div>
        ''')

    items_html = "\n".join(items_html_parts)

    return f'''<div class="oa-domain-stats-grid{extra_class}" style="display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 12px;">
    {items_html}
</div>'''

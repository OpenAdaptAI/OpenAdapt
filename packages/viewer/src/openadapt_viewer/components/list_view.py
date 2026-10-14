"""List view components for displaying selectable item lists.

This module provides:
- list_item: Single list item
- selectable_list: Container with selection support
"""

from __future__ import annotations

from typing import Any, TypedDict


class ListItemConfig(TypedDict, total=False):
    """List item configuration."""

    id: str
    title: str
    subtitle: str
    badge: str
    badge_color: str  # "success", "error", "warning", "info"
    metadata: dict[str, Any]


def list_item(
    item_id: str,
    title: str,
    subtitle: str | None = None,
    badge: str | None = None,
    badge_color: str = "info",
    selected: bool = False,
    click_handler: str | None = None,
    class_name: str = "",
) -> str:
    """Render a single list item.

    Args:
        item_id: Unique identifier for the item
        title: Main title text
        subtitle: Secondary text
        badge: Optional badge text
        badge_color: Badge color variant (success, error, warning, info)
        selected: Whether item is currently selected
        click_handler: Alpine.js click handler
        class_name: Additional CSS classes

    Returns:
        HTML string for the list item
    """
    extra_class = f" {class_name}" if class_name else ""
    selected_class = " oa-list-item-selected" if selected else ""

    # Click handler
    click_attr = f'@click="{click_handler}"' if click_handler else ""

    # Subtitle
    subtitle_html = ""
    if subtitle:
        subtitle_html = f'<div class="oa-list-item-subtitle">{subtitle}</div>'

    # Badge
    badge_html = ""
    if badge:
        badge_html = f'<span class="oa-badge oa-badge-{badge_color}">{badge}</span>'

    return f'''<div class="oa-list-item{selected_class}{extra_class}" data-id="{item_id}" {click_attr}>
    <div class="oa-list-item-content">
        <div style="flex: 1; min-width: 0;">
            <div class="oa-list-item-title">{title}</div>
            {subtitle_html}
        </div>
        {badge_html}
    </div>
</div>'''


def selectable_list(
    items: list[ListItemConfig],
    title: str | None = None,
    subtitle: str | None = None,
    max_height: str = "600px",
    alpine_data_name: str = "list",
    selected_item_var: str = "selectedItem",
    on_select: str | None = None,
    class_name: str = "",
) -> str:
    """Render a list with selection support.

    Args:
        items: List of item configurations
        title: Optional list header title
        subtitle: Optional list header subtitle
        max_height: Maximum height for scrolling
        alpine_data_name: Alpine.js x-data variable name
        selected_item_var: Variable name for selected item
        on_select: Additional Alpine.js code to run on selection
        class_name: Additional CSS classes

    Returns:
        HTML string for the selectable list
    """
    extra_class = f" {class_name}" if class_name else ""

    # Header
    header_html = ""
    if title:
        subtitle_el = f'<div class="oa-list-subtitle">{subtitle}</div>' if subtitle else ""
        header_html = f'''
        <div class="oa-list-header">
            <div class="oa-list-title">{title}</div>
            {subtitle_el}
        </div>
        '''

    # Generate items with selection binding
    items_html_parts = []
    for item in items:
        item_id = item.get("id", "")
        item_title = item.get("title", "")
        item_subtitle = item.get("subtitle")
        badge = item.get("badge")
        badge_color = item.get("badge_color", "info")

        # Selection check using Alpine.js
        selected_class = f":class=\"{{{selected_item_var}?.id === '{item_id}' ? 'oa-list-item-selected' : ''}}\""

        # Click handler
        on_select_code = f"; {on_select}" if on_select else ""
        click_handler = f"@click=\"{selected_item_var} = items.find(i => i.id === '{item_id}'){on_select_code}\""

        # Subtitle
        subtitle_html = ""
        if item_subtitle:
            subtitle_html = f'<div class="oa-list-item-subtitle">{item_subtitle}</div>'

        # Badge
        badge_html = ""
        if badge:
            badge_html = f'<span class="oa-badge oa-badge-{badge_color}">{badge}</span>'

        items_html_parts.append(f'''
        <div class="oa-list-item" data-id="{item_id}" {selected_class} {click_handler}>
            <div class="oa-list-item-content">
                <div style="flex: 1; min-width: 0;">
                    <div class="oa-list-item-title">{item_title}</div>
                    {subtitle_html}
                </div>
                {badge_html}
            </div>
        </div>
        ''')

    items_html = "\n".join(items_html_parts)

    return f'''<div class="oa-list{extra_class}">
    {header_html}
    <div class="oa-list-items" style="max-height: {max_height}; overflow-y: auto;">
        {items_html}
    </div>
</div>'''


def task_list(
    tasks: list[dict[str, Any]],
    title: str = "Tasks",
    max_height: str = "600px",
    alpine_data_name: str = "viewer",
    class_name: str = "",
) -> str:
    """Render a list of benchmark tasks with pass/fail badges.

    This is a convenience wrapper around selectable_list for task data.

    Args:
        tasks: List of task dictionaries with:
            - task_id: str
            - instruction: str
            - success: bool
            - domain: str (optional)
        title: List header title
        max_height: Maximum height for scrolling
        alpine_data_name: Alpine.js x-data variable name
        class_name: Additional CSS classes

    Returns:
        HTML string for the task list
    """
    items = []
    for task in tasks:
        task_id = task.get("task_id", "")
        instruction = task.get("instruction", "")
        success = task.get("success", False)
        task.get("domain", "")

        items.append({
            "id": task_id,
            "title": task_id,
            "subtitle": instruction[:60] + "..." if len(instruction) > 60 else instruction,
            "badge": "Pass" if success else "Fail",
            "badge_color": "success" if success else "error",
        })

    # Count for subtitle
    total = len(tasks)
    passed = sum(1 for t in tasks if t.get("success", False))

    return selectable_list(
        items=items,
        title=title,
        subtitle=f"{passed}/{total} passed",
        max_height=max_height,
        alpine_data_name=alpine_data_name,
        selected_item_var="selectedTask",
        class_name=class_name,
    )

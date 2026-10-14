"""Filter components for data filtering and search.

This module provides:
- filter_dropdown: Single dropdown filter
- filter_bar: Container for multiple filters
"""

from __future__ import annotations

from typing import TypedDict


class FilterOption(TypedDict, total=False):
    """Filter option definition."""

    value: str
    label: str
    selected: bool


class FilterConfig(TypedDict, total=False):
    """Filter configuration."""

    id: str
    label: str
    options: list[FilterOption]
    default_value: str


def filter_dropdown(
    filter_id: str,
    label: str,
    options: list[FilterOption] | list[str],
    default_value: str = "",
    alpine_model: str | None = None,
    class_name: str = "",
) -> str:
    """Render a single filter dropdown.

    Args:
        filter_id: Unique identifier for the filter
        label: Display label above the dropdown
        options: List of options (either FilterOption dicts or simple strings)
        default_value: Default selected value
        alpine_model: Alpine.js x-model binding (e.g., "filters.domain")
        class_name: Additional CSS classes

    Returns:
        HTML string for the filter dropdown
    """
    extra_class = f" {class_name}" if class_name else ""

    # Normalize options to FilterOption format
    normalized_options: list[FilterOption] = []
    for opt in options:
        if isinstance(opt, str):
            normalized_options.append({"value": opt, "label": opt.capitalize()})
        else:
            normalized_options.append(opt)

    # Generate option elements
    options_html_parts = [
        f'<option value="">All {label}s</option>'
    ]
    for opt in normalized_options:
        value = opt.get("value", "")
        label_text = opt.get("label", value)
        selected = "selected" if opt.get("selected") or value == default_value else ""
        options_html_parts.append(f'<option value="{value}" {selected}>{label_text}</option>')

    options_html = "\n".join(options_html_parts)

    # Alpine.js binding
    model_attr = f'x-model="{alpine_model}"' if alpine_model else ""

    return f'''<div class="oa-filter-group{extra_class}">
    <label class="oa-filter-label" for="filter-{filter_id}">{label}</label>
    <select id="filter-{filter_id}" class="oa-filter-dropdown" {model_attr}>
        {options_html}
    </select>
</div>'''


def filter_bar(
    filters: list[FilterConfig],
    search_placeholder: str | None = None,
    search_model: str | None = None,
    alpine_data_name: str = "filters",
    class_name: str = "",
) -> str:
    """Render a filter bar with multiple dropdowns and optional search.

    Args:
        filters: List of filter configurations
        search_placeholder: Placeholder text for search input (if None, no search)
        search_model: Alpine.js x-model for search input
        alpine_data_name: Alpine.js x-data variable name
        class_name: Additional CSS classes

    Returns:
        HTML string for the filter bar
    """
    extra_class = f" {class_name}" if class_name else ""

    # Generate filter dropdowns
    dropdowns_html = "\n".join(
        filter_dropdown(
            filter_id=f["id"],
            label=f["label"],
            options=f["options"],
            default_value=f.get("default_value", ""),
            alpine_model=f"{alpine_data_name}.{f['id']}" if alpine_data_name else None,
        )
        for f in filters
    )

    # Search input
    search_html = ""
    if search_placeholder:
        model_attr = f'x-model="{search_model}"' if search_model else ""
        search_html = f'''
        <div class="oa-filter-group" style="flex: 1; min-width: 200px;">
            <label class="oa-filter-label">Search</label>
            <input type="text" class="oa-filter-search" placeholder="{search_placeholder}" {model_attr}>
        </div>
        '''

    return f'''<div class="oa-filter-bar{extra_class}">
    {dropdowns_html}
    {search_html}
</div>'''


def status_filter(
    filter_id: str = "status",
    label: str = "Status",
    alpine_model: str | None = None,
    class_name: str = "",
) -> str:
    """Render a pre-configured status filter (passed/failed).

    Args:
        filter_id: Unique identifier for the filter
        label: Display label
        alpine_model: Alpine.js x-model binding
        class_name: Additional CSS classes

    Returns:
        HTML string for the status filter
    """
    return filter_dropdown(
        filter_id=filter_id,
        label=label,
        options=[
            {"value": "passed", "label": "Passed"},
            {"value": "failed", "label": "Failed"},
        ],
        alpine_model=alpine_model,
        class_name=class_name,
    )

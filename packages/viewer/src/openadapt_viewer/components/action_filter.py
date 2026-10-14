"""Action type filter component.

This component provides:
- Multi-select filtering by action type
- Toggle buttons for quick filtering
- Action type statistics
- Integration with list/timeline views
"""

from __future__ import annotations

import html
import json
from typing import TypedDict


class ActionTypeConfig(TypedDict, total=False):
    """Configuration for an action type filter."""

    type: str  # Action type identifier
    label: str  # Display label
    color: str  # Badge/indicator color
    icon: str  # Optional icon SVG
    count: int  # Number of actions of this type


# Default action types with colors
DEFAULT_ACTION_TYPES = [
    {"type": "click", "label": "Click", "color": "#3b82f6"},
    {"type": "type", "label": "Type", "color": "#22c55e"},
    {"type": "scroll", "label": "Scroll", "color": "#f59e0b"},
    {"type": "key", "label": "Key", "color": "#a855f7"},
    {"type": "drag", "label": "Drag", "color": "#ec4899"},
    {"type": "wait", "label": "Wait", "color": "#6b7280"},
    {"type": "done", "label": "Done", "color": "#14b8a6"},
]


def action_type_filter(
    action_types: list[ActionTypeConfig] | None = None,
    selected_types: list[str] | None = None,
    show_counts: bool = True,
    show_all_option: bool = True,
    multi_select: bool = True,
    alpine_model: str | None = None,
    on_change: str | None = None,
    layout: str = "horizontal",
    class_name: str = "",
) -> str:
    """Render an action type filter component.

    Args:
        action_types: List of action type configurations
        selected_types: Initially selected action types
        show_counts: Show action counts for each type
        show_all_option: Show "All" option to select/deselect all
        multi_select: Allow selecting multiple types
        alpine_model: Alpine.js model to bind selected types
        on_change: JavaScript callback when selection changes
        layout: "horizontal" or "vertical"
        class_name: Additional CSS classes

    Returns:
        HTML string for the action type filter
    """
    action_types = action_types or DEFAULT_ACTION_TYPES
    selected_types = selected_types or [at["type"] for at in action_types]
    extra_class = f" {class_name}" if class_name else ""

    # Properly escape JSON for HTML attributes to prevent Alpine.js parsing errors
    types_json = html.escape(json.dumps(action_types))
    selected_json = html.escape(json.dumps(selected_types))

    # Layout styles
    container_style = (
        "display: flex; flex-wrap: wrap; gap: var(--oa-space-sm);"
        if layout == "horizontal"
        else "display: flex; flex-direction: column; gap: var(--oa-space-xs);"
    )

    return f'''<div class="oa-action-type-filter{extra_class}"
     x-data="{{
         actionTypes: {types_json},
         selectedTypes: {selected_json},
         multiSelect: {str(multi_select).lower()},

         isSelected(type) {{
             return this.selectedTypes.includes(type);
         }},

         toggleType(type) {{
             if (this.multiSelect) {{
                 if (this.isSelected(type)) {{
                     this.selectedTypes = this.selectedTypes.filter(t => t !== type);
                 }} else {{
                     this.selectedTypes = [...this.selectedTypes, type];
                 }}
             }} else {{
                 this.selectedTypes = this.isSelected(type) ? [] : [type];
             }}
             this.notifyChange();
         }},

         selectAll() {{
             this.selectedTypes = this.actionTypes.map(at => at.type);
             this.notifyChange();
         }},

         selectNone() {{
             this.selectedTypes = [];
             this.notifyChange();
         }},

         get allSelected() {{
             return this.selectedTypes.length === this.actionTypes.length;
         }},

         get noneSelected() {{
             return this.selectedTypes.length === 0;
         }},

         notifyChange() {{
             {f"{alpine_model} = this.selectedTypes;" if alpine_model else ""}
             {f"({on_change})(this.selectedTypes);" if on_change else ""}
         }}
     }}"
     style="padding: var(--oa-space-md); background: var(--oa-bg-secondary); border-radius: var(--oa-border-radius-lg); border: 1px solid var(--oa-border-color);">

    <!-- Header -->
    <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: var(--oa-space-md);">
        <span style="font-weight: 600; font-size: var(--oa-font-size-sm);">Filter by Action Type</span>

        <!-- All/None Toggle -->
        <div x-show="{str(show_all_option).lower()} && multiSelect" style="display: flex; gap: var(--oa-space-xs);">
            <button @click="selectAll()"
                    :class="{{'oa-filter-btn-active': allSelected}}"
                    style="padding: 4px 8px; font-size: var(--oa-font-size-xs); border: 1px solid var(--oa-border-color); border-radius: var(--oa-border-radius); background: var(--oa-bg-tertiary); color: var(--oa-text-secondary); cursor: pointer;"
                    :style="allSelected ? 'background: var(--oa-accent-dim); border-color: var(--oa-accent); color: var(--oa-accent);' : ''">
                All
            </button>
            <button @click="selectNone()"
                    :class="{{'oa-filter-btn-active': noneSelected}}"
                    style="padding: 4px 8px; font-size: var(--oa-font-size-xs); border: 1px solid var(--oa-border-color); border-radius: var(--oa-border-radius); background: var(--oa-bg-tertiary); color: var(--oa-text-secondary); cursor: pointer;"
                    :style="noneSelected ? 'background: var(--oa-accent-dim); border-color: var(--oa-accent); color: var(--oa-accent);' : ''">
                None
            </button>
        </div>
    </div>

    <!-- Action Type Buttons -->
    <div style="{container_style}">
        <template x-for="at in actionTypes" :key="at.type">
            <button @click="toggleType(at.type)"
                    class="oa-action-type-btn"
                    :class="{{'oa-action-type-btn-selected': isSelected(at.type)}}"
                    style="display: flex; align-items: center; gap: var(--oa-space-sm); padding: var(--oa-space-sm) var(--oa-space-md); border: 2px solid var(--oa-border-color); border-radius: var(--oa-border-radius); background: var(--oa-bg-tertiary); cursor: pointer; transition: all var(--oa-transition-fast);"
                    :style="isSelected(at.type) ?
                        'border-color: ' + at.color + '; background: ' + at.color + '20;' :
                        'opacity: 0.5;'">

                <!-- Color Indicator -->
                <div style="width: 12px; height: 12px; border-radius: 50%;"
                     :style="'background: ' + at.color"></div>

                <!-- Label -->
                <span style="font-size: var(--oa-font-size-sm); font-weight: 500; color: var(--oa-text-primary);"
                      x-text="at.label"></span>

                <!-- Count Badge -->
                <span x-show="{str(show_counts).lower()} && at.count !== undefined"
                      style="padding: 2px 6px; font-size: var(--oa-font-size-xs); background: var(--oa-bg-secondary); border-radius: 9999px; color: var(--oa-text-muted);"
                      x-text="at.count">
                </span>

                <!-- Checkmark -->
                <svg x-show="isSelected(at.type)" width="14" height="14" viewBox="0 0 24 24" fill="none"
                     :style="'stroke: ' + at.color" stroke-width="3">
                    <path d="M20 6L9 17l-5-5"/>
                </svg>
            </button>
        </template>
    </div>

    <!-- Selection Summary -->
    <div style="margin-top: var(--oa-space-md); font-size: var(--oa-font-size-xs); color: var(--oa-text-muted);">
        <span x-text="selectedTypes.length + ' of ' + actionTypes.length + ' types selected'"></span>
    </div>
</div>'''


def action_type_pills(
    action_types: list[ActionTypeConfig] | None = None,
    selected_types: list[str] | None = None,
    alpine_model: str | None = None,
    on_change: str | None = None,
    class_name: str = "",
) -> str:
    """Render a compact pill-style action type filter.

    Args:
        action_types: List of action type configurations
        selected_types: Initially selected action types
        alpine_model: Alpine.js model to bind
        on_change: JavaScript callback when selection changes
        class_name: Additional CSS classes

    Returns:
        HTML string for action type pills
    """
    action_types = action_types or DEFAULT_ACTION_TYPES
    selected_types = selected_types or [at["type"] for at in action_types]
    extra_class = f" {class_name}" if class_name else ""

    # Properly escape JSON for HTML attributes to prevent Alpine.js parsing errors
    types_json = html.escape(json.dumps(action_types))
    selected_json = html.escape(json.dumps(selected_types))

    return f'''<div class="oa-action-type-pills{extra_class}"
     x-data="{{
         actionTypes: {types_json},
         selectedTypes: {selected_json},

         toggle(type) {{
             if (this.selectedTypes.includes(type)) {{
                 this.selectedTypes = this.selectedTypes.filter(t => t !== type);
             }} else {{
                 this.selectedTypes = [...this.selectedTypes, type];
             }}
             {f"{alpine_model} = this.selectedTypes;" if alpine_model else ""}
             {f"({on_change})(this.selectedTypes);" if on_change else ""}
         }}
     }}"
     style="display: flex; flex-wrap: wrap; gap: 6px;">

    <template x-for="at in actionTypes" :key="at.type">
        <button @click="toggle(at.type)"
                style="display: inline-flex; align-items: center; gap: 4px; padding: 4px 10px; border: none; border-radius: 9999px; font-size: 12px; font-weight: 500; cursor: pointer; transition: all 0.15s;"
                :style="selectedTypes.includes(at.type) ?
                    'background: ' + at.color + '; color: white;' :
                    'background: var(--oa-bg-tertiary); color: var(--oa-text-muted);'">
            <span x-text="at.label"></span>
            <span x-show="at.count !== undefined" x-text="'(' + at.count + ')'"
                  style="opacity: 0.7;"></span>
        </button>
    </template>
</div>'''


def action_type_dropdown(
    action_types: list[ActionTypeConfig] | None = None,
    selected_types: list[str] | None = None,
    placeholder: str = "Filter by action type",
    alpine_model: str | None = None,
    on_change: str | None = None,
    class_name: str = "",
) -> str:
    """Render a dropdown-style action type filter with checkboxes.

    Args:
        action_types: List of action type configurations
        selected_types: Initially selected action types
        placeholder: Placeholder text when collapsed
        alpine_model: Alpine.js model to bind
        on_change: JavaScript callback when selection changes
        class_name: Additional CSS classes

    Returns:
        HTML string for action type dropdown
    """
    action_types = action_types or DEFAULT_ACTION_TYPES
    selected_types = selected_types or [at["type"] for at in action_types]
    extra_class = f" {class_name}" if class_name else ""

    # Properly escape JSON for HTML attributes to prevent Alpine.js parsing errors
    types_json = html.escape(json.dumps(action_types))
    selected_json = html.escape(json.dumps(selected_types))

    return f'''<div class="oa-action-type-dropdown{extra_class}"
     x-data="{{
         actionTypes: {types_json},
         selectedTypes: {selected_json},
         isOpen: false,

         toggle(type) {{
             if (this.selectedTypes.includes(type)) {{
                 this.selectedTypes = this.selectedTypes.filter(t => t !== type);
             }} else {{
                 this.selectedTypes = [...this.selectedTypes, type];
             }}
             {f"{alpine_model} = this.selectedTypes;" if alpine_model else ""}
             {f"({on_change})(this.selectedTypes);" if on_change else ""}
         }},

         get displayText() {{
             if (this.selectedTypes.length === 0) return 'None selected';
             if (this.selectedTypes.length === this.actionTypes.length) return 'All types';
             if (this.selectedTypes.length === 1) {{
                 const at = this.actionTypes.find(a => a.type === this.selectedTypes[0]);
                 return at?.label || this.selectedTypes[0];
             }}
             return this.selectedTypes.length + ' types selected';
         }}
     }}"
     @click.outside="isOpen = false"
     style="position: relative; min-width: 200px;">

    <!-- Dropdown Button -->
    <button @click="isOpen = !isOpen"
            style="width: 100%; display: flex; align-items: center; justify-content: space-between; padding: var(--oa-space-sm) var(--oa-space-md); background: var(--oa-bg-tertiary); border: 1px solid var(--oa-border-color); border-radius: var(--oa-border-radius); cursor: pointer;">
        <span style="font-size: var(--oa-font-size-sm); color: var(--oa-text-primary);" x-text="displayText"></span>
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
             style="transition: transform 0.15s;" :style="isOpen ? 'transform: rotate(180deg)' : ''">
            <path d="M6 9l6 6 6-6"/>
        </svg>
    </button>

    <!-- Dropdown Menu -->
    <div x-show="isOpen"
         x-transition
         style="position: absolute; top: 100%; left: 0; right: 0; margin-top: 4px; padding: var(--oa-space-sm); background: var(--oa-bg-secondary); border: 1px solid var(--oa-border-color); border-radius: var(--oa-border-radius); box-shadow: var(--oa-shadow-md); z-index: 10;">

        <!-- Quick Actions -->
        <div style="display: flex; gap: var(--oa-space-xs); margin-bottom: var(--oa-space-sm); padding-bottom: var(--oa-space-sm); border-bottom: 1px solid var(--oa-border-color);">
            <button @click="selectedTypes = actionTypes.map(a => a.type); {f"{alpine_model} = selectedTypes;" if alpine_model else ""} {f"({on_change})(selectedTypes);" if on_change else ""}"
                    style="flex: 1; padding: 4px; font-size: var(--oa-font-size-xs); background: var(--oa-bg-tertiary); border: none; border-radius: var(--oa-border-radius); cursor: pointer; color: var(--oa-text-secondary);">
                Select All
            </button>
            <button @click="selectedTypes = []; {f"{alpine_model} = selectedTypes;" if alpine_model else ""} {f"({on_change})(selectedTypes);" if on_change else ""}"
                    style="flex: 1; padding: 4px; font-size: var(--oa-font-size-xs); background: var(--oa-bg-tertiary); border: none; border-radius: var(--oa-border-radius); cursor: pointer; color: var(--oa-text-secondary);">
                Clear All
            </button>
        </div>

        <!-- Options -->
        <template x-for="at in actionTypes" :key="at.type">
            <label style="display: flex; align-items: center; gap: var(--oa-space-sm); padding: var(--oa-space-xs); cursor: pointer; border-radius: var(--oa-border-radius);"
                   :style="selectedTypes.includes(at.type) ? 'background: var(--oa-accent-dim)' : ''"
                   @click="toggle(at.type)">
                <!-- Checkbox -->
                <div style="width: 16px; height: 16px; border: 2px solid var(--oa-border-color); border-radius: 3px; display: flex; align-items: center; justify-content: center;"
                     :style="selectedTypes.includes(at.type) ? 'border-color: ' + at.color + '; background: ' + at.color : ''">
                    <svg x-show="selectedTypes.includes(at.type)" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="3">
                        <path d="M20 6L9 17l-5-5"/>
                    </svg>
                </div>

                <!-- Color Dot -->
                <div style="width: 10px; height: 10px; border-radius: 50%;"
                     :style="'background: ' + at.color"></div>

                <!-- Label -->
                <span style="flex: 1; font-size: var(--oa-font-size-sm);" x-text="at.label"></span>

                <!-- Count -->
                <span x-show="at.count !== undefined" style="font-size: var(--oa-font-size-xs); color: var(--oa-text-muted);"
                      x-text="at.count"></span>
            </label>
        </template>
    </div>
</div>'''

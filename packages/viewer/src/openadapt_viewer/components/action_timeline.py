"""Action timeline component with seek functionality.

This component provides:
- Visual timeline showing actions as colored segments
- Click-to-seek to specific actions
- Action type indicators (click, type, scroll, key)
- Duration visualization
- Hover tooltips with action details
- Synchronization with playback controls
"""

from __future__ import annotations

import html
import json
from typing import TypedDict


class TimelineAction(TypedDict, total=False):
    """Action definition for timeline."""

    type: str  # Action type: click, type, scroll, key, etc.
    timestamp: float  # Start timestamp in seconds
    duration: float  # Duration in seconds (optional)
    label: str  # Short label
    description: str  # Full description
    success: bool  # Whether action succeeded (optional)
    frame_index: int  # Associated frame index (optional)
    details: dict  # Additional action details


# Color mapping for action types
ACTION_COLORS = {
    "click": "#3b82f6",  # blue
    "type": "#22c55e",  # green
    "scroll": "#f59e0b",  # amber
    "key": "#a855f7",  # purple
    "drag": "#ec4899",  # pink
    "wait": "#6b7280",  # gray
    "done": "#14b8a6",  # teal
    "error": "#ef4444",  # red
    "default": "#64748b",  # slate
}


def action_timeline(
    actions: list[TimelineAction] | None = None,
    duration: float | None = None,
    current_time: float = 0,
    width: str = "100%",
    height: int = 60,
    show_labels: bool = True,
    show_time_markers: bool = True,
    clickable: bool = True,
    alpine_sync_var: str | None = None,
    on_seek: str | None = None,
    class_name: str = "",
) -> str:
    """Render an action timeline with seek functionality.

    Args:
        actions: List of actions to display on timeline
        duration: Total duration in seconds (auto-calculated if not provided)
        current_time: Current playback position in seconds
        width: Timeline width (CSS value)
        height: Timeline height in pixels
        show_labels: Show action type labels on segments
        show_time_markers: Show time markers below timeline
        clickable: Allow clicking to seek
        alpine_sync_var: Alpine.js variable name to sync current time
        on_seek: JavaScript callback when seeking (receives timestamp)
        class_name: Additional CSS classes

    Returns:
        HTML string for the action timeline
    """
    actions = actions or []
    extra_class = f" {class_name}" if class_name else ""

    # Calculate duration from actions if not provided
    if duration is None and actions:
        duration = max(
            a.get("timestamp", 0) + a.get("duration", 0.5)
            for a in actions
        )
    duration = duration or 10  # Default duration

    # Properly escape JSON for HTML attributes to prevent Alpine.js parsing errors
    actions_json = html.escape(json.dumps(actions))
    colors_json = html.escape(json.dumps(ACTION_COLORS))

    # Build seek handler
    seek_handler = ""
    if clickable:
        handlers = []
        if alpine_sync_var:
            handlers.append(f"{alpine_sync_var} = seekTime")
        if on_seek:
            handlers.append(f"({on_seek})(seekTime)")
        seek_js = "; ".join(handlers) if handlers else ""
        seek_handler = f'''@click="(e) => {{
            const rect = $el.getBoundingClientRect();
            const clickX = e.clientX - rect.left;
            const percent = clickX / rect.width;
            const seekTime = percent * {duration};
            {seek_js}
            currentTime = seekTime;
        }}"'''

    # Sync watcher if provided
    sync_watcher = ""
    if alpine_sync_var:
        sync_watcher = f'''$watch('{alpine_sync_var}', (val) => {{ currentTime = val; }});'''

    return f'''<div class="oa-action-timeline{extra_class}"
     x-data="{{
         actions: {actions_json},
         colors: {colors_json},
         duration: {duration},
         currentTime: {current_time},
         hoveredAction: null,

         init() {{
             {sync_watcher}
         }},

         getActionColor(type) {{
             return this.colors[type?.toLowerCase()] || this.colors.default;
         }},

         getActionPosition(action) {{
             const start = (action.timestamp || 0) / this.duration * 100;
             const dur = (action.duration || 0.5) / this.duration * 100;
             return {{ left: start + '%', width: Math.max(dur, 1) + '%' }};
         }},

         seekToAction(action) {{
             this.currentTime = action.timestamp || 0;
             {f"{alpine_sync_var} = this.currentTime;" if alpine_sync_var else ""}
             {f"({on_seek})(this.currentTime);" if on_seek else ""}
         }},

         formatTime(seconds) {{
             const mins = Math.floor(seconds / 60);
             const secs = Math.floor(seconds % 60);
             return mins.toString().padStart(2, '0') + ':' + secs.toString().padStart(2, '0');
         }},

         get progressPercent() {{
             return (this.currentTime / this.duration) * 100;
         }}
     }}"
     style="width: {width};">

    <!-- Timeline Track -->
    <div class="oa-timeline-container" style="position: relative; height: {height}px;">
        <!-- Background Track -->
        <div class="oa-timeline-bg"
             {seek_handler}
             style="position: absolute; inset: 0; background: var(--oa-bg-tertiary); border-radius: var(--oa-border-radius); cursor: {'pointer' if clickable else 'default'}; overflow: hidden;">

            <!-- Action Segments -->
            <template x-for="(action, idx) in actions" :key="idx">
                <div class="oa-action-segment"
                     @click.stop="seekToAction(action)"
                     @mouseenter="hoveredAction = action"
                     @mouseleave="hoveredAction = null"
                     :style="{{
                         position: 'absolute',
                         top: '8px',
                         bottom: '8px',
                         ...getActionPosition(action),
                         background: getActionColor(action.type),
                         borderRadius: '4px',
                         cursor: 'pointer',
                         opacity: action.success === false ? 0.5 : 0.85,
                         transition: 'all 0.15s',
                         display: 'flex',
                         alignItems: 'center',
                         justifyContent: 'center',
                         overflow: 'hidden',
                     }}"
                     :class="{{'oa-action-segment-error': action.success === false}}">
                    <!-- Action Label -->
                    <span x-show="{str(show_labels).lower()} && (parseFloat(getActionPosition(action).width) > 3)"
                          style="font-size: 10px; font-weight: 600; color: white; text-transform: uppercase; letter-spacing: 0.5px; text-shadow: 0 1px 2px rgba(0,0,0,0.3); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; padding: 0 4px;"
                          x-text="action.label || action.type?.substring(0, 4)?.toUpperCase()">
                    </span>
                </div>
            </template>

            <!-- Current Time Indicator -->
            <div class="oa-timeline-cursor"
                 :style="'left: ' + progressPercent + '%;'"
                 style="position: absolute; top: 0; bottom: 0; width: 2px; background: var(--oa-accent); pointer-events: none; transition: left 0.1s linear;">
                <div style="position: absolute; top: -4px; left: -4px; width: 10px; height: 10px; background: var(--oa-accent); border-radius: 50%; box-shadow: 0 2px 4px rgba(0,0,0,0.3);"></div>
            </div>
        </div>

        <!-- Hover Tooltip -->
        <div x-show="hoveredAction"
             x-transition
             style="position: absolute; bottom: 100%; left: 50%; transform: translateX(-50%); margin-bottom: 8px; padding: 8px 12px; background: var(--oa-bg-primary); border: 1px solid var(--oa-border-color); border-radius: var(--oa-border-radius); box-shadow: var(--oa-shadow-md); white-space: nowrap; z-index: 10;">
            <div style="font-weight: 600; font-size: var(--oa-font-size-sm); margin-bottom: 4px;">
                <span :style="'color: ' + getActionColor(hoveredAction?.type)" x-text="hoveredAction?.type?.toUpperCase()"></span>
            </div>
            <div style="font-size: var(--oa-font-size-xs); color: var(--oa-text-secondary);">
                <span x-text="formatTime(hoveredAction?.timestamp || 0)"></span>
                <span x-show="hoveredAction?.description" x-text="' - ' + hoveredAction?.description"></span>
            </div>
        </div>
    </div>

    <!-- Time Markers -->
    <div x-show="{str(show_time_markers).lower()}" style="display: flex; justify-content: space-between; margin-top: var(--oa-space-xs); font-size: var(--oa-font-size-xs); color: var(--oa-text-muted);">
        <span>0:00</span>
        <span x-text="formatTime(duration / 4)"></span>
        <span x-text="formatTime(duration / 2)"></span>
        <span x-text="formatTime(duration * 3 / 4)"></span>
        <span x-text="formatTime(duration)"></span>
    </div>
</div>'''


def action_timeline_vertical(
    actions: list[TimelineAction] | None = None,
    height: str = "400px",
    show_details: bool = True,
    clickable: bool = True,
    alpine_sync_var: str | None = None,
    class_name: str = "",
) -> str:
    """Render a vertical action list/timeline.

    This is an alternative view showing actions as a vertical list
    with timestamps and details.

    Args:
        actions: List of actions to display
        height: Maximum height (scrollable)
        show_details: Show action details
        clickable: Allow clicking to seek
        alpine_sync_var: Alpine.js variable to sync
        class_name: Additional CSS classes

    Returns:
        HTML string for vertical action timeline
    """
    actions = actions or []
    extra_class = f" {class_name}" if class_name else ""
    # Properly escape JSON for HTML attributes to prevent Alpine.js parsing errors
    actions_json = html.escape(json.dumps(actions))
    colors_json = html.escape(json.dumps(ACTION_COLORS))

    return f'''<div class="oa-action-timeline-vertical{extra_class}"
     x-data="{{
         actions: {actions_json},
         colors: {colors_json},
         selectedIndex: -1,

         getActionColor(type) {{
             return this.colors[type?.toLowerCase()] || this.colors.default;
         }},

         formatTime(seconds) {{
             if (seconds === undefined || seconds === null) return '--:--';
             const mins = Math.floor(seconds / 60);
             const secs = Math.floor(seconds % 60);
             return mins.toString().padStart(2, '0') + ':' + secs.toString().padStart(2, '0');
         }},

         selectAction(idx) {{
             this.selectedIndex = idx;
             const action = this.actions[idx];
             if (action) {{
                 {f"{alpine_sync_var} = action.timestamp || 0;" if alpine_sync_var else ""}
             }}
         }}
     }}"
     style="max-height: {height}; overflow-y: auto;">

    <div class="oa-timeline-list" style="position: relative;">
        <!-- Timeline Line -->
        <div style="position: absolute; left: 20px; top: 0; bottom: 0; width: 2px; background: var(--oa-border-color);"></div>

        <!-- Actions -->
        <template x-for="(action, idx) in actions" :key="idx">
            <div class="oa-timeline-item"
                 @click="selectAction(idx)"
                 :class="{{'oa-timeline-item-selected': selectedIndex === idx}}"
                 style="display: flex; align-items: flex-start; gap: var(--oa-space-md); padding: var(--oa-space-md); padding-left: var(--oa-space-xl); cursor: {'pointer' if clickable else 'default'}; position: relative; transition: background 0.15s;"
                 :style="selectedIndex === idx ? 'background: var(--oa-accent-dim)' : ''">

                <!-- Dot -->
                <div style="position: absolute; left: 14px; width: 14px; height: 14px; border-radius: 50%; border: 3px solid; background: var(--oa-bg-primary);"
                     :style="'border-color: ' + getActionColor(action.type) + '; background: ' + (selectedIndex === idx ? getActionColor(action.type) : 'var(--oa-bg-primary)')">
                </div>

                <!-- Content -->
                <div style="flex: 1; min-width: 0;">
                    <div style="display: flex; align-items: center; gap: var(--oa-space-sm); margin-bottom: var(--oa-space-xs);">
                        <span class="oa-action-badge"
                              :style="'background: ' + getActionColor(action.type) + '20; color: ' + getActionColor(action.type)"
                              x-text="action.type?.toUpperCase()">
                        </span>
                        <span style="font-size: var(--oa-font-size-xs); color: var(--oa-text-muted); font-family: var(--oa-font-mono);"
                              x-text="formatTime(action.timestamp)">
                        </span>
                        <span x-show="action.success === false"
                              style="font-size: var(--oa-font-size-xs); color: var(--oa-error);">
                            Failed
                        </span>
                    </div>
                    <div x-show="{str(show_details).lower()} && action.description"
                         style="font-size: var(--oa-font-size-sm); color: var(--oa-text-secondary);"
                         x-text="action.description">
                    </div>
                    <div x-show="{str(show_details).lower()} && action.details"
                         style="font-size: var(--oa-font-size-xs); color: var(--oa-text-muted); font-family: var(--oa-font-mono); margin-top: var(--oa-space-xs);"
                         x-text="JSON.stringify(action.details)">
                    </div>
                </div>
            </div>
        </template>

        <!-- Empty State -->
        <div x-show="actions.length === 0" style="padding: var(--oa-space-lg); text-align: center; color: var(--oa-text-muted);">
            No actions recorded
        </div>
    </div>
</div>'''

"""Reusable UI components for OpenAdapt viewers.

This module provides building blocks for creating viewer HTML:
- screenshot_display: Screenshot with click/highlight overlays
- playback_controls: Play/pause/speed controls for step playback
- timeline: Progress bar for step navigation
- action_display: Format actions (click, type, scroll, etc.)
- metrics_card/metrics_grid: Statistics display cards
- filter_bar: Filter dropdowns and search
- selectable_list: List with selection support
- badge: Status badges (pass/fail, etc.)
- video_playback: Video playback from screenshot sequences
- action_timeline: Timeline with seek functionality
- comparison_view: Side-by-side comparison
- action_type_filter: Filter by action type
- failure_analysis_panel: Benchmark failure analysis

All components return HTML strings that can be composed together.
"""

from openadapt_viewer.components.action_display import action_display
from openadapt_viewer.components.action_filter import (
    action_type_dropdown,
    action_type_filter,
    action_type_pills,
)
from openadapt_viewer.components.action_timeline import (
    action_timeline,
    action_timeline_vertical,
)
from openadapt_viewer.components.badge import badge
from openadapt_viewer.components.comparison import (
    comparison_view,
    overlay_comparison,
)
from openadapt_viewer.components.failure_analysis import (
    failure_analysis_panel,
    failure_summary_card,
)
from openadapt_viewer.components.filters import filter_bar, filter_dropdown
from openadapt_viewer.components.list_view import list_item, selectable_list
from openadapt_viewer.components.metrics import metrics_card, metrics_grid
from openadapt_viewer.components.playback import playback_controls
from openadapt_viewer.components.screenshot import screenshot_display
from openadapt_viewer.components.timeline import timeline
from openadapt_viewer.components.video_playback import (
    video_playback,
    video_playback_with_actions,
)

__all__ = [
    "screenshot_display",
    "playback_controls",
    "timeline",
    "action_display",
    "metrics_card",
    "metrics_grid",
    "filter_bar",
    "filter_dropdown",
    "selectable_list",
    "list_item",
    "badge",
    # New enhanced components
    "video_playback",
    "video_playback_with_actions",
    "action_timeline",
    "action_timeline_vertical",
    "comparison_view",
    "overlay_comparison",
    "action_type_filter",
    "action_type_pills",
    "action_type_dropdown",
    "failure_analysis_panel",
    "failure_summary_card",
]

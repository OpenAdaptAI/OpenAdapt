"""Enhanced capture playback viewer demonstrating new dashboard features.

This example showcases:
- Video playback from captured screenshots (High priority)
- Action timeline with seek functionality (High priority)
- Side-by-side comparison view (Medium priority)
- Filtering by action type (Medium priority)
- Benchmark result integration for failure analysis (Medium priority)

Usage:
    python -m openadapt_viewer.examples.enhanced_capture_example
"""

from __future__ import annotations

import html
import json
from pathlib import Path

from openadapt_viewer.builders import PageBuilder
from openadapt_viewer.components import (
    action_timeline,
    action_timeline_vertical,
    action_type_filter,
    comparison_view,
    failure_analysis_panel,
    failure_summary_card,
    # Core components
    metrics_grid,
    # New enhanced components
    video_playback,
)


def generate_enhanced_capture_viewer(
    capture_id: str = "enhanced-demo",
    goal: str = "Demonstrate enhanced viewer features",
    output_path: str | Path = "enhanced_capture_viewer.html",
) -> Path:
    """Generate an enhanced capture playback viewer with all new features.

    Args:
        capture_id: Capture identifier
        goal: Task goal/description
        output_path: Output HTML file path

    Returns:
        Path to generated HTML file
    """
    # Generate sample data
    frames, actions, human_steps, ai_steps, failures = _generate_sample_data()

    # Build page
    builder = PageBuilder(
        title=f"Enhanced Capture Viewer - {capture_id}",
        include_alpine=True,
    )

    # Custom CSS for enhanced components
    builder.add_css('''
        .demo-section {
            margin-bottom: 48px;
        }
        .demo-section-title {
            font-size: 1.25rem;
            font-weight: 700;
            margin-bottom: 16px;
            padding-bottom: 8px;
            border-bottom: 2px solid var(--oa-accent);
            color: var(--oa-accent);
        }
        .demo-section-desc {
            font-size: 0.9rem;
            color: var(--oa-text-secondary);
            margin-bottom: 24px;
        }
        .feature-badge {
            display: inline-block;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 0.75rem;
            font-weight: 600;
            margin-left: 8px;
        }
        .feature-badge.high {
            background: var(--oa-error-bg);
            color: var(--oa-error);
        }
        .feature-badge.medium {
            background: var(--oa-warning-bg);
            color: var(--oa-warning);
        }
    ''')

    # Header
    builder.add_header(
        title="Enhanced Capture Viewer",
        subtitle=f"Goal: {goal}",
        nav_tabs=[
            {"href": "#video", "label": "Video Playback"},
            {"href": "#timeline", "label": "Timeline"},
            {"href": "#comparison", "label": "Comparison"},
            {"href": "#filters", "label": "Filters"},
            {"href": "#failures", "label": "Failure Analysis"},
        ],
    )

    # Summary metrics
    builder.add_section(
        metrics_grid([
            {"label": "Capture ID", "value": capture_id},
            {"label": "Total Frames", "value": len(frames)},
            {"label": "Total Actions", "value": len(actions)},
            {"label": "Duration", "value": f"{actions[-1]['timestamp']:.1f}s" if actions else "0s"},
        ], columns=4),
    )

    # ===================
    # Feature 1: Video Playback from Screenshots (HIGH PRIORITY)
    # ===================
    # Properly escape JSON for HTML attributes to prevent Alpine.js parsing errors
    html.escape(json.dumps(frames))

    builder.add_section(f'''
        <div class="demo-section" id="video">
            <h2 class="demo-section-title">
                1. Video Playback from Screenshots
                <span class="feature-badge high">High Priority</span>
            </h2>
            <p class="demo-section-desc">
                Plays captured screenshots as a video with full playback controls.
                Supports variable speed, frame-by-frame navigation, and timeline seeking.
            </p>

            {video_playback(
                frames=frames,
                width=960,
                height=540,
                show_controls=True,
                show_timeline=True,
                show_frame_counter=True,
                default_fps=2.0,
            )}
        </div>
    ''')

    # ===================
    # Feature 2: Action Timeline with Seek (HIGH PRIORITY)
    # ===================
    # Properly escape JSON for HTML attributes to prevent Alpine.js parsing errors
    actions_json = html.escape(json.dumps(actions))

    builder.add_section(f'''
        <div class="demo-section" id="timeline">
            <h2 class="demo-section-title">
                2. Action Timeline with Seek Functionality
                <span class="feature-badge high">High Priority</span>
            </h2>
            <p class="demo-section-desc">
                Visual timeline showing all actions as colored segments.
                Click anywhere to seek, hover for details.
            </p>

            <div style="margin-bottom: 24px;">
                <h4 style="margin-bottom: 12px;">Horizontal Timeline</h4>
                {action_timeline(
                    actions=actions,
                    height=60,
                    show_labels=True,
                    show_time_markers=True,
                    clickable=True,
                )}
            </div>

            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 24px;">
                <div>
                    <h4 style="margin-bottom: 12px;">Vertical Action List</h4>
                    {action_timeline_vertical(
                        actions=actions,
                        height="400px",
                        show_details=True,
                        clickable=True,
                    )}
                </div>
                <div style="background: var(--oa-bg-secondary); border-radius: var(--oa-border-radius-lg); padding: var(--oa-space-lg);">
                    <h4 style="margin-bottom: 12px;">Action Statistics</h4>
                    <div x-data="{{
                        actions: {actions_json},
                        get stats() {{
                            const counts = {{}};
                            this.actions.forEach(a => {{
                                counts[a.type] = (counts[a.type] || 0) + 1;
                            }});
                            return Object.entries(counts).map(([type, count]) => ({{ type, count }})).sort((a, b) => b.count - a.count);
                        }}
                    }}">
                        <template x-for="stat in stats" :key="stat.type">
                            <div style="display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid var(--oa-border-color);">
                                <span style="text-transform: uppercase; font-weight: 500;" x-text="stat.type"></span>
                                <span class="oa-badge oa-badge-info" x-text="stat.count"></span>
                            </div>
                        </template>
                    </div>
                </div>
            </div>
        </div>
    ''')

    # ===================
    # Feature 3: Side-by-Side Comparison View (MEDIUM PRIORITY)
    # ===================
    human_data = {"label": "Human Demo", "steps": human_steps, "variant": "human"}
    ai_data = {"label": "AI Replay", "steps": ai_steps, "variant": "predicted"}

    builder.add_section(f'''
        <div class="demo-section" id="comparison">
            <h2 class="demo-section-title">
                3. Side-by-Side Comparison View
                <span class="feature-badge medium">Medium Priority</span>
            </h2>
            <p class="demo-section-desc">
                Compare human demonstrations with AI replays side-by-side.
                Shows match/mismatch indicators and synchronized playback.
            </p>

            {comparison_view(
                left_data=human_data,
                right_data=ai_data,
                width=1200,
                height=400,
                show_diff=True,
                sync_playback=True,
                show_actions=True,
            )}
        </div>
    ''')

    # ===================
    # Feature 4: Filtering by Action Type (MEDIUM PRIORITY)
    # ===================
    # Calculate action counts
    action_counts = {}
    for a in actions:
        action_counts[a["type"]] = action_counts.get(a["type"], 0) + 1

    action_types = [
        {"type": "click", "label": "Click", "color": "#3b82f6", "count": action_counts.get("click", 0)},
        {"type": "type", "label": "Type", "color": "#22c55e", "count": action_counts.get("type", 0)},
        {"type": "scroll", "label": "Scroll", "color": "#f59e0b", "count": action_counts.get("scroll", 0)},
        {"type": "key", "label": "Key", "color": "#a855f7", "count": action_counts.get("key", 0)},
        {"type": "drag", "label": "Drag", "color": "#ec4899", "count": action_counts.get("drag", 0)},
    ]

    builder.add_section(f'''
        <div class="demo-section" id="filters">
            <h2 class="demo-section-title">
                4. Filtering by Action Type
                <span class="feature-badge medium">Medium Priority</span>
            </h2>
            <p class="demo-section-desc">
                Filter actions by type for focused analysis.
                Multiple filter styles available: buttons, pills, and dropdown.
            </p>

            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 24px;">
                <div>
                    <h4 style="margin-bottom: 12px;">Button Style Filter</h4>
                    {action_type_filter(
                        action_types=action_types,
                        show_counts=True,
                        multi_select=True,
                    )}
                </div>
                <div x-data="{{
                    actions: {actions_json},
                    selectedTypes: ['click', 'type', 'scroll', 'key', 'drag'],
                    get filteredActions() {{
                        return this.actions.filter(a => this.selectedTypes.includes(a.type));
                    }}
                }}">
                    <h4 style="margin-bottom: 12px;">Filtered Action List</h4>
                    <div style="background: var(--oa-bg-secondary); border-radius: var(--oa-border-radius-lg); padding: var(--oa-space-md); max-height: 300px; overflow-y: auto;">
                        <div style="font-size: var(--oa-font-size-sm); color: var(--oa-text-muted); margin-bottom: 12px;">
                            Showing <span x-text="filteredActions.length"></span> of <span x-text="actions.length"></span> actions
                        </div>
                        <template x-for="action in filteredActions" :key="action.timestamp">
                            <div style="padding: 8px; border-bottom: 1px solid var(--oa-border-color); display: flex; align-items: center; gap: 8px;">
                                <span class="oa-action-badge" x-text="action.type?.toUpperCase()"></span>
                                <span style="font-size: var(--oa-font-size-xs); color: var(--oa-text-muted);" x-text="action.timestamp?.toFixed(2) + 's'"></span>
                                <span style="font-size: var(--oa-font-size-sm);" x-text="action.description"></span>
                            </div>
                        </template>
                    </div>
                </div>
            </div>
        </div>
    ''')

    # ===================
    # Feature 5: Benchmark Result Integration (MEDIUM PRIORITY)
    # ===================
    builder.add_section(f'''
        <div class="demo-section" id="failures">
            <h2 class="demo-section-title">
                5. Benchmark Result Integration for Failure Analysis
                <span class="feature-badge medium">Medium Priority</span>
            </h2>
            <p class="demo-section-desc">
                Analyze benchmark failures with category breakdowns, error patterns,
                and detailed failure inspection.
            </p>

            <div style="display: grid; grid-template-columns: 300px 1fr; gap: 24px; margin-bottom: 24px;">
                {failure_summary_card(
                    total_failures=len(failures),
                    total_tasks=20,
                    top_error_type="Wrong Action",
                    top_error_count=3,
                )}
                <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px;">
                    <div class="oa-metrics-card">
                        <div class="oa-metrics-card-label">Error Types</div>
                        <div class="oa-metrics-card-value">4</div>
                    </div>
                    <div class="oa-metrics-card">
                        <div class="oa-metrics-card-label">Avg Steps to Failure</div>
                        <div class="oa-metrics-card-value">3.2</div>
                    </div>
                    <div class="oa-metrics-card">
                        <div class="oa-metrics-card-label">Most Failed Domain</div>
                        <div class="oa-metrics-card-value" style="font-size: var(--oa-font-size-md);">Browser</div>
                    </div>
                </div>
            </div>

            {failure_analysis_panel(
                failures=failures,
                total_tasks=20,
                show_categories=True,
                show_list=True,
                show_details=True,
            )}
        </div>
    ''')

    return builder.render_to_file(output_path)


def _generate_sample_data():
    """Generate sample data for demonstration."""
    import random

    # Sample frames for video playback
    frames = []
    for i in range(15):
        frames.append({
            "path": None,  # Would be real screenshot paths in production
            "timestamp": i * 0.5,
            "action": {
                "type": random.choice(["click", "type", "scroll"]),
                "x": random.uniform(0.2, 0.8),
                "y": random.uniform(0.2, 0.8),
            } if random.random() > 0.3 else None,
        })

    # Sample actions for timeline
    actions = [
        {"type": "click", "timestamp": 0.0, "duration": 0.3, "description": "Click System Settings icon", "details": {"x": 0.85, "y": 0.05}},
        {"type": "click", "timestamp": 0.8, "duration": 0.2, "description": "Click Displays menu item", "details": {"x": 0.15, "y": 0.30}},
        {"type": "scroll", "timestamp": 1.5, "duration": 0.5, "description": "Scroll down", "details": {"direction": "down", "amount": 200}},
        {"type": "click", "timestamp": 2.5, "duration": 0.2, "description": "Click Night Shift option", "details": {"x": 0.70, "y": 0.45}},
        {"type": "type", "timestamp": 3.2, "duration": 1.0, "description": "Enter schedule time", "details": {"text": "9:00 PM"}},
        {"type": "click", "timestamp": 4.5, "duration": 0.2, "description": "Toggle Night Shift off", "details": {"x": 0.80, "y": 0.35}},
        {"type": "key", "timestamp": 5.0, "duration": 0.1, "description": "Press Enter to confirm", "details": {"key": "Enter"}},
        {"type": "click", "timestamp": 5.5, "duration": 0.2, "description": "Close settings window", "details": {"x": 0.02, "y": 0.02}},
    ]

    # Sample steps for comparison (human vs AI)
    human_steps = [
        {"screenshot": None, "action_type": "click", "action_details": {"x": 0.85, "y": 0.05}},
        {"screenshot": None, "action_type": "click", "action_details": {"x": 0.15, "y": 0.30}},
        {"screenshot": None, "action_type": "scroll", "action_details": {"direction": "down"}},
        {"screenshot": None, "action_type": "click", "action_details": {"x": 0.70, "y": 0.45}},
        {"screenshot": None, "action_type": "click", "action_details": {"x": 0.80, "y": 0.35}},
    ]

    ai_steps = [
        {"screenshot": None, "action_type": "click", "action_details": {"x": 0.84, "y": 0.06}},  # Close match
        {"screenshot": None, "action_type": "click", "action_details": {"x": 0.15, "y": 0.32}},  # Close match
        {"screenshot": None, "action_type": "scroll", "action_details": {"direction": "down"}},  # Exact match
        {"screenshot": None, "action_type": "click", "action_details": {"x": 0.50, "y": 0.50}},  # Mismatch
        {"screenshot": None, "action_type": "type", "action_details": {"text": "wrong"}},  # Wrong action type
    ]

    # Sample failures for analysis
    failures = [
        {
            "task_id": "task-001",
            "instruction": "Open System Settings and navigate to Displays",
            "error_type": "wrong_action",
            "error_message": "Expected click at (0.70, 0.45) but got click at (0.50, 0.50)",
            "failed_step": 4,
            "total_steps": 5,
            "action_type": "click",
            "expected_action": {"type": "click", "x": 0.70, "y": 0.45},
            "actual_action": {"type": "click", "x": 0.50, "y": 0.50},
            "domain": "system",
            "difficulty": "easy",
        },
        {
            "task_id": "task-005",
            "instruction": "Search for a file in Finder",
            "error_type": "timeout",
            "error_message": "Operation timed out after 30 seconds",
            "failed_step": 2,
            "total_steps": 4,
            "action_type": "type",
            "domain": "system",
            "difficulty": "medium",
        },
        {
            "task_id": "task-008",
            "instruction": "Fill out a web form in Safari",
            "error_type": "element_not_found",
            "error_message": "Could not locate element: #submit-button",
            "failed_step": 6,
            "total_steps": 8,
            "action_type": "click",
            "domain": "browser",
            "difficulty": "medium",
        },
        {
            "task_id": "task-012",
            "instruction": "Send an email in Mail app",
            "error_type": "wrong_action",
            "error_message": "Expected type action but got click",
            "failed_step": 3,
            "total_steps": 5,
            "action_type": "type",
            "expected_action": {"type": "type", "text": "Hello"},
            "actual_action": {"type": "click", "x": 0.5, "y": 0.5},
            "domain": "office",
            "difficulty": "easy",
        },
        {
            "task_id": "task-015",
            "instruction": "Create a new document in Pages",
            "error_type": "wrong_action",
            "error_message": "Clicked wrong menu item",
            "failed_step": 2,
            "total_steps": 6,
            "action_type": "click",
            "domain": "office",
            "difficulty": "medium",
        },
        {
            "task_id": "task-018",
            "instruction": "Download file from Chrome",
            "error_type": "navigation_error",
            "error_message": "Navigation to download page failed",
            "failed_step": 4,
            "total_steps": 5,
            "action_type": "click",
            "domain": "browser",
            "difficulty": "hard",
        },
    ]

    return frames, actions, human_steps, ai_steps, failures


if __name__ == "__main__":
    output = generate_enhanced_capture_viewer()
    print(f"Generated enhanced capture viewer: {output}")
    print("\nFeatures demonstrated:")
    print("  1. Video playback from screenshots (High priority)")
    print("  2. Action timeline with seek functionality (High priority)")
    print("  3. Side-by-side comparison view (Medium priority)")
    print("  4. Filtering by action type (Medium priority)")
    print("  5. Benchmark result integration for failure analysis (Medium priority)")

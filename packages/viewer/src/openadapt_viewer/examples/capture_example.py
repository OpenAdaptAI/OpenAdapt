"""Example: Capture playback viewer using component library.

This shows how openadapt-capture can use the component library to
display recorded captures with step-by-step playback.

Usage:
    python -m openadapt_viewer.examples.capture_example
"""

from __future__ import annotations

from pathlib import Path

from openadapt_viewer.builders import PageBuilder
from openadapt_viewer.components import (
    metrics_grid,
)


def generate_capture_viewer(
    capture_id: str = "turn-off-nightshift",
    goal: str = "Turn off Night Shift in System Settings",
    steps: list[dict] | None = None,
    output_path: str | Path = "capture_viewer.html",
) -> Path:
    """Generate a capture playback viewer.

    Args:
        capture_id: Capture identifier
        goal: Task goal/description
        steps: List of step data [{screenshot, action, timestamp}]
        output_path: Output HTML file path

    Returns:
        Path to generated HTML file
    """
    # Sample data if not provided
    if steps is None:
        steps = _generate_sample_steps()

    total_steps = len(steps)
    duration = sum(s.get("duration", 0) for s in steps)

    # Build page
    builder = PageBuilder(
        title=f"Capture Viewer - {capture_id}",
        include_alpine=True,
    )

    # Header
    builder.add_header(
        title="Capture Viewer",
        subtitle=f"Goal: {goal}",
        nav_tabs=[
            {"href": "dashboard.html", "label": "Training"},
            {"href": "viewer.html", "label": "Viewer", "active": True},
            {"href": "benchmark.html", "label": "Benchmarks"},
        ],
    )

    # Capture info
    builder.add_section(
        metrics_grid([
            {"label": "Capture ID", "value": capture_id},
            {"label": "Total Steps", "value": total_steps},
            {"label": "Duration", "value": f"{duration:.1f}s"},
        ], columns=3),
    )

    # Main viewer with playback controls
    # This uses Alpine.js for state management
    import html
    import json
    # Properly escape JSON for HTML attributes to prevent Alpine.js parsing errors
    steps_json = html.escape(json.dumps(steps))

    builder.add_section(f'''
        <div x-data="{{
            currentStep: 0,
            isPlaying: false,
            playbackSpeed: 1,
            playbackInterval: null,
            steps: {steps_json},

            get totalSteps() {{ return this.steps.length; }},
            get currentStepData() {{ return this.steps[this.currentStep] || {{}}; }},

            prevStep() {{ if (this.currentStep > 0) this.currentStep--; }},
            nextStep() {{ if (this.currentStep < this.totalSteps - 1) this.currentStep++; }},
            goToStart() {{ this.currentStep = 0; }},
            goToEnd() {{ this.currentStep = this.totalSteps - 1; }},
            togglePlayback() {{
                if (this.isPlaying) {{
                    this.stopPlayback();
                }} else {{
                    this.startPlayback();
                }}
            }},
            startPlayback() {{
                this.isPlaying = true;
                const interval = 1000 / this.playbackSpeed;
                this.playbackInterval = setInterval(() => {{
                    if (this.currentStep < this.totalSteps - 1) {{
                        this.currentStep++;
                    }} else {{
                        this.stopPlayback();
                    }}
                }}, interval);
            }},
            stopPlayback() {{
                this.isPlaying = false;
                if (this.playbackInterval) {{
                    clearInterval(this.playbackInterval);
                    this.playbackInterval = null;
                }}
            }}
        }}"
        @keydown.space.window.prevent="togglePlayback()"
        @keydown.left.window="prevStep()"
        @keydown.right.window="nextStep()"
        @keydown.home.window="goToStart()"
        @keydown.end.window="goToEnd()">

            <!-- Screenshot Display -->
            <div style="background: var(--oa-bg-secondary); border-radius: 12px; padding: 24px; margin-bottom: 16px;">
                <div style="aspect-ratio: 16/9; background: var(--oa-bg-tertiary); border-radius: 8px; display: flex; align-items: center; justify-content: center; margin-bottom: 16px; position: relative;">
                    <template x-if="currentStepData.screenshot">
                        <img :src="currentStepData.screenshot" style="max-width: 100%; max-height: 100%; object-fit: contain;">
                    </template>
                    <template x-if="!currentStepData.screenshot">
                        <div style="color: var(--oa-text-muted);">No screenshot for step <span x-text="currentStep + 1"></span></div>
                    </template>

                    <!-- Click overlay -->
                    <template x-if="currentStepData.action?.type === 'click' && currentStepData.action?.x">
                        <div style="position: absolute; transform: translate(-50%, -50%); width: 24px; height: 24px; border-radius: 50%; border: 3px solid #22c55e; background: rgba(34, 197, 94, 0.2); display: flex; align-items: center; justify-content: center;"
                             :style="'left: ' + (currentStepData.action.x * 100) + '%; top: ' + (currentStepData.action.y * 100) + '%'">
                            <span style="font-size: 10px; font-weight: bold; color: white;">H</span>
                        </div>
                    </template>
                </div>

                <!-- Playback Controls -->
                <div style="display: flex; align-items: center; gap: 8px; padding: 12px; background: var(--oa-bg-tertiary); border-radius: 8px;">
                    <button @click="goToStart()" :disabled="currentStep === 0" style="padding: 8px; border: none; border-radius: 6px; background: var(--oa-bg-secondary); color: var(--oa-text-primary); cursor: pointer;">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M6 6h2v12H6V6zm3.5 6l8.5 6V6l-8.5 6z"/></svg>
                    </button>
                    <button @click="prevStep()" :disabled="currentStep === 0" style="padding: 8px; border: none; border-radius: 6px; background: var(--oa-bg-secondary); color: var(--oa-text-primary); cursor: pointer;">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M15.41 7.41L14 6l-6 6 6 6 1.41-1.41L10.83 12z"/></svg>
                    </button>
                    <button @click="togglePlayback()" style="padding: 8px 16px; border: none; border-radius: 6px; background: var(--oa-accent); color: var(--oa-bg-primary); cursor: pointer; font-weight: 600;">
                        <span x-text="isPlaying ? 'Pause' : 'Play'"></span>
                    </button>
                    <button @click="nextStep()" :disabled="currentStep >= totalSteps - 1" style="padding: 8px; border: none; border-radius: 6px; background: var(--oa-bg-secondary); color: var(--oa-text-primary); cursor: pointer;">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M10 6L8.59 7.41 13.17 12l-4.58 4.59L10 18l6-6z"/></svg>
                    </button>
                    <button @click="goToEnd()" :disabled="currentStep >= totalSteps - 1" style="padding: 8px; border: none; border-radius: 6px; background: var(--oa-bg-secondary); color: var(--oa-text-primary); cursor: pointer;">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M6 18l8.5-6L6 6v12zm2-12v12h2V6h-2z" transform="scale(-1, 1) translate(-24, 0)"/></svg>
                    </button>
                    <span style="flex: 1; text-align: center; font-size: 0.85rem; color: var(--oa-text-secondary);">
                        Step <span x-text="currentStep + 1"></span> of <span x-text="totalSteps"></span>
                    </span>
                    <select x-model.number="playbackSpeed" @change="if (isPlaying) {{ stopPlayback(); startPlayback(); }}" style="padding: 6px 12px; border: 1px solid var(--oa-border-color); border-radius: 6px; background: var(--oa-bg-secondary); color: var(--oa-text-primary);">
                        <option value="0.5">0.5x</option>
                        <option value="1" selected>1x</option>
                        <option value="2">2x</option>
                        <option value="4">4x</option>
                    </select>
                </div>

                <!-- Timeline -->
                <div style="margin-top: 12px; cursor: pointer;"
                     @click="(e) => {{
                         const rect = $el.getBoundingClientRect();
                         const clickX = e.clientX - rect.left;
                         const percent = clickX / rect.width;
                         currentStep = Math.min(Math.floor(percent * totalSteps), totalSteps - 1);
                     }}">
                    <div style="height: 8px; background: var(--oa-bg-tertiary); border-radius: 4px; overflow: hidden;">
                        <div style="height: 100%; background: linear-gradient(90deg, var(--oa-accent), #a78bfa); border-radius: 4px; transition: width 0.2s;"
                             :style="'width: ' + ((currentStep + 1) / totalSteps * 100) + '%'"></div>
                    </div>
                </div>
            </div>

            <!-- Action Details -->
            <div style="background: var(--oa-bg-secondary); border-radius: 12px; padding: 24px;">
                <h3 style="margin: 0 0 16px 0; font-size: 1rem;">Action Details</h3>
                <div style="display: grid; grid-template-columns: auto 1fr; gap: 12px; font-size: 0.9rem;">
                    <span style="color: var(--oa-text-muted);">Type:</span>
                    <span x-text="currentStepData.action?.type?.toUpperCase() || 'N/A'" style="font-weight: 600;"></span>

                    <template x-if="currentStepData.action?.type === 'click'">
                        <span style="color: var(--oa-text-muted);">Position:</span>
                    </template>
                    <template x-if="currentStepData.action?.type === 'click'">
                        <span x-text="'(' + (currentStepData.action?.x * 100).toFixed(1) + '%, ' + (currentStepData.action?.y * 100).toFixed(1) + '%)'"></span>
                    </template>

                    <template x-if="currentStepData.action?.type === 'type'">
                        <span style="color: var(--oa-text-muted);">Text:</span>
                    </template>
                    <template x-if="currentStepData.action?.type === 'type'">
                        <span x-text="currentStepData.action?.text" style="font-family: var(--oa-font-mono);"></span>
                    </template>

                    <span style="color: var(--oa-text-muted);">Timestamp:</span>
                    <span x-text="currentStepData.timestamp ? currentStepData.timestamp.toFixed(2) + 's' : 'N/A'"></span>
                </div>
            </div>
        </div>
    ''')

    return builder.render_to_file(output_path)


def _generate_sample_steps() -> list[dict]:
    """Generate sample step data."""
    import random

    steps = []
    timestamp = 0

    actions = [
        {"type": "click", "x": 0.85, "y": 0.05, "description": "Click System Settings icon"},
        {"type": "click", "x": 0.15, "y": 0.30, "description": "Click Displays"},
        {"type": "scroll", "direction": "down", "amount": 200},
        {"type": "click", "x": 0.70, "y": 0.45, "description": "Click Night Shift"},
        {"type": "click", "x": 0.80, "y": 0.35, "description": "Toggle Night Shift off"},
    ]

    for i, action in enumerate(actions):
        duration = random.uniform(0.5, 2.0)
        steps.append({
            "screenshot": None,  # Would be real path in production
            "action": action,
            "timestamp": timestamp,
            "duration": duration,
        })
        timestamp += duration

    return steps


if __name__ == "__main__":
    output = generate_capture_viewer()
    print(f"Generated: {output}")

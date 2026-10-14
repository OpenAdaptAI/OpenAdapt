"""HTML generator for capture viewer.

This module generates standalone HTML files for visualizing
OpenAdapt capture recordings with interactive playback controls.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

from openadapt_viewer.components import metrics_grid


def generate_capture_html(
    capture_id: str = "capture",
    goal: str | None = None,
    steps: list[dict[str, Any]] | None = None,
    episodes: list[dict[str, Any]] | None = None,
    output_path: str | Path = "capture_viewer.html",
) -> str:
    """Generate a standalone HTML viewer for capture playback.

    Args:
        capture_id: Identifier for the capture recording
        goal: Goal/description of the capture task
        steps: List of step data with actions and screenshots
        episodes: Optional episode segmentation data
        output_path: Where to write the HTML file

    Returns:
        Path to the generated HTML file
    """
    steps = steps or []
    episodes = episodes or []

    # Calculate stats
    total_steps = len(steps)
    duration = 0.0
    if steps:
        last_step = steps[-1]
        duration = last_step.get("timestamp", 0) + last_step.get("duration", 0)

    # Generate HTML
    html = _generate_viewer_html(
        capture_id=capture_id,
        goal=goal or "Capture playback",
        steps=steps,
        episodes=episodes,
        total_steps=total_steps,
        duration=duration,
    )

    # Write to file
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        f.write(html)

    return str(output_path)


def _generate_viewer_html(
    capture_id: str,
    goal: str,
    steps: list[dict[str, Any]],
    episodes: list[dict[str, Any]],
    total_steps: int,
    duration: float,
) -> str:
    """Generate the complete HTML for the capture viewer."""

    # Metrics cards for summary
    metrics_html = metrics_grid([
        {"label": "Capture ID", "value": capture_id},
        {"label": "Total Steps", "value": str(total_steps)},
        {"label": "Duration", "value": f"{duration:.1f}s"},
    ])

    # Serialize steps and episodes to JSON for Alpine.js
    steps_json = json.dumps(steps)
    episodes_json = json.dumps(episodes)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Capture Viewer - {capture_id}</title>
    <script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>

    <style>
        {_get_core_css()}
        {_get_episode_timeline_css()}
    </style>

    <script>
        {_get_episode_timeline_js()}
    </script>
</head>
<body style="background: var(--oa-bg-primary); color: var(--oa-text-primary); font-family: var(--oa-font-sans); min-height: 100vh; margin: 0;">

    <!-- Header -->
    <header style="padding: 16px 24px; background: var(--oa-bg-secondary); border-bottom: 1px solid var(--oa-border-color); margin-bottom: 24px;">
        <div style="display: flex; align-items: center; justify-content: space-between;">
            <div>
                <h1 style="font-size: 1.25rem; font-weight: 600; margin: 0;">Capture Viewer</h1>
                <p style="font-size: 0.85rem; color: var(--oa-text-secondary);">Goal: {goal}</p>
            </div>
            <div style="display: flex; align-items: center; gap: 16px;">
                {_get_dark_mode_toggle()}
            </div>
        </div>
    </header>

    <!-- Navigation Tabs -->
    <nav style="display: flex; gap: 4px; padding: 8px 24px; background: var(--oa-bg-secondary); border-bottom: 1px solid var(--oa-border-color); margin-bottom: 24px;">
        <a href="dashboard.html" style="padding: 8px 16px; border-radius: 6px; font-size: 0.85rem; font-weight: 500; text-decoration: none; color: var(--oa-text-secondary); transition: all 0.2s;">Training</a>
        <a href="viewer.html" style="padding: 8px 16px; border-radius: 6px; font-size: 0.85rem; font-weight: 500; text-decoration: none; color: var(--oa-text-secondary); transition: all 0.2s; background: var(--oa-accent); color: var(--oa-bg-primary);">Viewer</a>
        <a href="benchmark.html" style="padding: 8px 16px; border-radius: 6px; font-size: 0.85rem; font-weight: 500; text-decoration: none; color: var(--oa-text-secondary); transition: all 0.2s;">Benchmarks</a>
    </nav>

    <main style="max-width: 1400px; margin: 0 auto; padding: 0 24px 24px;">

        <!-- Summary Section -->
        <section class="oa-section" style="margin-bottom: 24px;">
            <!-- Episode context banner -->
            <div x-data="{{
                episodeName: new URLSearchParams(window.location.search).get('episode_name'),
                highlightStart: new URLSearchParams(window.location.search).get('highlight_start'),
                highlightEnd: new URLSearchParams(window.location.search).get('highlight_end')
            }}"
            x-show="episodeName"
            style="background: linear-gradient(135deg, rgba(255, 200, 0, 0.2), rgba(255, 165, 0, 0.2)); border: 2px solid rgba(255, 200, 0, 0.5); border-radius: 8px; padding: 16px; margin-bottom: 24px; display: flex; align-items: center; gap: 12px;">
                <svg width="24" height="24" viewBox="0 0 24 24" fill="rgba(255, 200, 0, 0.9)">
                    <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z"/>
                </svg>
                <div style="flex: 1;">
                    <div style="font-weight: 600; color: var(--oa-text-primary); margin-bottom: 4px;">
                        Viewing Episode Context
                    </div>
                    <div style="font-size: 0.9rem; color: var(--oa-text-secondary);">
                        Episode: <span x-text="episodeName" style="color: rgba(255, 200, 0, 0.9); font-weight: 600;"></span>
                        <template x-if="highlightStart && highlightEnd">
                            <span> (Time range: <span x-text="parseFloat(highlightStart).toFixed(1)"></span>s - <span x-text="parseFloat(highlightEnd).toFixed(1)"></span>s)</span>
                        </template>
                    </div>
                </div>
            </div>

            {metrics_html}
        </section>

        <!-- Playback Section -->
        <section class="oa-section" style="margin-bottom: 24px;">
            <div x-data="{{
                currentStep: 0,
                isPlaying: false,
                playbackSpeed: 1,
                playbackInterval: null,
                highlightStart: null,
                highlightEnd: null,
                episodeName: null,
                episodes: {episodes_json},
                currentEpisodeIndex: -1,
                episodeTimeline: null,
                steps: {steps_json},

                async init() {{
                    // Parse URL parameters
                    const params = new URLSearchParams(window.location.search);
                    this.highlightStart = params.has('highlight_start') ? parseFloat(params.get('highlight_start')) : null;
                    this.highlightEnd = params.has('highlight_end') ? parseFloat(params.get('highlight_end')) : null;
                    this.episodeName = params.get('episode_name');

                    // Jump to highlight start time if provided
                    if (this.highlightStart !== null) {{
                        this.jumpToTimestamp(this.highlightStart);
                    }}

                    // Initialize episode timeline if episodes loaded
                    if (this.episodes.length > 0) {{
                        this.$nextTick(() => {{
                            this.initializeEpisodeTimeline();
                        }});
                    }}
                }},

                initializeEpisodeTimeline() {{
                    const container = document.getElementById('episode-timeline-container');
                    if (!container || !this.episodes.length) return;

                    this.episodeTimeline = new EpisodeTimeline({{
                        container: container,
                        episodes: this.episodes,
                        currentTime: this.getCurrentTime(),
                        totalDuration: this.getTotalDuration(),
                        onSeek: (time) => this.seekToTime(time),
                        onEpisodeChange: (episode) => {{
                            console.log('Episode changed:', episode.name);
                        }}
                    }});
                }},

                getCurrentTime() {{
                    return this.steps[this.currentStep]?.timestamp || 0;
                }},

                getTotalDuration() {{
                    if (!this.steps.length) return 0;
                    const lastStep = this.steps[this.steps.length - 1];
                    return lastStep.timestamp + (lastStep.duration || 0);
                }},

                seekToTime(time) {{
                    // Find the step closest to this time
                    for (let i = 0; i < this.steps.length; i++) {{
                        if (this.steps[i].timestamp >= time) {{
                            this.currentStep = i;
                            break;
                        }}
                    }}
                    // Update episode timeline
                    if (this.episodeTimeline) {{
                        this.episodeTimeline.update({{ currentTime: time }});
                    }}
                }},

                jumpToTimestamp(timestamp) {{
                    // Find the step closest to this timestamp
                    for (let i = 0; i < this.steps.length; i++) {{
                        if (this.steps[i].timestamp >= timestamp) {{
                            this.currentStep = i;
                            return;
                        }}
                    }}
                    // If timestamp is after all steps, go to last step
                    this.currentStep = this.steps.length - 1;
                }},

                isStepInHighlight(stepIndex) {{
                    if (this.highlightStart === null || this.highlightEnd === null) {{
                        return false;
                    }}
                    const step = this.steps[stepIndex];
                    if (!step) return false;
                    return step.timestamp >= this.highlightStart && step.timestamp <= this.highlightEnd;
                }},

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
            x-effect="if (episodeTimeline) episodeTimeline.update({{ currentTime: getCurrentTime() }})"
            @keydown.space.window.prevent="togglePlayback()"
            @keydown.left.window="prevStep()"
            @keydown.right.window="nextStep()"
            @keydown.home.window="goToStart()"
            @keydown.end.window="goToEnd()">

                <!-- Episode Timeline -->
                <template x-if="episodes.length > 0">
                    <div id="episode-timeline-container" style="margin-bottom: 24px;"></div>
                </template>

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
                        <select x-model.number="playbackSpeed" @change="if (isPlaying) {{ {{ stopPlayback(); startPlayback(); }} }}" style="padding: 6px 12px; border: 1px solid var(--oa-border-color); border-radius: 6px; background: var(--oa-bg-secondary); color: var(--oa-text-primary);">
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
                        <div style="height: 8px; background: var(--oa-bg-tertiary); border-radius: 4px; overflow: hidden; position: relative;">
                            <!-- Progress bar -->
                            <div style="height: 100%; background: linear-gradient(90deg, var(--oa-accent), #a78bfa); border-radius: 4px; transition: width 0.2s;"
                                 :style="'width: ' + ((currentStep + 1) / totalSteps * 100) + '%'"></div>

                            <!-- Episode highlight overlay -->
                            <template x-if="highlightStart !== null && highlightEnd !== null">
                                <div style="position: absolute; top: 0; height: 100%; background: rgba(255, 200, 0, 0.3); border: 2px solid rgba(255, 200, 0, 0.8); pointer-events: none;"
                                     :style="`left: ${{(highlightStart / steps[steps.length - 1].timestamp) * 100}}%; width: ${{((highlightEnd - highlightStart) / steps[steps.length - 1].timestamp) * 100}}%`">
                                </div>
                            </template>
                        </div>
                        <template x-if="episodeName">
                            <div style="margin-top: 8px; text-align: center; font-size: 0.85rem; color: rgba(255, 200, 0, 0.9); font-weight: 600;">
                                Episode: <span x-text="episodeName"></span>
                            </div>
                        </template>
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
        </section>

    </main>
    <footer style="text-align: center; padding: 24px; font-size: 0.8rem; color: var(--oa-text-muted);">
        Generated by <a href="https://github.com/OpenAdaptAI/openadapt-viewer" style="color: var(--oa-accent);">openadapt-viewer</a>
    </footer>
</body>
</html>"""


def _read_package_asset(package: str, name: str) -> str:
    """Return the text of a file that ships inside the installed package.

    The generated page is one file the user can move anywhere, so its stylesheet
    and its script are inlined rather than linked. A ``<link>`` or ``<script
    src>`` resolves against the directory the output HTML lands in, which is
    not a source checkout for anyone who installed this package.

    ``importlib.resources`` is used rather than a path built from ``__file__``
    so the lookup answers for an installed distribution as well as this tree.

    Args:
        package: Dotted name of the package holding the file.
        name: The file's name within that package.

    Returns:
        The file's text, or an empty string when it cannot be read. An
        unreadable asset degrades one component of the page rather than
        failing the whole generation, and says so on stdout.
    """
    try:
        return resources.files(package).joinpath(name).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ModuleNotFoundError) as e:
        # Silence here would render a page whose episode timeline is unstyled
        # and inert, with nothing anywhere saying why.
        print(f"Warning: Could not read {package}/{name}, omitting it: {e}")
        return ""


def _get_episode_timeline_css() -> str:
    """Return the episode timeline's stylesheet, for inlining in a <style>."""
    return _read_package_asset("openadapt_viewer.styles", "episode_timeline.css")


def _get_episode_timeline_js() -> str:
    """Return the episode timeline's script, for inlining in a <script>.

    It defines the ``EpisodeTimeline`` class the page constructs during Alpine's
    ``init``, so it must run before that: the caller places it in ``<head>``
    without ``defer``.
    """
    return _read_package_asset("openadapt_viewer.components", "episode_timeline.js")


def _get_core_css() -> str:
    """Return core CSS with variables and component styles."""
    # Try to read from core.css file
    css_path = Path(__file__).parent.parent.parent / "styles" / "core.css"
    try:
        if css_path.exists():
            return css_path.read_text()
    except (OSError, UnicodeDecodeError) as e:
        # A packaged core.css that exists but cannot be read used to fall
        # through to the truncated inline fallback in total silence, so a
        # page rendered with visibly different styling and nothing said why.
        print(f"Warning: Could not read {css_path}, using inline CSS: {e}")

    # Fallback: inline CSS (same as in capture_viewer.html)
    return """/* OpenAdapt Viewer - Core Styles
 * Shared CSS variables and base component styles.
 * All classes use the 'oa-' prefix to avoid conflicts.
 */

/* === CSS Variables === */
:root {
    /* Background colors */
    --oa-bg-primary: #0a0a0f;
    --oa-bg-secondary: #12121a;
    --oa-bg-tertiary: #1a1a24;

    /* Border */
    --oa-border-color: rgba(255, 255, 255, 0.06);
    --oa-border-radius: 8px;
    --oa-border-radius-lg: 12px;

    /* Text colors */
    --oa-text-primary: #f0f0f0;
    --oa-text-secondary: #888;
    --oa-text-muted: #555;

    /* Accent colors */
    --oa-accent: #00d4aa;
    --oa-accent-dim: rgba(0, 212, 170, 0.15);
    --oa-accent-secondary: #a78bfa;

    /* Status colors */
    --oa-success: #34d399;
    --oa-success-bg: rgba(52, 211, 153, 0.15);
    --oa-error: #ff5f5f;
    --oa-error-bg: rgba(255, 95, 95, 0.15);
    --oa-warning: #f59e0b;
    --oa-warning-bg: rgba(245, 158, 11, 0.15);
    --oa-info: #3b82f6;
    --oa-info-bg: rgba(59, 130, 246, 0.15);

    /* Spacing */
    --oa-space-xs: 4px;
    --oa-space-sm: 8px;
    --oa-space-md: 16px;
    --oa-space-lg: 24px;
    --oa-space-xl: 32px;

    /* Typography */
    --oa-font-sans: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", sans-serif;
    --oa-font-mono: "SF Mono", Monaco, "Cascadia Code", Consolas, monospace;
    --oa-font-size-xs: 0.75rem;
    --oa-font-size-sm: 0.85rem;
    --oa-font-size-md: 1rem;
    --oa-font-size-lg: 1.125rem;
    --oa-font-size-xl: 1.5rem;

    /* Shadows */
    --oa-shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.3);
    --oa-shadow-md: 0 4px 6px rgba(0, 0, 0, 0.3);
    --oa-shadow-lg: 0 8px 16px rgba(0, 0, 0, 0.4);

    /* Transitions */
    --oa-transition-fast: 0.15s ease;
    --oa-transition-normal: 0.2s ease;
    --oa-transition-slow: 0.3s ease;
}

/* Light mode overrides */
.oa-light, [data-theme="light"] {
    --oa-bg-primary: #ffffff;
    --oa-bg-secondary: #f3f4f6;
    --oa-bg-tertiary: #e5e7eb;
    --oa-border-color: rgba(0, 0, 0, 0.1);
    --oa-text-primary: #111827;
    --oa-text-secondary: #6b7280;
    --oa-text-muted: #9ca3af;
}

/* === Metrics === */
.oa-metrics-grid {
    display: grid;
    gap: var(--oa-space-md);
}

.oa-metrics-card {
    background: var(--oa-bg-secondary);
    border: 1px solid var(--oa-border-color);
    border-radius: var(--oa-border-radius-lg);
    padding: var(--oa-space-md);
    transition: border-color var(--oa-transition-fast);
}

.oa-metrics-card:hover {
    border-color: var(--oa-accent-dim);
}

.oa-metrics-card-label {
    font-size: var(--oa-font-size-sm);
    color: var(--oa-text-secondary);
    margin-bottom: var(--oa-space-xs);
}

.oa-metrics-card-value {
    font-size: var(--oa-font-size-xl);
    font-weight: 700;
    color: var(--oa-text-primary);
}
"""


def _get_dark_mode_toggle() -> str:
    """Generate dark mode toggle button."""
    return """
        <button onclick="document.body.classList.toggle('oa-light')"
                style="padding: 8px; border-radius: 8px; background: var(--oa-bg-tertiary); border: none; cursor: pointer; color: var(--oa-text-primary);"
                title="Toggle dark/light mode">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
                <path d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z"/>
            </svg>
        </button>
        """

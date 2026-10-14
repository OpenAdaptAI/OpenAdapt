"""Side-by-side comparison view component.

This component provides:
- Split-screen comparison of two execution traces
- Synchronized playback between views
- Visual diff highlighting for actions
- Support for human vs AI comparisons
- Screenshot overlay comparison
"""

from __future__ import annotations

import html
import json
from typing import TypedDict


class ComparisonStep(TypedDict, total=False):
    """Step definition for comparison."""

    screenshot: str  # Screenshot path or data URL
    action_type: str  # Action type
    action_details: dict  # Action parameters
    timestamp: float  # Timestamp
    label: str  # Optional label


class ComparisonData(TypedDict, total=False):
    """Data for one side of comparison."""

    label: str  # E.g., "Human", "AI", "Expected", "Actual"
    steps: list[ComparisonStep]
    variant: str  # "human", "predicted", "expected", "actual"


def comparison_view(
    left_data: ComparisonData | None = None,
    right_data: ComparisonData | None = None,
    width: int = 1200,
    height: int = 450,
    show_diff: bool = True,
    sync_playback: bool = True,
    show_actions: bool = True,
    click_tolerance: float = 0.05,
    class_name: str = "",
) -> str:
    """Render a side-by-side comparison view.

    Args:
        left_data: Data for left panel (e.g., human demonstration)
        right_data: Data for right panel (e.g., AI prediction)
        width: Total width of component
        height: Height of each screenshot panel
        show_diff: Highlight differences between actions
        sync_playback: Synchronize playback between panels
        show_actions: Show action details below screenshots
        click_tolerance: Tolerance for matching click positions (0-1)
        class_name: Additional CSS classes

    Returns:
        HTML string for the comparison view
    """
    left_data = left_data or {"label": "Left", "steps": [], "variant": "human"}
    right_data = right_data or {"label": "Right", "steps": [], "variant": "predicted"}
    extra_class = f" {class_name}" if class_name else ""

    # Properly escape JSON for HTML attributes to prevent Alpine.js parsing errors
    left_json = html.escape(json.dumps(left_data))
    right_json = html.escape(json.dumps(right_data))

    return f'''<div class="oa-comparison-view{extra_class}"
     x-data="{{
         left: {left_json},
         right: {right_json},
         currentStep: 0,
         isPlaying: false,
         playbackSpeed: 1,
         playbackInterval: null,
         syncPlayback: {str(sync_playback).lower()},
         showDiff: {str(show_diff).lower()},
         clickTolerance: {click_tolerance},

         get maxSteps() {{
             return Math.max(this.left.steps?.length || 0, this.right.steps?.length || 0);
         }},
         get leftStep() {{
             return this.left.steps?.[this.currentStep] || null;
         }},
         get rightStep() {{
             return this.right.steps?.[this.currentStep] || null;
         }},

         // Check if actions match
         actionsMatch(left, right) {{
             if (!left || !right) return null;
             if (left.action_type !== right.action_type) return false;

             if (left.action_type === 'click') {{
                 const dx = Math.abs((left.action_details?.x || 0) - (right.action_details?.x || 0));
                 const dy = Math.abs((left.action_details?.y || 0) - (right.action_details?.y || 0));
                 return dx <= this.clickTolerance && dy <= this.clickTolerance;
             }}

             if (left.action_type === 'type') {{
                 return left.action_details?.text === right.action_details?.text;
             }}

             if (left.action_type === 'key') {{
                 return left.action_details?.key === right.action_details?.key;
             }}

             if (left.action_type === 'scroll') {{
                 return left.action_details?.direction === right.action_details?.direction;
             }}

             return true;
         }},

         get isMatch() {{
             return this.actionsMatch(this.leftStep, this.rightStep);
         }},

         prevStep() {{ if (this.currentStep > 0) this.currentStep--; }},
         nextStep() {{ if (this.currentStep < this.maxSteps - 1) this.currentStep++; }},
         goToStart() {{ this.currentStep = 0; }},
         goToEnd() {{ this.currentStep = Math.max(0, this.maxSteps - 1); }},

         togglePlayback() {{
             if (this.isPlaying) {{
                 this.stopPlayback();
             }} else {{
                 this.startPlayback();
             }}
         }},
         startPlayback() {{
             if (this.maxSteps <= 1) return;
             if (this.currentStep >= this.maxSteps - 1) this.currentStep = 0;
             this.isPlaying = true;
             const interval = 1000 / this.playbackSpeed;
             this.playbackInterval = setInterval(() => {{
                 if (this.currentStep < this.maxSteps - 1) {{
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
         }},

         getOverlayStyle(step, variant) {{
             if (!step?.action_details?.x) return 'display: none';
             const color = variant === 'human' ? '#22c55e' : '#3b82f6';
             return `left: ${{step.action_details.x * 100}}%; top: ${{step.action_details.y * 100}}%; border-color: ${{color}}; background: ${{color}}20;`;
         }}
     }}"
     @keydown.space.window.prevent="togglePlayback()"
     @keydown.left.window="prevStep()"
     @keydown.right.window="nextStep()"
     style="width: {width}px;">

    <!-- Header with labels -->
    <div style="display: flex; gap: var(--oa-space-md); margin-bottom: var(--oa-space-md);">
        <div style="flex: 1; padding: var(--oa-space-sm) var(--oa-space-md); background: var(--oa-bg-secondary); border-radius: var(--oa-border-radius); display: flex; align-items: center; gap: var(--oa-space-sm);">
            <div style="width: 12px; height: 12px; border-radius: 50%; background: #22c55e;"></div>
            <span style="font-weight: 600;" x-text="left.label"></span>
            <span style="font-size: var(--oa-font-size-sm); color: var(--oa-text-muted);" x-text="'(' + (left.steps?.length || 0) + ' steps)'"></span>
        </div>
        <div style="flex: 1; padding: var(--oa-space-sm) var(--oa-space-md); background: var(--oa-bg-secondary); border-radius: var(--oa-border-radius); display: flex; align-items: center; gap: var(--oa-space-sm);">
            <div style="width: 12px; height: 12px; border-radius: 50%; background: #3b82f6;"></div>
            <span style="font-weight: 600;" x-text="right.label"></span>
            <span style="font-size: var(--oa-font-size-sm); color: var(--oa-text-muted);" x-text="'(' + (right.steps?.length || 0) + ' steps)'"></span>
        </div>
    </div>

    <!-- Screenshot Panels -->
    <div style="display: flex; gap: var(--oa-space-md); margin-bottom: var(--oa-space-md);">
        <!-- Left Panel -->
        <div style="flex: 1; position: relative;">
            <div style="aspect-ratio: 16/9; background: var(--oa-bg-tertiary); border-radius: var(--oa-border-radius-lg); overflow: hidden; display: flex; align-items: center; justify-content: center; position: relative;">
                <template x-if="leftStep?.screenshot">
                    <img :src="leftStep.screenshot" style="max-width: 100%; max-height: 100%; object-fit: contain;">
                </template>
                <template x-if="!leftStep?.screenshot">
                    <div style="color: var(--oa-text-muted);">
                        <span x-show="currentStep < (left.steps?.length || 0)">No screenshot</span>
                        <span x-show="currentStep >= (left.steps?.length || 0)">No step at index <span x-text="currentStep + 1"></span></span>
                    </div>
                </template>

                <!-- Click Overlay -->
                <template x-if="leftStep?.action_type === 'click' && leftStep?.action_details?.x">
                    <div style="position: absolute; transform: translate(-50%, -50%); width: 28px; height: 28px; border-radius: 50%; border: 3px solid #22c55e; background: rgba(34, 197, 94, 0.2); display: flex; align-items: center; justify-content: center;"
                         :style="'left: ' + (leftStep.action_details.x * 100) + '%; top: ' + (leftStep.action_details.y * 100) + '%'">
                        <span style="font-size: 10px; font-weight: bold; color: #22c55e;">H</span>
                    </div>
                </template>
            </div>
        </div>

        <!-- Match Indicator -->
        <div x-show="showDiff" style="display: flex; flex-direction: column; justify-content: center; align-items: center; width: 60px;">
            <template x-if="isMatch === true">
                <div style="padding: 8px; border-radius: 50%; background: var(--oa-success-bg);">
                    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="var(--oa-success)" stroke-width="2">
                        <path d="M20 6L9 17l-5-5"/>
                    </svg>
                </div>
            </template>
            <template x-if="isMatch === false">
                <div style="padding: 8px; border-radius: 50%; background: var(--oa-error-bg);">
                    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="var(--oa-error)" stroke-width="2">
                        <path d="M18 6L6 18M6 6l12 12"/>
                    </svg>
                </div>
            </template>
            <template x-if="isMatch === null">
                <div style="padding: 8px; border-radius: 50%; background: var(--oa-bg-tertiary);">
                    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="var(--oa-text-muted)" stroke-width="2">
                        <path d="M5 12h14"/>
                    </svg>
                </div>
            </template>
            <span style="font-size: var(--oa-font-size-xs); color: var(--oa-text-muted); margin-top: var(--oa-space-xs);"
                  x-text="isMatch === true ? 'Match' : (isMatch === false ? 'Mismatch' : 'N/A')"></span>
        </div>

        <!-- Right Panel -->
        <div style="flex: 1; position: relative;">
            <div style="aspect-ratio: 16/9; background: var(--oa-bg-tertiary); border-radius: var(--oa-border-radius-lg); overflow: hidden; display: flex; align-items: center; justify-content: center; position: relative;">
                <template x-if="rightStep?.screenshot">
                    <img :src="rightStep.screenshot" style="max-width: 100%; max-height: 100%; object-fit: contain;">
                </template>
                <template x-if="!rightStep?.screenshot">
                    <div style="color: var(--oa-text-muted);">
                        <span x-show="currentStep < (right.steps?.length || 0)">No screenshot</span>
                        <span x-show="currentStep >= (right.steps?.length || 0)">No step at index <span x-text="currentStep + 1"></span></span>
                    </div>
                </template>

                <!-- Click Overlay -->
                <template x-if="rightStep?.action_type === 'click' && rightStep?.action_details?.x">
                    <div style="position: absolute; transform: translate(-50%, -50%); width: 28px; height: 28px; border-radius: 50%; border: 3px solid #3b82f6; background: rgba(59, 130, 246, 0.2); display: flex; align-items: center; justify-content: center;"
                         :style="'left: ' + (rightStep.action_details.x * 100) + '%; top: ' + (rightStep.action_details.y * 100) + '%'">
                        <span style="font-size: 10px; font-weight: bold; color: #3b82f6;">AI</span>
                    </div>
                </template>
            </div>
        </div>
    </div>

    <!-- Action Details -->
    <div x-show="{str(show_actions).lower()}" style="display: flex; gap: var(--oa-space-md); margin-bottom: var(--oa-space-md);">
        <!-- Left Action -->
        <div style="flex: 1; padding: var(--oa-space-md); background: var(--oa-bg-secondary); border-radius: var(--oa-border-radius-lg);">
            <template x-if="leftStep">
                <div>
                    <span class="oa-action-badge oa-action-click" x-text="leftStep.action_type?.toUpperCase()"
                          :class="{{'oa-action-click': leftStep.action_type === 'click', 'oa-action-type': leftStep.action_type === 'type', 'oa-action-scroll': leftStep.action_type === 'scroll', 'oa-action-key': leftStep.action_type === 'key'}}"></span>
                    <span style="margin-left: var(--oa-space-sm); font-size: var(--oa-font-size-sm); color: var(--oa-text-secondary); font-family: var(--oa-font-mono);"
                          x-text="JSON.stringify(leftStep.action_details || {{}})"></span>
                </div>
            </template>
            <template x-if="!leftStep">
                <span style="color: var(--oa-text-muted);">No action</span>
            </template>
        </div>

        <!-- Spacer for match indicator -->
        <div x-show="showDiff" style="width: 60px;"></div>

        <!-- Right Action -->
        <div style="flex: 1; padding: var(--oa-space-md); background: var(--oa-bg-secondary); border-radius: var(--oa-border-radius-lg);">
            <template x-if="rightStep">
                <div>
                    <span class="oa-action-badge" x-text="rightStep.action_type?.toUpperCase()"
                          :class="{{'oa-action-click': rightStep.action_type === 'click', 'oa-action-type': rightStep.action_type === 'type', 'oa-action-scroll': rightStep.action_type === 'scroll', 'oa-action-key': rightStep.action_type === 'key'}}"></span>
                    <span style="margin-left: var(--oa-space-sm); font-size: var(--oa-font-size-sm); color: var(--oa-text-secondary); font-family: var(--oa-font-mono);"
                          x-text="JSON.stringify(rightStep.action_details || {{}})"></span>
                </div>
            </template>
            <template x-if="!rightStep">
                <span style="color: var(--oa-text-muted);">No action</span>
            </template>
        </div>
    </div>

    <!-- Playback Controls -->
    <div class="oa-playback-controls">
        <!-- Rewind -->
        <button class="oa-playback-btn" @click="goToStart()" :disabled="currentStep === 0" title="Go to start">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                <path d="M6 6h2v12H6V6zm3.5 6l8.5 6V6l-8.5 6z"/>
            </svg>
        </button>

        <!-- Previous -->
        <button class="oa-playback-btn" @click="prevStep()" :disabled="currentStep === 0" title="Previous step">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                <path d="M15.41 7.41L14 6l-6 6 6 6 1.41-1.41L10.83 12z"/>
            </svg>
        </button>

        <!-- Play/Pause -->
        <button class="oa-playback-btn oa-playback-btn-primary" @click="togglePlayback()" :disabled="maxSteps <= 1" title="Play/Pause">
            <svg x-show="!isPlaying" width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                <path d="M8 5v14l11-7z"/>
            </svg>
            <svg x-show="isPlaying" width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                <path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/>
            </svg>
        </button>

        <!-- Next -->
        <button class="oa-playback-btn" @click="nextStep()" :disabled="currentStep >= maxSteps - 1" title="Next step">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                <path d="M10 6L8.59 7.41 13.17 12l-4.58 4.59L10 18l6-6z"/>
            </svg>
        </button>

        <!-- End -->
        <button class="oa-playback-btn" @click="goToEnd()" :disabled="currentStep >= maxSteps - 1" title="Go to end">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                <path d="M6 18l8.5-6L6 6v12zm2-12v12h2V6h-2z" transform="scale(-1, 1) translate(-24, 0)"/>
            </svg>
        </button>

        <!-- Step Counter -->
        <span class="oa-playback-counter" x-text="'Step ' + (currentStep + 1) + ' of ' + maxSteps"></span>

        <!-- Speed Selector -->
        <select class="oa-playback-speed" x-model.number="playbackSpeed" @change="if (isPlaying) {{ stopPlayback(); startPlayback(); }}">
            <option value="0.5">0.5x</option>
            <option value="1" selected>1x</option>
            <option value="2">2x</option>
            <option value="4">4x</option>
        </select>
    </div>

    <!-- Timeline -->
    <div style="margin-top: var(--oa-space-md);">
        <div class="oa-timeline-track"
             @click="(e) => {{
                 const rect = $el.getBoundingClientRect();
                 const clickX = e.clientX - rect.left;
                 const percent = clickX / rect.width;
                 currentStep = Math.min(Math.floor(percent * maxSteps), maxSteps - 1);
             }}"
             style="height: 12px; cursor: pointer; position: relative;">
            <div class="oa-timeline-progress"
                 :style="'width: ' + ((currentStep + 1) / maxSteps * 100) + '%'"></div>

            <!-- Step Markers with Match Indicators -->
            <template x-for="idx in maxSteps" :key="idx">
                <div @click.stop="currentStep = idx - 1"
                     style="position: absolute; top: -4px; width: 20px; height: 20px; border-radius: 50%; cursor: pointer; display: flex; align-items: center; justify-content: center; font-size: 9px; font-weight: bold; transition: all 0.15s;"
                     :style="'left: calc(' + ((idx - 1) / (maxSteps - 1) * 100) + '% - 10px); ' +
                             'background: ' + (actionsMatch(left.steps?.[idx-1], right.steps?.[idx-1]) === true ? 'var(--oa-success)' :
                                             actionsMatch(left.steps?.[idx-1], right.steps?.[idx-1]) === false ? 'var(--oa-error)' : 'var(--oa-bg-tertiary)') + '; ' +
                             'border: 2px solid ' + (idx - 1 === currentStep ? 'var(--oa-accent)' : 'transparent') + '; ' +
                             'color: ' + (actionsMatch(left.steps?.[idx-1], right.steps?.[idx-1]) !== null ? 'white' : 'var(--oa-text-muted)')"
                     x-text="idx">
                </div>
            </template>
        </div>
    </div>
</div>'''


def overlay_comparison(
    base_screenshot: str | None = None,
    human_click: dict | None = None,
    predicted_click: dict | None = None,
    width: int = 800,
    height: int = 450,
    show_distance: bool = True,
    class_name: str = "",
) -> str:
    """Render a single screenshot with overlays for both human and predicted actions.

    Args:
        base_screenshot: Path to screenshot image
        human_click: Human click position {x, y} (normalized 0-1)
        predicted_click: Predicted click position {x, y}
        width: Display width
        height: Display height
        show_distance: Show distance between clicks
        class_name: Additional CSS classes

    Returns:
        HTML string for overlay comparison
    """
    # Properly escape JSON for HTML attributes to prevent Alpine.js parsing errors
    human_json = html.escape(json.dumps(human_click)) if human_click else "null"
    predicted_json = html.escape(json.dumps(predicted_click)) if predicted_click else "null"
    extra_class = f" {class_name}" if class_name else ""

    return f'''<div class="oa-overlay-comparison{extra_class}"
     x-data="{{
         humanClick: {human_json},
         predictedClick: {predicted_json},

         get distance() {{
             if (!this.humanClick || !this.predictedClick) return null;
             const dx = this.humanClick.x - this.predictedClick.x;
             const dy = this.humanClick.y - this.predictedClick.y;
             return Math.sqrt(dx * dx + dy * dy);
         }},

         get isMatch() {{
             return this.distance !== null && this.distance < 0.05;
         }}
     }}"
     style="width: {width}px;">

    <!-- Screenshot with Overlays -->
    <div style="position: relative; width: 100%; height: {height}px; background: var(--oa-bg-tertiary); border-radius: var(--oa-border-radius-lg); overflow: hidden;">
        <img src="{base_screenshot or ''}" style="width: 100%; height: 100%; object-fit: contain;" x-show="'{base_screenshot}'">
        <div x-show="!'{base_screenshot}'" style="display: flex; align-items: center; justify-content: center; height: 100%; color: var(--oa-text-muted);">No screenshot</div>

        <!-- Human Click (green) -->
        <template x-if="humanClick">
            <div style="position: absolute; transform: translate(-50%, -50%); width: 32px; height: 32px; border-radius: 50%; border: 3px solid #22c55e; background: rgba(34, 197, 94, 0.2); display: flex; align-items: center; justify-content: center;"
                 :style="'left: ' + (humanClick.x * 100) + '%; top: ' + (humanClick.y * 100) + '%'">
                <span style="font-size: 11px; font-weight: bold; color: #22c55e;">H</span>
            </div>
        </template>

        <!-- Predicted Click (blue) -->
        <template x-if="predictedClick">
            <div style="position: absolute; transform: translate(-50%, -50%); width: 32px; height: 32px; border-radius: 50%; border: 3px solid #3b82f6; background: rgba(59, 130, 246, 0.2); display: flex; align-items: center; justify-content: center;"
                 :style="'left: ' + (predictedClick.x * 100) + '%; top: ' + (predictedClick.y * 100) + '%'">
                <span style="font-size: 11px; font-weight: bold; color: #3b82f6;">AI</span>
            </div>
        </template>

        <!-- Connection Line -->
        <template x-if="humanClick && predictedClick">
            <svg style="position: absolute; inset: 0; pointer-events: none;">
                <line :x1="humanClick.x * 100 + '%'"
                      :y1="humanClick.y * 100 + '%'"
                      :x2="predictedClick.x * 100 + '%'"
                      :y2="predictedClick.y * 100 + '%'"
                      stroke="var(--oa-text-muted)"
                      stroke-width="2"
                      stroke-dasharray="4 4"/>
            </svg>
        </template>
    </div>

    <!-- Distance Info -->
    <div x-show="{str(show_distance).lower()} && distance !== null" style="margin-top: var(--oa-space-md); padding: var(--oa-space-md); background: var(--oa-bg-secondary); border-radius: var(--oa-border-radius); display: flex; align-items: center; justify-content: space-between;">
        <div style="display: flex; align-items: center; gap: var(--oa-space-md);">
            <div style="display: flex; align-items: center; gap: var(--oa-space-sm);">
                <div style="width: 12px; height: 12px; border-radius: 50%; background: #22c55e;"></div>
                <span style="font-size: var(--oa-font-size-sm);">Human</span>
            </div>
            <div style="display: flex; align-items: center; gap: var(--oa-space-sm);">
                <div style="width: 12px; height: 12px; border-radius: 50%; background: #3b82f6;"></div>
                <span style="font-size: var(--oa-font-size-sm);">Predicted</span>
            </div>
        </div>
        <div style="display: flex; align-items: center; gap: var(--oa-space-sm);">
            <span style="font-size: var(--oa-font-size-sm); color: var(--oa-text-secondary);">Distance:</span>
            <span :class="isMatch ? 'oa-badge oa-badge-success' : 'oa-badge oa-badge-error'"
                  x-text="(distance * 100).toFixed(1) + '%'"></span>
        </div>
    </div>

    <!-- Legend -->
    <div style="display: flex; justify-content: center; gap: var(--oa-space-lg); margin-top: var(--oa-space-sm); font-size: var(--oa-font-size-xs); color: var(--oa-text-muted);">
        <span><span style="color: var(--oa-success);">H</span> = Human action</span>
        <span><span style="color: var(--oa-info);">AI</span> = Predicted action</span>
    </div>
</div>'''

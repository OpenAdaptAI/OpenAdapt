"""HTML generator for benchmark viewer.

This module generates standalone HTML files for visualizing
benchmark evaluation results with interactive features.
"""

from pathlib import Path

from openadapt_viewer.core.html_builder import HTMLBuilder
from openadapt_viewer.core.types import BenchmarkRun
from openadapt_viewer.recording_db import LegacyCaptureError
from openadapt_viewer.viewers.benchmark.data import create_sample_data, load_benchmark_data
from openadapt_viewer.viewers.benchmark.real_data_loader import load_real_capture_data


def generate_benchmark_html(
    data_path: Path | str | None = None,
    output_path: Path | str = "benchmark_viewer.html",
    standalone: bool = False,
    run_data: BenchmarkRun | None = None,
    use_real_data: bool = True,
) -> str:
    """Generate a standalone HTML viewer for benchmark results.

    Args:
        data_path: Path to benchmark results directory OR capture directory (optional if run_data provided)
        output_path: Where to write the HTML file
        standalone: If True, embed Plotly.js for offline viewing
        run_data: Pre-loaded BenchmarkRun data (optional, will load from data_path if not provided)
        use_real_data: If True (default), load real capture data from $OPENADAPT_CAPTURE_RECORDING

    Returns:
        Path to the generated HTML file

    Raises:
        LegacyCaptureError: If data_path holds a pre-2026-07-17 ``capture.db``.
            The message names the conversion command.

    POLICY: ALWAYS defaults to real data, from $OPENADAPT_CAPTURE_RECORDING.
    Set use_real_data=False ONLY for unit tests with sample data.
    """
    # Load data
    if run_data is not None:
        run = run_data
    elif data_path is not None:
        # Try to load as capture directory first, fall back to benchmark data
        try:
            run = load_real_capture_data(data_path)
        except LegacyCaptureError:
            # Re-raised ahead of the fallback because it is a subclass of
            # FileNotFoundError and the fallback would otherwise swallow it.
            # This directory is a recording, not a benchmark result directory,
            # and load_benchmark_data reads it as an empty one: the user got
            # "Generated: ..." and a viewer holding zero tasks, while the
            # conversion command sat unread in this exception.
            raise
        except (FileNotFoundError, ValueError, KeyError):
            # Fall back to benchmark data format
            run = load_benchmark_data(data_path)
    else:
        # DEFAULT: Use the real recording named by $OPENADAPT_CAPTURE_RECORDING
        if use_real_data:
            run = load_real_capture_data()
        else:
            # ONLY for unit tests: create sample data
            run = create_sample_data()

    # Generate HTML using template
    builder = HTMLBuilder()
    html = _generate_viewer_html(builder, run, standalone)

    # Write to file
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        f.write(html)

    return str(output_path)


def _generate_viewer_html(builder: HTMLBuilder, run: BenchmarkRun, standalone: bool) -> str:
    """Generate the complete HTML for the benchmark viewer.

    Uses PageBuilder and components instead of inline template.
    """
    import json

    from openadapt_viewer.builders.page_builder import PageBuilder
    from openadapt_viewer.components import (
        filter_bar,
        metrics_grid,
    )
    from openadapt_viewer.components.metrics import domain_stats_grid

    # Prepare data for template
    domain_stats = run.get_domain_stats()

    # Convert run data to JSON-serializable format
    tasks_data = []
    for task in run.tasks:
        # Find corresponding execution
        execution = next((e for e in run.executions if e.task_id == task.task_id), None)
        tasks_data.append({
            "task_id": task.task_id,
            "instruction": task.instruction,
            "domain": task.domain or "unknown",
            "difficulty": task.difficulty or "unknown",
            "success": execution.success if execution else False,
            "steps": [
                {
                    "step_number": s.step_number,
                    "action_type": s.action_type,
                    "action_details": s.action_details,
                    "reasoning": s.reasoning,
                    "screenshot_path": s.screenshot_path,
                }
                for s in (execution.steps if execution else [])
            ],
            "error": execution.error if execution else None,
        })

    # Build page using PageBuilder
    page = PageBuilder(
        title=f"Benchmark Viewer - {run.benchmark_name}",
        include_alpine=True,
    )

    # Add header
    page.add_header(
        title=run.benchmark_name,
        subtitle=f"Model: {run.model_id}",
    )

    # Add summary metrics
    page.add_section(
        metrics_grid([
            {"label": "Total Tasks", "value": run.total_tasks},
            {"label": "Passed", "value": run.passed_tasks, "color": "success"},
            {"label": "Failed", "value": run.failed_tasks, "color": "error"},
            {"label": "Success Rate", "value": f"{run.success_rate * 100:.1f}%", "color": "accent"},
        ], columns=4),
        title="Summary",
    )

    # Add domain breakdown
    page.add_section(
        domain_stats_grid(domain_stats),
        title="Results by Domain",
    )

    # Add filters
    domain_options = [{"value": domain, "label": domain.capitalize()} for domain in domain_stats]
    page.add_section(
        filter_bar(
            filters=[
                {"id": "domain", "label": "Domain", "options": domain_options},
                {"id": "status", "label": "Status", "options": [
                    {"value": "passed", "label": "Passed"},
                    {"value": "failed", "label": "Failed"},
                ]},
            ],
            alpine_data_name="viewer",
        ),
        title="Filters",
    )

    # Add task list and detail view section
    tasks_json = json.dumps(tasks_data)
    page.add_section(
        _generate_task_viewer_section(tasks_json),
        class_name="oa-task-viewer",
    )

    # Add custom CSS for task viewer layout
    page.add_css("""
        .oa-task-viewer {
            display: grid;
            grid-template-columns: 1fr 2fr;
            gap: var(--oa-space-lg);
        }
        @media (max-width: 1024px) {
            .oa-task-viewer {
                grid-template-columns: 1fr;
            }
        }
        .screenshot-container {
            aspect-ratio: 16/9;
            background: var(--oa-bg-tertiary);
            border-radius: var(--oa-border-radius);
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
        }
        .screenshot-container img {
            max-width: 100%;
            max-height: 100%;
            object-fit: contain;
        }

        /* Provenance badges */
        .oa-badge-ml {
            background: var(--oa-accent-dim);
            color: var(--oa-accent);
            border: 1px solid var(--oa-accent);
        }
        .oa-badge-raw {
            background: var(--oa-info-bg);
            color: var(--oa-info);
            border: 1px solid var(--oa-info);
        }

        /* Metadata display */
        .oa-metadata {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 12px;
        }
        .oa-metadata-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 8px;
            background: var(--oa-bg-secondary);
            border-radius: var(--oa-border-radius);
        }
        .oa-metadata-item .oa-label {
            font-weight: 600;
            color: var(--oa-text-secondary);
            font-size: 0.85rem;
        }
        .oa-metadata-item .oa-value {
            color: var(--oa-text-primary);
            font-family: var(--oa-font-mono);
            font-size: 0.85rem;
        }

        .oa-metadata-details summary:hover {
            color: var(--oa-accent);
        }
    """)

    # Add Alpine.js state management
    alpine_script = """
        document.addEventListener('alpine:init', () => {
            Alpine.data('viewer', () => ({
                tasks: TASKS_DATA_PLACEHOLDER,
                selectedTask: null,
                currentStep: 0,
                isPlaying: false,
                playbackSpeed: 1,
                playbackInterval: null,
                filters: {
                    domain: '',
                    status: '',
                },

                init() {
                    if (this.filteredTasks.length > 0) {
                        this.selectTask(this.filteredTasks[0]);
                    }
                },

                get filteredTasks() {
                    return this.tasks.filter(task => {
                        if (this.filters.domain && task.domain !== this.filters.domain) return false;
                        if (this.filters.status === 'passed' && !task.success) return false;
                        if (this.filters.status === 'failed' && task.success) return false;
                        return true;
                    });
                },

                selectTask(task) {
                    this.selectedTask = task;
                    this.currentStep = 0;
                    this.stopPlayback();
                },

                prevStep() {
                    if (this.currentStep > 0) this.currentStep--;
                },

                nextStep() {
                    if (this.selectedTask && this.currentStep < this.selectedTask.steps.length - 1) {
                        this.currentStep++;
                    }
                },

                togglePlayback() {
                    if (this.isPlaying) {
                        this.stopPlayback();
                    } else {
                        this.startPlayback();
                    }
                },

                startPlayback() {
                    this.isPlaying = true;
                    const interval = 1000 / this.playbackSpeed;
                    this.playbackInterval = setInterval(() => {
                        if (this.selectedTask && this.currentStep < this.selectedTask.steps.length - 1) {
                            this.currentStep++;
                        } else {
                            this.stopPlayback();
                        }
                    }, interval);
                },

                stopPlayback() {
                    this.isPlaying = false;
                    if (this.playbackInterval) {
                        clearInterval(this.playbackInterval);
                        this.playbackInterval = null;
                    }
                }
            }))
        });
    """.replace("TASKS_DATA_PLACEHOLDER", tasks_json)
    page.add_script(alpine_script)

    return page.render()


def _generate_task_viewer_section(tasks_json: str) -> str:
    """Generate the task list and detail viewer section using Alpine.js.

    This section provides a two-column layout with task list on the left
    and detail view on the right with step-by-step playback.

    Args:
        tasks_json: JSON string of tasks data

    Returns:
        HTML string for the task viewer section
    """
    return '''
    <div x-data="viewer">
        <!-- Task List -->
        <div class="oa-list">
            <div class="oa-list-header">
                <div class="oa-list-title">Tasks</div>
                <div class="oa-list-subtitle" x-text="'Showing ' + filteredTasks.length + ' of ' + tasks.length"></div>
            </div>
            <div class="oa-list-items">
                <template x-for="task in filteredTasks" :key="task.task_id">
                    <div class="oa-list-item"
                         @click="selectTask(task)"
                         :class="{'oa-list-item-selected': selectedTask?.task_id === task.task_id}">
                        <div class="oa-list-item-content">
                            <div style="flex: 1; min-width: 0;">
                                <div class="oa-list-item-title" x-text="task.task_id"></div>
                                <div class="oa-list-item-subtitle" x-text="task.instruction.length > 60 ? task.instruction.substring(0, 60) + '...' : task.instruction"></div>
                            </div>
                            <span class="oa-badge"
                                  :class="task.success ? 'oa-badge-success' : 'oa-badge-error'"
                                  x-text="task.success ? 'Pass' : 'Fail'"></span>
                        </div>
                    </div>
                </template>
            </div>
        </div>

        <!-- Detail View -->
        <div style="background: var(--oa-bg-secondary); border-radius: var(--oa-border-radius-lg); border: 1px solid var(--oa-border-color); overflow: hidden;">
            <template x-if="selectedTask">
                <div>
                    <!-- Task Header -->
                    <div style="padding: var(--oa-space-md); border-bottom: 1px solid var(--oa-border-color);">
                        <div style="display: flex; align-items: center; justify-content: space-between;">
                            <h2 style="font-weight: 600;" x-text="selectedTask.task_id"></h2>
                            <span class="oa-badge"
                                  :class="selectedTask.success ? 'oa-badge-success' : 'oa-badge-error'"
                                  x-text="selectedTask.success ? 'Passed' : 'Failed'"></span>
                        </div>
                        <p style="margin-top: 8px; color: var(--oa-text-secondary);" x-text="selectedTask.instruction"></p>
                        <div style="margin-top: 8px; display: flex; gap: 16px; font-size: 0.85rem; color: var(--oa-text-muted);">
                            <span x-text="'Domain: ' + selectedTask.domain"></span>
                            <span x-text="'Difficulty: ' + selectedTask.difficulty"></span>
                            <span x-text="'Steps: ' + selectedTask.steps.length"></span>
                        </div>
                        <template x-if="selectedTask.error">
                            <div style="margin-top: 8px; padding: 8px; background: var(--oa-error-bg); color: var(--oa-error); border-radius: var(--oa-border-radius); font-size: 0.85rem;" x-text="selectedTask.error"></div>
                        </template>
                    </div>

                    <!-- Step Viewer -->
                    <div style="padding: var(--oa-space-md);" x-show="selectedTask.steps.length > 0">
                        <!-- Playback Controls -->
                        <div class="oa-playback-controls" style="margin-bottom: 16px;">
                            <button class="oa-playback-btn" @click="currentStep = 0" :disabled="currentStep === 0" title="Go to start">
                                <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                                    <path d="M6 6h2v12H6V6zm3.5 6l8.5 6V6l-8.5 6z"/>
                                </svg>
                            </button>
                            <button class="oa-playback-btn" @click="prevStep()" :disabled="currentStep === 0" title="Previous">
                                <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                                    <path d="M15.41 7.41L14 6l-6 6 6 6 1.41-1.41L10.83 12z"/>
                                </svg>
                            </button>
                            <button class="oa-playback-btn oa-playback-btn-primary" @click="togglePlayback()" title="Play/Pause">
                                <svg x-show="!isPlaying" width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                                    <path d="M8 5v14l11-7z"/>
                                </svg>
                                <svg x-show="isPlaying" width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                                    <path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/>
                                </svg>
                            </button>
                            <button class="oa-playback-btn" @click="nextStep()" :disabled="currentStep >= selectedTask.steps.length - 1" title="Next">
                                <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                                    <path d="M10 6L8.59 7.41 13.17 12l-4.58 4.59L10 18l6-6z"/>
                                </svg>
                            </button>
                            <button class="oa-playback-btn" @click="currentStep = selectedTask.steps.length - 1" :disabled="currentStep >= selectedTask.steps.length - 1" title="Go to end">
                                <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                                    <path d="M6 18l8.5-6L6 6v12zm2-12v12h2V6h-2z" transform="scale(-1, 1) translate(-24, 0)"/>
                                </svg>
                            </button>
                            <span class="oa-playback-counter" x-text="'Step ' + (currentStep + 1) + ' of ' + selectedTask.steps.length"></span>
                            <select class="oa-playback-speed" x-model.number="playbackSpeed" @change="if (isPlaying) { stopPlayback(); startPlayback(); }">
                                <option value="0.5">0.5x</option>
                                <option value="1">1x</option>
                                <option value="2">2x</option>
                                <option value="4">4x</option>
                            </select>
                        </div>

                        <!-- Timeline -->
                        <div class="oa-timeline" style="margin-bottom: 16px;">
                            <div class="oa-timeline-track" @click="(e) => {
                                const rect = $el.getBoundingClientRect();
                                const clickX = e.clientX - rect.left;
                                const percent = clickX / rect.width;
                                currentStep = Math.floor(percent * selectedTask.steps.length);
                                if (currentStep >= selectedTask.steps.length) currentStep = selectedTask.steps.length - 1;
                            }">
                                <div class="oa-timeline-progress" :style="'width: ' + ((currentStep + 1) / selectedTask.steps.length * 100) + '%'"></div>
                            </div>
                        </div>

                        <!-- Current Step -->
                        <template x-if="selectedTask.steps[currentStep]">
                            <div>
                                <div class="screenshot-container" style="margin-bottom: 16px;">
                                    <template x-if="selectedTask.steps[currentStep].screenshot_path">
                                        <img :src="selectedTask.steps[currentStep].screenshot_path" alt="Screenshot">
                                    </template>
                                    <template x-if="!selectedTask.steps[currentStep].screenshot_path">
                                        <div style="color: var(--oa-text-muted); font-size: 0.85rem;">No screenshot available</div>
                                    </template>
                                </div>
                                <div class="oa-action" style="margin-bottom: 8px;">
                                    <!-- Provenance badge -->
                                    <span class="oa-badge oa-badge-ml"
                                          x-show="selectedTask.steps[currentStep].action_details.provenance === 'ml_inferred'"
                                          :title="'Generated by ' + selectedTask.steps[currentStep].action_details.model + ' with ' + (selectedTask.steps[currentStep].action_details.confidence * 100).toFixed(0) + '% confidence'">
                                        ML-INFERRED
                                    </span>
                                    <span class="oa-badge oa-badge-raw"
                                          x-show="selectedTask.steps[currentStep].action_details.provenance === 'raw'"
                                          title="Raw hardware event">
                                        RAW
                                    </span>
                                    <!-- Description -->
                                    <span class="oa-action-details" x-text="selectedTask.steps[currentStep].action_details.description"></span>
                                </div>
                                <!-- Provenance metadata (expandable) -->
                                <details class="oa-metadata-details" style="margin-bottom: 8px;">
                                    <summary style="cursor: pointer; color: var(--oa-text-muted); font-size: 0.85rem;">
                                        View Provenance & Metadata
                                    </summary>
                                    <div style="margin-top: 8px; padding: 12px; background: var(--oa-bg-tertiary); border-radius: var(--oa-border-radius); font-size: 0.85rem;">
                                        <div class="oa-metadata">
                                            <div class="oa-metadata-item" x-show="selectedTask.steps[currentStep].action_details.model">
                                                <span class="oa-label">Model:</span>
                                                <span class="oa-value" x-text="selectedTask.steps[currentStep].action_details.model"></span>
                                            </div>
                                            <div class="oa-metadata-item" x-show="selectedTask.steps[currentStep].action_details.confidence !== undefined">
                                                <span class="oa-label">Confidence:</span>
                                                <span class="oa-value" x-text="(selectedTask.steps[currentStep].action_details.confidence * 100).toFixed(1) + '%'"></span>
                                            </div>
                                            <div class="oa-metadata-item" x-show="selectedTask.steps[currentStep].action_details.source">
                                                <span class="oa-label">Source:</span>
                                                <span class="oa-value" x-text="selectedTask.steps[currentStep].action_details.source"></span>
                                            </div>
                                            <div class="oa-metadata-item" x-show="selectedTask.steps[currentStep].action_details.episode">
                                                <span class="oa-label">Episode:</span>
                                                <span class="oa-value" x-text="selectedTask.steps[currentStep].action_details.episode"></span>
                                            </div>
                                            <div class="oa-metadata-item" x-show="selectedTask.steps[currentStep].action_details.frame_index !== undefined && selectedTask.steps[currentStep].action_details.frame_index !== null">
                                                <span class="oa-label">Frame Index:</span>
                                                <span class="oa-value" x-text="selectedTask.steps[currentStep].action_details.frame_index"></span>
                                            </div>
                                        </div>
                                    </div>
                                </details>
                                <template x-if="selectedTask.steps[currentStep].reasoning">
                                    <div style="padding: 12px; background: var(--oa-bg-tertiary); border-radius: var(--oa-border-radius); font-size: 0.85rem;">
                                        <strong>Reasoning:</strong>
                                        <div style="color: var(--oa-text-secondary); margin-top: 4px;" x-text="selectedTask.steps[currentStep].reasoning"></div>
                                    </div>
                                </template>
                            </div>
                        </template>
                    </div>

                    <div style="padding: var(--oa-space-lg); text-align: center; color: var(--oa-text-muted);" x-show="selectedTask.steps.length === 0">
                        No steps recorded for this task
                    </div>
                </div>
            </template>

            <div x-show="!selectedTask" style="padding: 48px; text-align: center; color: var(--oa-text-muted);">
                Select a task from the list to view details
            </div>
        </div>
    </div>
    '''

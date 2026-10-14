"""Benchmark failure analysis component.

This component provides:
- Detailed failure breakdown by category
- Error pattern detection
- Failed step visualization
- Comparison with successful executions
- Statistics and trends
"""

from __future__ import annotations

import html
import json
from typing import TypedDict


class FailureRecord(TypedDict, total=False):
    """A single failure record for analysis."""

    task_id: str
    instruction: str
    error_type: str  # e.g., "wrong_action", "timeout", "element_not_found"
    error_message: str
    failed_step: int
    total_steps: int
    action_type: str  # The action that failed
    expected_action: dict | None  # What was expected
    actual_action: dict | None  # What happened
    screenshot: str  # Screenshot at failure point
    domain: str
    difficulty: str


class FailureCategory(TypedDict, total=False):
    """Failure category with counts."""

    category: str
    label: str
    count: int
    percentage: float
    color: str


# Default failure categories
DEFAULT_FAILURE_CATEGORIES = [
    {"category": "wrong_action", "label": "Wrong Action", "color": "#ef4444"},
    {"category": "wrong_target", "label": "Wrong Target", "color": "#f97316"},
    {"category": "timeout", "label": "Timeout", "color": "#f59e0b"},
    {"category": "element_not_found", "label": "Element Not Found", "color": "#eab308"},
    {"category": "navigation_error", "label": "Navigation Error", "color": "#84cc16"},
    {"category": "assertion_failed", "label": "Assertion Failed", "color": "#22c55e"},
    {"category": "crash", "label": "Crash/Exception", "color": "#14b8a6"},
    {"category": "unknown", "label": "Unknown", "color": "#6b7280"},
]


def failure_analysis_panel(
    failures: list[FailureRecord] | None = None,
    total_tasks: int = 0,
    show_categories: bool = True,
    show_list: bool = True,
    show_details: bool = True,
    on_select_failure: str | None = None,
    class_name: str = "",
) -> str:
    """Render a comprehensive failure analysis panel.

    Args:
        failures: List of failure records
        total_tasks: Total number of tasks for percentage calculation
        show_categories: Show failure category breakdown
        show_list: Show list of individual failures
        show_details: Show detailed failure info on selection
        on_select_failure: JavaScript callback when failure is selected
        class_name: Additional CSS classes

    Returns:
        HTML string for the failure analysis panel
    """
    failures = failures or []
    extra_class = f" {class_name}" if class_name else ""
    # Properly escape JSON for HTML attributes to prevent Alpine.js parsing errors
    failures_json = html.escape(json.dumps(failures))
    categories_json = html.escape(json.dumps(DEFAULT_FAILURE_CATEGORIES))

    return f'''<div class="oa-failure-analysis{extra_class}"
     x-data="{{
         failures: {failures_json},
         totalTasks: {total_tasks},
         categories: {categories_json},
         selectedFailure: null,
         filterCategory: '',
         searchQuery: '',

         get categoryStats() {{
             const stats = {{}};
             this.categories.forEach(c => {{
                 stats[c.category] = {{ ...c, count: 0, percentage: 0 }};
             }});

             this.failures.forEach(f => {{
                 const cat = f.error_type || 'unknown';
                 if (stats[cat]) {{
                     stats[cat].count++;
                 }} else {{
                     stats['unknown'].count++;
                 }}
             }});

             const total = this.failures.length;
             Object.values(stats).forEach(s => {{
                 s.percentage = total > 0 ? (s.count / total) * 100 : 0;
             }});

             return Object.values(stats).filter(s => s.count > 0).sort((a, b) => b.count - a.count);
         }},

         get filteredFailures() {{
             return this.failures.filter(f => {{
                 if (this.filterCategory && f.error_type !== this.filterCategory) return false;
                 if (this.searchQuery) {{
                     const q = this.searchQuery.toLowerCase();
                     return f.task_id?.toLowerCase().includes(q) ||
                            f.instruction?.toLowerCase().includes(q) ||
                            f.error_message?.toLowerCase().includes(q);
                 }}
                 return true;
             }});
         }},

         get failureRate() {{
             return this.totalTasks > 0 ? (this.failures.length / this.totalTasks * 100).toFixed(1) : 0;
         }},

         selectFailure(failure) {{
             this.selectedFailure = failure;
             {f"({on_select_failure})(failure);" if on_select_failure else ""}
         }},

         getCategoryColor(type) {{
             const cat = this.categories.find(c => c.category === type);
             return cat?.color || '#6b7280';
         }},

         getCategoryLabel(type) {{
             const cat = this.categories.find(c => c.category === type);
             return cat?.label || type;
         }}
     }}"
     style="display: flex; flex-direction: column; gap: var(--oa-space-lg);">

    <!-- Summary Stats -->
    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: var(--oa-space-md);">
        <div class="oa-metrics-card">
            <div class="oa-metrics-card-label">Total Failures</div>
            <div class="oa-metrics-card-value oa-metrics-error" x-text="failures.length"></div>
        </div>
        <div class="oa-metrics-card">
            <div class="oa-metrics-card-label">Failure Rate</div>
            <div class="oa-metrics-card-value oa-metrics-error" x-text="failureRate + '%'"></div>
        </div>
        <div class="oa-metrics-card">
            <div class="oa-metrics-card-label">Error Types</div>
            <div class="oa-metrics-card-value" x-text="categoryStats.length"></div>
        </div>
        <div class="oa-metrics-card">
            <div class="oa-metrics-card-label">Most Common</div>
            <div class="oa-metrics-card-value" style="font-size: var(--oa-font-size-md);" x-text="categoryStats[0]?.label || 'N/A'"></div>
        </div>
    </div>

    <!-- Category Breakdown -->
    <div x-show="{str(show_categories).lower()}" style="background: var(--oa-bg-secondary); border-radius: var(--oa-border-radius-lg); padding: var(--oa-space-lg);">
        <h3 style="margin: 0 0 var(--oa-space-md) 0; font-size: var(--oa-font-size-md); font-weight: 600;">Failure Categories</h3>

        <!-- Category Bar Chart -->
        <div style="display: flex; flex-direction: column; gap: var(--oa-space-sm);">
            <template x-for="cat in categoryStats" :key="cat.category">
                <div @click="filterCategory = filterCategory === cat.category ? '' : cat.category"
                     style="cursor: pointer;"
                     :style="filterCategory === cat.category ? 'opacity: 1' : (filterCategory ? 'opacity: 0.5' : '')">
                    <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 4px;">
                        <div style="display: flex; align-items: center; gap: var(--oa-space-sm);">
                            <div style="width: 12px; height: 12px; border-radius: 3px;" :style="'background: ' + cat.color"></div>
                            <span style="font-size: var(--oa-font-size-sm);" x-text="cat.label"></span>
                        </div>
                        <span style="font-size: var(--oa-font-size-sm); color: var(--oa-text-muted);">
                            <span x-text="cat.count"></span> (<span x-text="cat.percentage.toFixed(1)"></span>%)
                        </span>
                    </div>
                    <div style="height: 8px; background: var(--oa-bg-tertiary); border-radius: 4px; overflow: hidden;">
                        <div style="height: 100%; border-radius: 4px; transition: width 0.3s ease;"
                             :style="'width: ' + cat.percentage + '%; background: ' + cat.color"></div>
                    </div>
                </div>
            </template>
        </div>

        <!-- Clear Filter -->
        <button x-show="filterCategory"
                @click="filterCategory = ''"
                style="margin-top: var(--oa-space-md); padding: var(--oa-space-xs) var(--oa-space-sm); font-size: var(--oa-font-size-xs); background: var(--oa-bg-tertiary); border: 1px solid var(--oa-border-color); border-radius: var(--oa-border-radius); cursor: pointer; color: var(--oa-text-secondary);">
            Clear filter
        </button>
    </div>

    <!-- Failure List -->
    <div x-show="{str(show_list).lower()}" style="display: flex; gap: var(--oa-space-md);">
        <!-- List Panel -->
        <div style="flex: 1; min-width: 300px;">
            <!-- Search -->
            <input type="text"
                   x-model="searchQuery"
                   placeholder="Search failures..."
                   style="width: 100%; padding: var(--oa-space-sm) var(--oa-space-md); margin-bottom: var(--oa-space-md); background: var(--oa-bg-tertiary); border: 1px solid var(--oa-border-color); border-radius: var(--oa-border-radius); color: var(--oa-text-primary); font-size: var(--oa-font-size-sm);">

            <!-- Failure Items -->
            <div class="oa-list" style="max-height: 500px; overflow-y: auto;">
                <template x-for="failure in filteredFailures" :key="failure.task_id">
                    <div @click="selectFailure(failure)"
                         class="oa-list-item"
                         :class="{{'oa-list-item-selected': selectedFailure?.task_id === failure.task_id}}">
                        <div class="oa-list-item-content">
                            <div style="flex: 1; min-width: 0;">
                                <div class="oa-list-item-title" x-text="failure.task_id"></div>
                                <div class="oa-list-item-subtitle" x-text="failure.instruction"></div>
                                <div style="margin-top: var(--oa-space-xs); display: flex; align-items: center; gap: var(--oa-space-sm);">
                                    <span class="oa-badge"
                                          :style="'background: ' + getCategoryColor(failure.error_type) + '20; color: ' + getCategoryColor(failure.error_type)"
                                          x-text="getCategoryLabel(failure.error_type)"></span>
                                    <span style="font-size: var(--oa-font-size-xs); color: var(--oa-text-muted);"
                                          x-text="'Step ' + failure.failed_step + '/' + failure.total_steps"></span>
                                </div>
                            </div>
                        </div>
                    </div>
                </template>

                <!-- Empty State -->
                <div x-show="filteredFailures.length === 0" style="padding: var(--oa-space-lg); text-align: center; color: var(--oa-text-muted);">
                    No failures match your criteria
                </div>
            </div>
        </div>

        <!-- Detail Panel -->
        <div x-show="{str(show_details).lower()} && selectedFailure" style="flex: 1; min-width: 400px;">
            <div style="background: var(--oa-bg-secondary); border-radius: var(--oa-border-radius-lg); padding: var(--oa-space-lg);">
                <h3 style="margin: 0 0 var(--oa-space-md) 0; font-size: var(--oa-font-size-md); font-weight: 600;" x-text="selectedFailure?.task_id"></h3>

                <!-- Instruction -->
                <div style="padding: var(--oa-space-md); background: var(--oa-bg-tertiary); border-radius: var(--oa-border-radius); margin-bottom: var(--oa-space-md);">
                    <div style="font-size: var(--oa-font-size-xs); color: var(--oa-text-muted); margin-bottom: var(--oa-space-xs);">Instruction</div>
                    <div style="font-size: var(--oa-font-size-sm);" x-text="selectedFailure?.instruction"></div>
                </div>

                <!-- Error Details -->
                <div style="display: grid; grid-template-columns: repeat(2, 1fr); gap: var(--oa-space-md); margin-bottom: var(--oa-space-md);">
                    <div>
                        <div style="font-size: var(--oa-font-size-xs); color: var(--oa-text-muted);">Error Type</div>
                        <span class="oa-badge oa-badge-error" x-text="getCategoryLabel(selectedFailure?.error_type)"></span>
                    </div>
                    <div>
                        <div style="font-size: var(--oa-font-size-xs); color: var(--oa-text-muted);">Failed At</div>
                        <div x-text="'Step ' + selectedFailure?.failed_step + ' of ' + selectedFailure?.total_steps"></div>
                    </div>
                    <div>
                        <div style="font-size: var(--oa-font-size-xs); color: var(--oa-text-muted);">Domain</div>
                        <div x-text="selectedFailure?.domain || 'N/A'"></div>
                    </div>
                    <div>
                        <div style="font-size: var(--oa-font-size-xs); color: var(--oa-text-muted);">Difficulty</div>
                        <div x-text="selectedFailure?.difficulty || 'N/A'"></div>
                    </div>
                </div>

                <!-- Error Message -->
                <div style="padding: var(--oa-space-md); background: var(--oa-error-bg); border-radius: var(--oa-border-radius); margin-bottom: var(--oa-space-md);">
                    <div style="font-size: var(--oa-font-size-xs); color: var(--oa-error); margin-bottom: var(--oa-space-xs);">Error Message</div>
                    <div style="font-size: var(--oa-font-size-sm); color: var(--oa-error); font-family: var(--oa-font-mono);"
                         x-text="selectedFailure?.error_message || 'No error message'"></div>
                </div>

                <!-- Action Comparison -->
                <div x-show="selectedFailure?.expected_action || selectedFailure?.actual_action" style="margin-bottom: var(--oa-space-md);">
                    <div style="font-size: var(--oa-font-size-sm); font-weight: 600; margin-bottom: var(--oa-space-sm);">Action Comparison</div>
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: var(--oa-space-md);">
                        <div style="padding: var(--oa-space-md); background: var(--oa-success-bg); border-radius: var(--oa-border-radius);">
                            <div style="font-size: var(--oa-font-size-xs); color: var(--oa-success); margin-bottom: var(--oa-space-xs);">Expected</div>
                            <div style="font-family: var(--oa-font-mono); font-size: var(--oa-font-size-xs);"
                                 x-text="JSON.stringify(selectedFailure?.expected_action, null, 2)"></div>
                        </div>
                        <div style="padding: var(--oa-space-md); background: var(--oa-error-bg); border-radius: var(--oa-border-radius);">
                            <div style="font-size: var(--oa-font-size-xs); color: var(--oa-error); margin-bottom: var(--oa-space-xs);">Actual</div>
                            <div style="font-family: var(--oa-font-mono); font-size: var(--oa-font-size-xs);"
                                 x-text="JSON.stringify(selectedFailure?.actual_action, null, 2)"></div>
                        </div>
                    </div>
                </div>

                <!-- Screenshot -->
                <div x-show="selectedFailure?.screenshot">
                    <div style="font-size: var(--oa-font-size-sm); font-weight: 600; margin-bottom: var(--oa-space-sm);">Screenshot at Failure</div>
                    <div style="aspect-ratio: 16/9; background: var(--oa-bg-tertiary); border-radius: var(--oa-border-radius); overflow: hidden;">
                        <img :src="selectedFailure?.screenshot" style="width: 100%; height: 100%; object-fit: contain;">
                    </div>
                </div>
            </div>
        </div>

        <!-- Empty Selection State -->
        <div x-show="{str(show_details).lower()} && !selectedFailure"
             style="flex: 1; min-width: 400px; display: flex; align-items: center; justify-content: center; color: var(--oa-text-muted);">
            Select a failure to view details
        </div>
    </div>
</div>'''


def failure_summary_card(
    total_failures: int = 0,
    total_tasks: int = 0,
    top_error_type: str | None = None,
    top_error_count: int = 0,
    class_name: str = "",
) -> str:
    """Render a compact failure summary card.

    Args:
        total_failures: Number of failed tasks
        total_tasks: Total number of tasks
        top_error_type: Most common error type
        top_error_count: Count of most common error
        class_name: Additional CSS classes

    Returns:
        HTML string for failure summary card
    """
    extra_class = f" {class_name}" if class_name else ""
    failure_rate = (total_failures / total_tasks * 100) if total_tasks > 0 else 0

    return f'''<div class="oa-failure-summary-card{extra_class}"
     style="background: var(--oa-bg-secondary); border-radius: var(--oa-border-radius-lg); padding: var(--oa-space-lg); border-left: 4px solid var(--oa-error);">
    <div style="display: flex; align-items: flex-start; justify-content: space-between;">
        <div>
            <div style="font-size: var(--oa-font-size-xl); font-weight: 700; color: var(--oa-error);">{total_failures}</div>
            <div style="font-size: var(--oa-font-size-sm); color: var(--oa-text-secondary);">Failed Tasks</div>
        </div>
        <div style="text-align: right;">
            <div style="font-size: var(--oa-font-size-lg); font-weight: 600; color: var(--oa-text-primary);">{failure_rate:.1f}%</div>
            <div style="font-size: var(--oa-font-size-xs); color: var(--oa-text-muted);">of {total_tasks} total</div>
        </div>
    </div>
    {'<div style="margin-top: var(--oa-space-md); padding-top: var(--oa-space-md); border-top: 1px solid var(--oa-border-color);">' +
        '<div style="font-size: var(--oa-font-size-xs); color: var(--oa-text-muted);">Most Common Error</div>' +
        '<div style="display: flex; align-items: center; justify-content: space-between; margin-top: var(--oa-space-xs);">' +
            f'<span style="font-size: var(--oa-font-size-sm); font-weight: 500;">{top_error_type}</span>' +
            f'<span class="oa-badge oa-badge-error">{top_error_count}</span>' +
        '</div>' +
    '</div>' if top_error_type else ""}
</div>'''

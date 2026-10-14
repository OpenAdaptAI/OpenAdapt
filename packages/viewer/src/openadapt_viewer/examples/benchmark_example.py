"""Example: Benchmark results viewer using component library.

This shows how openadapt-evals can use the component library to
display benchmark evaluation results.

Usage:
    python -m openadapt_viewer.examples.benchmark_example
"""

from __future__ import annotations

from pathlib import Path

from openadapt_viewer.builders import PageBuilder
from openadapt_viewer.components import (
    filter_bar,
    metrics_grid,
    selectable_list,
)
from openadapt_viewer.components.metrics import domain_stats_grid


def generate_benchmark_viewer(
    benchmark_name: str = "Windows Agent Arena",
    model_id: str = "gpt-5.1",
    tasks: list[dict] | None = None,
    output_path: str | Path = "benchmark_viewer.html",
) -> Path:
    """Generate a benchmark results viewer.

    Args:
        benchmark_name: Name of the benchmark
        model_id: Model identifier
        tasks: List of task results
        output_path: Output HTML file path

    Returns:
        Path to generated HTML file
    """
    # Sample data if not provided
    if tasks is None:
        tasks = _generate_sample_tasks()

    # Calculate stats
    total = len(tasks)
    passed = sum(1 for t in tasks if t.get("success", False))
    failed = total - passed
    success_rate = (passed / total * 100) if total > 0 else 0

    # Domain stats
    domain_stats = {}
    for task in tasks:
        domain = task.get("domain", "unknown")
        if domain not in domain_stats:
            domain_stats[domain] = {"passed": 0, "failed": 0, "total": 0}
        domain_stats[domain]["total"] += 1
        if task.get("success", False):
            domain_stats[domain]["passed"] += 1
        else:
            domain_stats[domain]["failed"] += 1

    # Build page
    builder = PageBuilder(
        title=f"Benchmark Viewer - {benchmark_name}",
        include_alpine=True,
    )

    # Header
    builder.add_header(
        title=benchmark_name,
        subtitle=f"Model: {model_id}",
        nav_tabs=[
            {"href": "dashboard.html", "label": "Training"},
            {"href": "viewer.html", "label": "Viewer"},
            {"href": "benchmark.html", "label": "Benchmarks", "active": True},
        ],
    )

    # Summary metrics
    builder.add_section(
        metrics_grid([
            {"label": "Total Tasks", "value": total},
            {"label": "Passed", "value": passed, "color": "success"},
            {"label": "Failed", "value": failed, "color": "error"},
            {"label": "Success Rate", "value": f"{success_rate:.1f}%", "color": "accent"},
        ]),
        title="Summary",
    )

    # Domain breakdown
    builder.add_section(
        domain_stats_grid(domain_stats),
        title="Results by Domain",
    )

    # Filters
    domains = list(domain_stats.keys())
    builder.add_section(
        filter_bar(
            filters=[
                {
                    "id": "domain",
                    "label": "Domain",
                    "options": [{"value": d, "label": d.title()} for d in domains],
                },
                {
                    "id": "status",
                    "label": "Status",
                    "options": [
                        {"value": "passed", "label": "Passed"},
                        {"value": "failed", "label": "Failed"},
                    ],
                },
            ],
            search_placeholder="Search tasks...",
        ),
    )

    # Task list and detail view (would need Alpine.js bindings for full interactivity)
    task_items = [
        {
            "id": t.get("task_id", ""),
            "title": t.get("task_id", ""),
            "subtitle": t.get("instruction", "")[:60] + "...",
            "badge": "Pass" if t.get("success") else "Fail",
            "badge_color": "success" if t.get("success") else "error",
        }
        for t in tasks[:10]  # Show first 10 for example
    ]

    builder.add_section(f'''
        <div style="display: grid; grid-template-columns: 1fr 2fr; gap: 24px;">
            <div>
                {selectable_list(task_items, title="Tasks", subtitle=f"Showing {len(task_items)} of {total}")}
            </div>
            <div style="background: var(--oa-bg-secondary); border-radius: 12px; padding: 24px;">
                <div style="text-align: center; color: var(--oa-text-muted); padding: 40px;">
                    Select a task from the list to view details
                </div>
            </div>
        </div>
    ''')

    # Note about interactivity
    builder.add_section('''
        <div style="background: var(--oa-info-bg); border: 1px solid var(--oa-info); border-radius: 8px; padding: 16px; color: var(--oa-text-secondary);">
            <strong>Note:</strong> This is a static example. Full interactivity (task selection, step playback)
            requires Alpine.js data bindings. See the benchmark viewer generator for the complete implementation.
        </div>
    ''')

    return builder.render_to_file(output_path)


def _generate_sample_tasks() -> list[dict]:
    """Generate sample task data for demonstration."""
    import random

    domains = ["office", "browser", "system", "file_management", "communication"]
    instructions = [
        "Open Notepad and type 'Hello World'",
        "Navigate to google.com in Chrome",
        "Create a new folder on the Desktop",
        "Open the Windows Settings app",
        "Compose a new email in Outlook",
    ]

    tasks = []
    for i in range(20):
        task = {
            "task_id": f"task_{i+1:03d}",
            "instruction": random.choice(instructions),
            "domain": random.choice(domains),
            "success": random.random() > 0.3,
            "steps": [],
        }
        tasks.append(task)

    return tasks


if __name__ == "__main__":
    output = generate_benchmark_viewer()
    print(f"Generated: {output}")

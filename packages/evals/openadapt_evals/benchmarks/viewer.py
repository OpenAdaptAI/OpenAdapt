"""Benchmark viewer HTML generation.

This module generates a standalone HTML viewer for benchmark results,
showing task list with pass/fail status, step-by-step replay of
benchmark executions, screenshots, actions, and reasoning at each step.

Usage:
    from openadapt_evals.benchmarks.viewer import generate_benchmark_viewer

    # Generate viewer from benchmark results directory
    generate_benchmark_viewer(
        benchmark_dir=Path("benchmark_results/waa_eval_20241214"),
        output_path=Path("benchmark_results/waa_eval_20241214/benchmark.html"),
    )

Directory structure expected:
    benchmark_results/{run_name}/
    |-- metadata.json          # Benchmark config, models evaluated
    |-- summary.json           # Aggregate results
    |-- tasks/
    |   |-- task_001/
    |   |   |-- task.json      # Task definition
    |   |   |-- execution.json # Execution trace with steps
    |   |   |-- screenshots/   # Step screenshots
    |   |       |-- step_000.png
    |   |       |-- step_001.png
    |   |       |-- ...
    |   |-- task_002/
    |   |   |-- ...
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any

from openadapt_evals.adapters.base import (
    UNSCORED_BENCHMARK_ERROR_TYPES,
    normalize_benchmark_result_artifact,
)
from openadapt_evals.shared_ui import (
    get_keyboard_shortcuts_css,
    get_keyboard_shortcuts_js,
)

logger = logging.getLogger(__name__)


def _normalize_execution_artifact(
    execution: object,
    task_id: str,
) -> dict[str, Any]:
    """Preserve trace fields while normalizing its result envelope."""
    raw = dict(execution) if isinstance(execution, dict) else {}
    result = normalize_benchmark_result_artifact(
        execution,
        expected_task_id=task_id,
        context=f"viewer execution artifact for {task_id}",
    )
    raw.update(
        {
            "task_id": result.task_id,
            "success": result.success,
            "score": result.score,
            "num_steps": result.num_steps,
            "total_time_seconds": result.total_time_seconds,
            "error": result.error,
            "reason": result.reason,
            "error_type": result.error_type,
        }
    )
    return raw


def load_benchmark_metadata(benchmark_dir: Path) -> dict[str, Any]:
    """Load benchmark metadata from metadata.json.

    Args:
        benchmark_dir: Path to benchmark run directory.

    Returns:
        Metadata dictionary with benchmark_name, run_name, model_id, etc.
    """
    metadata_path = benchmark_dir / "metadata.json"
    if metadata_path.exists():
        with open(metadata_path) as f:
            return json.load(f)
    return {
        "benchmark_name": "unknown",
        "run_name": benchmark_dir.name,
        "model_id": "unknown",
        "created_at": None,
    }


def load_benchmark_summary(benchmark_dir: Path) -> dict[str, Any]:
    """Load benchmark summary from summary.json.

    Args:
        benchmark_dir: Path to benchmark run directory.

    Returns:
        Summary dictionary with success_rate, num_tasks, etc.
    """
    summary_path = benchmark_dir / "summary.json"
    if summary_path.exists():
        with open(summary_path) as f:
            return json.load(f)
    return {
        "num_tasks": 0,
        "num_success": 0,
        "success_rate": 0.0,
        "avg_score": 0.0,
        "avg_steps": 0.0,
        "tasks": [],
    }


def load_task_results(benchmark_dir: Path) -> list[dict[str, Any]]:
    """Load all task results from benchmark run.

    Args:
        benchmark_dir: Path to benchmark run directory.

    Returns:
        List of task dictionaries with task definition, execution trace,
        and screenshot paths.
    """
    tasks_dir = benchmark_dir / "tasks"
    if not tasks_dir.exists():
        return []

    results = []
    for task_dir in sorted(tasks_dir.iterdir()):
        if not task_dir.is_dir():
            continue

        task_data: dict[str, Any] = {
            "task_dir": str(task_dir),
            "task_id": task_dir.name,
        }

        # Load task definition
        task_json = task_dir / "task.json"
        if task_json.exists():
            with open(task_json) as f:
                task_data["definition"] = json.load(f)
        else:
            task_data["definition"] = {"task_id": task_dir.name, "instruction": ""}

        # Load execution trace
        execution_json = task_dir / "execution.json"
        if execution_json.exists():
            with open(execution_json) as f:
                execution = json.load(f)
        else:
            execution = None
        task_data["execution"] = _normalize_execution_artifact(
            execution,
            task_dir.name,
        )

        # Load screenshot paths
        screenshots_dir = task_dir / "screenshots"
        if screenshots_dir.exists():
            screenshot_paths = sorted(screenshots_dir.glob("*.png"))
            task_data["screenshots"] = [str(p.relative_to(benchmark_dir)) for p in screenshot_paths]
        else:
            task_data["screenshots"] = []

        results.append(task_data)

    return results


def _encode_image_to_base64(image_path: Path) -> str | None:
    """Encode image to base64 data URL for embedding in HTML.

    Args:
        image_path: Path to PNG image.

    Returns:
        Data URL string or None if image cannot be loaded.
    """
    try:
        if image_path.exists():
            with open(image_path, "rb") as f:
                data = f.read()
            return f"data:image/png;base64,{base64.b64encode(data).decode()}"
    except Exception as e:
        logger.warning(f"Failed to encode image {image_path}: {e}")
    return None


def _get_domain_stats(tasks: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Calculate per-domain statistics.

    Args:
        tasks: List of task result dictionaries.

    Returns:
        Dictionary mapping domain name to {total, success, fail} counts.
    """
    domain_stats: dict[str, dict[str, int]] = {}

    for task in tasks:
        domain = task.get("definition", {}).get("domain", "unknown")
        task_id = str(task.get("task_id") or "unknown")
        result = normalize_benchmark_result_artifact(
            task.get("execution"),
            expected_task_id=task_id,
            context=f"viewer domain artifact for {task_id}",
        )

        if domain not in domain_stats:
            domain_stats[domain] = {
                "total": 0,
                "outcomes": 0,
                "success": 0,
                "fail": 0,
                "unscored": 0,
            }

        domain_stats[domain]["total"] += 1
        if result.error_type in UNSCORED_BENCHMARK_ERROR_TYPES:
            domain_stats[domain]["unscored"] += 1
        elif result.success:
            domain_stats[domain]["outcomes"] += 1
            domain_stats[domain]["success"] += 1
        else:
            domain_stats[domain]["outcomes"] += 1
            domain_stats[domain]["fail"] += 1

    return domain_stats


def _get_shared_header_css() -> str:
    """Generate CSS for the shared dashboard header."""
    return '''
    .unified-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 12px 24px;
        background: linear-gradient(180deg, rgba(18,18,26,0.98) 0%, rgba(26,26,36,0.98) 100%);
        border-bottom: 1px solid rgba(255,255,255,0.08);
        margin-bottom: 20px;
        gap: 16px;
        flex-wrap: wrap;
        box-shadow: 0 2px 8px rgba(0,0,0,0.3);
    }
    .unified-header .nav-tabs {
        display: flex;
        align-items: center;
        gap: 4px;
        background: rgba(0,0,0,0.3);
        padding: 4px;
        border-radius: 8px;
    }
    .unified-header .nav-tab {
        padding: 8px 16px;
        border-radius: 6px;
        font-size: 0.85rem;
        font-weight: 500;
        text-decoration: none;
        color: var(--text-secondary);
        background: transparent;
        border: none;
        transition: all 0.2s;
        cursor: pointer;
    }
    .unified-header .nav-tab:hover {
        color: var(--text-primary);
        background: rgba(255,255,255,0.05);
    }
    .unified-header .nav-tab.active {
        color: var(--bg-primary);
        background: var(--accent);
        font-weight: 600;
    }
    '''


def _generate_shared_header_html(active_page: str) -> str:
    """Generate the shared header HTML.

    Args:
        active_page: Either "training", "viewer", or "benchmarks" to highlight the active tab

    Returns:
        HTML string for the header
    """
    benchmarks_active = "active" if active_page == "benchmarks" else ""

    return f'''
    <div class="unified-header">
        <div class="nav-tabs">
            <a href="dashboard.html" class="nav-tab">Training</a>
            <a href="viewer.html" class="nav-tab">Viewer</a>
            <a href="benchmark.html" class="nav-tab {benchmarks_active}">Benchmarks</a>
        </div>
    </div>
    '''


def generate_benchmark_viewer(
    benchmark_dir: Path,
    output_path: Path | None = None,
    embed_screenshots: bool = False,
    compact: bool = False,
) -> Path:
    """Generate HTML viewer for benchmark results.

    Args:
        benchmark_dir: Path to benchmark run directory containing metadata.json,
            summary.json, and tasks/ subdirectory.
        output_path: Path for output HTML file. Defaults to benchmark_dir/benchmark.html.
        embed_screenshots: If True, embed screenshots as base64 data URLs.
            This creates a larger but fully standalone HTML file.
        compact: If True, hide the navigation header, summary panel, and filter
            bar to maximize space for the task list and screenshots. Useful when
            generating screenshots for animations.

    Returns:
        Path to generated HTML file.
    """
    benchmark_dir = Path(benchmark_dir)
    if output_path is None:
        output_path = benchmark_dir / "benchmark.html"

    # Load all data
    metadata = load_benchmark_metadata(benchmark_dir)
    summary = load_benchmark_summary(benchmark_dir)
    tasks = load_task_results(benchmark_dir)

    # Calculate domain statistics
    domain_stats = _get_domain_stats(tasks)

    # Generate HTML
    html = _generate_benchmark_viewer_html(
        metadata=metadata,
        summary=summary,
        tasks=tasks,
        domain_stats=domain_stats,
        benchmark_dir=benchmark_dir,
        embed_screenshots=embed_screenshots,
        compact=compact,
    )

    # Write output
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html)

    logger.info(f"Generated benchmark viewer: {output_path}")
    return output_path


def _generate_benchmark_viewer_html(
    metadata: dict[str, Any],
    summary: dict[str, Any],
    tasks: list[dict[str, Any]],
    domain_stats: dict[str, dict[str, int]],
    benchmark_dir: Path,
    embed_screenshots: bool = False,
    compact: bool = False,
) -> str:
    """Generate the HTML content for benchmark viewer.

    Args:
        metadata: Benchmark metadata.
        summary: Summary statistics.
        tasks: List of task result dictionaries.
        domain_stats: Per-domain statistics.
        benchmark_dir: Base directory for resolving relative paths.
        embed_screenshots: If True, embed screenshots as base64.
        compact: If True, hide header, summary, and filter bar.

    Returns:
        HTML string.
    """
    # Get shared header components
    shared_header_css = _get_shared_header_css()
    shared_header_html = _generate_shared_header_html("benchmarks")

    # Get keyboard shortcuts components
    keyboard_shortcuts_css = get_keyboard_shortcuts_css()
    keyboard_shortcuts_js = get_keyboard_shortcuts_js()

    # Serialize data for JavaScript
    metadata_json = json.dumps(metadata)
    summary_json = json.dumps(summary)
    domain_stats_json = json.dumps(domain_stats)

    # Process tasks for JavaScript - include execution steps and screenshot paths
    tasks_for_js = []
    for task in tasks:
        task_js = {
            "task_id": task.get("task_id"),
            "definition": task.get("definition", {}),
            "execution": task.get("execution", {}),
            "screenshots": task.get("screenshots", []),
        }

        # Optionally embed screenshots as base64
        if embed_screenshots:
            embedded_screenshots = []
            for screenshot_rel_path in task.get("screenshots", []):
                screenshot_path = benchmark_dir / screenshot_rel_path
                data_url = _encode_image_to_base64(screenshot_path)
                embedded_screenshots.append(data_url or "")
            task_js["embedded_screenshots"] = embedded_screenshots

        tasks_for_js.append(task_js)

    tasks_json = json.dumps(tasks_for_js)

    # Calculate aggregate metrics
    num_tasks = len(tasks)
    executions = [
        normalize_benchmark_result_artifact(
            task.get("execution"),
            expected_task_id=str(task.get("task_id") or "unknown"),
            context="viewer aggregate artifact",
        )
        for task in tasks
    ]
    outcomes = [
        result
        for result in executions
        if result.error_type not in UNSCORED_BENCHMARK_ERROR_TYPES
    ]
    num_outcomes = len(outcomes)
    num_success = sum(1 for result in outcomes if result.success)
    num_failed = num_outcomes - num_success
    num_unscored = num_tasks - num_outcomes
    success_rate = (
        num_success / num_outcomes * 100 if num_outcomes > 0 else 0
    )

    body_class = ' class="compact"' if compact else ''

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Benchmark Viewer - {metadata.get("run_name", "Unknown")}</title>
    <style>
        :root {{
            --bg-primary: #0a0a0f;
            --bg-secondary: #12121a;
            --bg-tertiary: #1a1a24;
            --border-color: rgba(255, 255, 255, 0.06);
            --text-primary: #f0f0f0;
            --text-secondary: #888;
            --text-muted: #555;
            --accent: #00d4aa;
            --accent-dim: rgba(0, 212, 170, 0.15);
            --success: #34d399;
            --error: #ff5f5f;
            --warning: #f59e0b;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: "SF Pro Display", -apple-system, BlinkMacSystemFont, "Inter", sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            min-height: 100vh;
            line-height: 1.5;
        }}
        .container {{
            max-width: 1600px;
            margin: 0 auto;
            padding: 24px;
        }}
        {shared_header_css}

        /* Summary Panel */
        .summary-panel {{
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 24px;
        }}
        .summary-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 16px;
        }}
        .summary-header h2 {{
            font-size: 1rem;
            font-weight: 600;
        }}
        .summary-meta {{
            font-size: 0.75rem;
            color: var(--text-secondary);
            font-family: "SF Mono", Monaco, monospace;
        }}
        .summary-stats {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
            gap: 16px;
            margin-bottom: 16px;
        }}
        .stat-card {{
            background: var(--bg-tertiary);
            border-radius: 8px;
            padding: 16px;
        }}
        .stat-card .stat-value {{
            font-size: 1.8rem;
            font-weight: 600;
            font-family: "SF Mono", Monaco, monospace;
        }}
        .stat-card .stat-value.success {{ color: var(--success); }}
        .stat-card .stat-value.error {{ color: var(--error); }}
        .stat-card .stat-label {{
            font-size: 0.7rem;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-top: 4px;
        }}

        /* Domain breakdown */
        .domain-breakdown {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
        }}
        .domain-tag {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 6px 12px;
            background: var(--bg-tertiary);
            border-radius: 6px;
            font-size: 0.75rem;
        }}
        .domain-tag .domain-name {{
            color: var(--text-primary);
        }}
        .domain-tag .domain-stats {{
            font-family: "SF Mono", Monaco, monospace;
            color: var(--text-secondary);
        }}

        /* Filters */
        .filter-bar {{
            display: flex;
            gap: 16px;
            padding: 12px 16px;
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            margin-bottom: 16px;
            flex-wrap: wrap;
            align-items: center;
        }}
        .filter-group {{
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .filter-label {{
            font-size: 0.7rem;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}
        .filter-select {{
            padding: 8px 32px 8px 12px;
            border-radius: 8px;
            font-size: 0.85rem;
            background: var(--bg-tertiary);
            color: var(--text-primary);
            border: 1px solid var(--border-color);
            cursor: pointer;
            appearance: none;
            background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23888' d='M3 4.5L6 7.5L9 4.5'/%3E%3C/svg%3E");
            background-repeat: no-repeat;
            background-position: right 10px center;
            transition: all 0.2s;
        }}
        .filter-select:hover {{ border-color: var(--accent); }}
        .filter-count {{
            font-size: 0.8rem;
            color: var(--text-secondary);
            margin-left: auto;
        }}
        .search-container {{
            display: flex;
            align-items: center;
            gap: 8px;
            flex: 1;
            max-width: 400px;
        }}
        .search-input {{
            flex: 1;
            padding: 8px 12px;
            border-radius: 8px;
            font-size: 0.85rem;
            background: var(--bg-tertiary);
            color: var(--text-primary);
            border: 1px solid var(--border-color);
            transition: all 0.2s;
        }}
        .search-input:focus {{
            outline: none;
            border-color: var(--accent);
            box-shadow: 0 0 0 2px rgba(0, 212, 170, 0.15);
        }}
        .search-input::placeholder {{
            color: var(--text-muted);
        }}
        .search-clear-btn {{
            padding: 6px 12px;
            border-radius: 6px;
            font-size: 0.75rem;
            background: var(--bg-tertiary);
            color: var(--text-secondary);
            border: 1px solid var(--border-color);
            cursor: pointer;
            transition: all 0.2s;
        }}
        .search-clear-btn:hover {{
            border-color: var(--accent);
            color: var(--text-primary);
        }}

        /* Main Content Layout */
        .main-content {{
            display: grid;
            grid-template-columns: 350px 1fr;
            gap: 24px;
        }}
        @media (max-width: 1200px) {{
            .main-content {{ grid-template-columns: 1fr; }}
        }}

        /* Task List */
        .task-list {{
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            max-height: calc(100vh - 300px);
            overflow-y: auto;
        }}
        .task-list-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 14px 16px;
            border-bottom: 1px solid var(--border-color);
            position: sticky;
            top: 0;
            background: var(--bg-secondary);
            z-index: 10;
        }}
        .task-list-header h3 {{
            font-size: 0.9rem;
            font-weight: 600;
        }}
        .task-item {{
            padding: 12px 16px;
            border-bottom: 1px solid var(--border-color);
            cursor: pointer;
            transition: background 0.2s;
        }}
        .task-item:hover {{ background: var(--bg-tertiary); }}
        .task-item.active {{
            background: var(--accent-dim);
            border-left: 3px solid var(--accent);
        }}
        .task-item.hidden {{ display: none; }}
        .task-item .task-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 4px;
        }}
        .task-item .task-id {{
            font-family: "SF Mono", Monaco, monospace;
            font-size: 0.8rem;
            font-weight: 600;
        }}
        .task-item .task-status {{
            font-size: 0.7rem;
            font-weight: 600;
            padding: 2px 8px;
            border-radius: 4px;
        }}
        .task-item .task-status.success {{
            background: rgba(52, 211, 153, 0.2);
            color: var(--success);
        }}
        .task-item .task-status.fail {{
            background: rgba(255, 95, 95, 0.2);
            color: var(--error);
        }}
        .task-item .task-status.infra {{
            background: rgba(245, 158, 11, 0.2);
            color: #f59e0b;
        }}
        .task-item .task-status.unscored {{
            background: rgba(245, 158, 11, 0.2);
            color: #f59e0b;
        }}
        .task-item .task-info {{
            font-size: 0.75rem;
            color: var(--text-secondary);
        }}
        .task-item .task-domain {{
            color: var(--accent);
        }}

        /* Task Detail Panel */
        .task-detail {{
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            overflow: hidden;
        }}
        .task-detail-header {{
            padding: 16px 20px;
            border-bottom: 1px solid var(--border-color);
        }}
        .task-detail-header h2 {{
            font-size: 1rem;
            font-weight: 600;
            margin-bottom: 8px;
        }}
        .task-detail-meta {{
            font-size: 0.8rem;
            color: var(--text-secondary);
            line-height: 1.6;
        }}
        .task-detail-instruction {{
            font-style: italic;
            color: var(--text-primary);
            margin-top: 8px;
            padding: 10px;
            background: var(--bg-tertiary);
            border-radius: 6px;
            font-size: 0.85rem;
        }}

        /* Step Viewer */
        .step-viewer {{
            display: grid;
            grid-template-columns: 1fr 300px;
            gap: 16px;
            padding: 16px;
        }}
        @media (max-width: 900px) {{
            .step-viewer {{ grid-template-columns: 1fr; }}
        }}
        .screenshot-container {{
            background: #000;
            border-radius: 8px;
            overflow: hidden;
            min-height: 400px;
            display: flex;
            align-items: center;
            justify-content: center;
        }}
        .img-wrapper {{
            position: relative;
            display: inline-block;
            line-height: 0;
        }}
        .img-wrapper img {{
            max-width: 100%;
            max-height: 70vh;
        }}
        .screenshot-placeholder {{
            color: var(--text-muted);
            font-size: 0.9rem;
        }}
        .click-marker {{
            position: absolute;
            width: 24px;
            height: 24px;
            border-radius: 50%;
            transform: translate(-50%, -50%);
            display: none;
            pointer-events: none;
            z-index: 100;
            background: rgba(167, 139, 250, 0.4);
            border: 2px solid #a78bfa;
            color: #a78bfa;
        }}

        /* Step Controls */
        .step-sidebar {{
            display: flex;
            flex-direction: column;
            gap: 16px;
        }}
        .step-controls {{
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
            align-items: center;
        }}
        .step-btn {{
            padding: 8px 12px;
            border: 1px solid var(--border-color);
            background: var(--bg-tertiary);
            color: var(--text-primary);
            border-radius: 6px;
            cursor: pointer;
            font-size: 0.85rem;
            min-width: 40px;
            text-align: center;
            transition: all 0.2s;
        }}
        .step-btn:hover {{ border-color: var(--accent); }}
        .step-btn.primary {{ flex: 1; min-width: 60px; }}
        .step-btn.active {{
            background: var(--accent);
            color: var(--bg-primary);
            border-color: var(--accent);
        }}
        .step-progress {{
            font-size: 0.8rem;
            color: var(--text-secondary);
            font-family: "SF Mono", Monaco, monospace;
        }}

        /* Action Timeline */
        .action-timeline {{
            display: flex;
            height: 28px;
            border-radius: 6px;
            overflow: hidden;
            cursor: pointer;
            background: var(--bg-tertiary);
            border: 1px solid var(--border-color);
        }}
        .timeline-seg {{
            position: relative;
            min-width: 4px;
            transition: opacity 0.15s;
        }}
        .timeline-seg:hover {{ opacity: 0.8; }}
        .timeline-seg.active {{ box-shadow: inset 0 0 0 2px #fff; }}
        .timeline-seg .tl-tip {{
            display: none;
            position: absolute;
            bottom: 100%;
            left: 50%;
            transform: translateX(-50%);
            background: var(--bg-primary);
            color: var(--text-primary);
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 0.7rem;
            white-space: nowrap;
            z-index: 200;
            border: 1px solid var(--border-color);
            pointer-events: none;
        }}
        .timeline-seg:hover .tl-tip {{ display: block; }}

        /* Heatmap Canvas */
        .heatmap-canvas {{
            position: absolute;
            top: 0;
            left: 0;
            pointer-events: none;
            z-index: 50;
        }}
        .heatmap-toggle {{
            display: flex;
            align-items: center;
            gap: 6px;
            font-size: 0.75rem;
            color: var(--text-secondary);
            cursor: pointer;
            user-select: none;
        }}
        .heatmap-toggle input {{ accent-color: var(--accent); }}

        /* Step List */
        .step-list {{
            background: var(--bg-tertiary);
            border-radius: 8px;
            max-height: 300px;
            overflow-y: auto;
        }}
        .step-list-item {{
            padding: 10px 12px;
            border-bottom: 1px solid var(--border-color);
            cursor: pointer;
            transition: background 0.2s;
            font-size: 0.8rem;
        }}
        .step-list-item:hover {{ background: var(--bg-secondary); }}
        .step-list-item.active {{
            background: var(--accent-dim);
            border-left: 2px solid var(--accent);
        }}
        .step-list-item .step-num {{
            font-weight: 600;
            color: var(--accent);
            margin-right: 8px;
        }}
        .step-list-item .step-action {{
            color: var(--text-secondary);
        }}

        /* Action Detail */
        .action-detail {{
            background: var(--bg-tertiary);
            border-radius: 8px;
            padding: 12px;
        }}
        .action-detail h4 {{
            font-size: 0.8rem;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 8px;
        }}
        .action-content {{
            font-family: "SF Mono", Monaco, monospace;
            font-size: 0.8rem;
            color: var(--text-primary);
            word-break: break-word;
        }}
        .reasoning-box {{
            margin-top: 12px;
            padding: 10px;
            background: var(--bg-secondary);
            border-radius: 6px;
            font-size: 0.8rem;
            color: var(--text-secondary);
            line-height: 1.6;
            max-height: 200px;
            overflow-y: auto;
        }}
        .reasoning-box h4 {{
            margin-bottom: 8px;
        }}

        /* Agent Thinking Panel */
        .thinking-panel {{
            margin-top: 12px;
            background: var(--bg-secondary);
            border-radius: 8px;
            border: 1px solid var(--border);
            overflow: hidden;
        }}
        .thinking-header {{
            padding: 10px 12px;
            cursor: pointer;
            display: flex;
            align-items: center;
        }}
        .thinking-header:hover {{
            background: var(--bg-tertiary);
        }}
        .thinking-header h4 {{
            font-size: 0.8rem;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            display: flex;
            align-items: center;
            gap: 6px;
        }}
        .thinking-body {{
            padding: 0 12px 12px;
        }}
        .thinking-section {{
            margin-top: 8px;
        }}
        .thinking-section h5 {{
            font-size: 0.75rem;
            color: var(--text-muted);
            margin-bottom: 4px;
        }}
        .thinking-pre {{
            font-family: "SF Mono", Monaco, monospace;
            font-size: 0.75rem;
            line-height: 1.5;
            color: var(--text-secondary);
            background: var(--bg-primary);
            border-radius: 4px;
            padding: 8px;
            max-height: 250px;
            overflow-y: auto;
            white-space: pre-wrap;
            word-break: break-word;
        }}
        .thinking-prompt-pre {{
            max-height: 150px;
        }}
        .thinking-badge {{
            display: inline-block;
            font-size: 0.65rem;
            padding: 1px 6px;
            border-radius: 4px;
            font-weight: 600;
            text-transform: none;
            letter-spacing: 0;
        }}
        .thinking-badge.parse {{ background: rgba(88,166,255,0.15); color: #58a6ff; }}
        .thinking-badge.demo {{ background: rgba(63,185,80,0.15); color: #3fb950; }}
        .thinking-badge.loop {{ background: rgba(248,81,73,0.15); color: #f85149; }}
        .thinking-badge.tokens {{ background: rgba(210,153,34,0.15); color: #d29922; }}
        .thinking-badge.time {{ background: rgba(163,113,247,0.15); color: #a371f7; }}

        /* Speed Control */
        .speed-control {{
            display: flex;
            align-items: center;
            gap: 6px;
            margin-left: auto;
        }}
        .speed-control label {{
            font-size: 0.7rem;
            color: var(--text-muted);
            text-transform: uppercase;
        }}
        .speed-control select {{
            padding: 4px 8px;
            border-radius: 4px;
            background: var(--bg-tertiary);
            color: var(--text-primary);
            border: 1px solid var(--border-color);
            font-size: 0.8rem;
            cursor: pointer;
        }}

        /* Progress Bar */
        .progress-bar {{
            width: 100%;
            height: 4px;
            background: var(--bg-tertiary);
            border-radius: 2px;
            margin-top: 8px;
            overflow: hidden;
            cursor: pointer;
        }}
        .progress-bar .progress {{
            height: 100%;
            background: var(--accent);
            transition: width 0.1s ease;
        }}

        /* No task selected state */
        .no-task-selected {{
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            min-height: 400px;
            color: var(--text-muted);
        }}
        .no-task-selected .icon {{
            font-size: 3rem;
            margin-bottom: 16px;
        }}
        .no-task-selected p {{
            font-size: 0.9rem;
        }}

        /* Log Panel */
        .log-panel {{
            background: var(--bg-tertiary);
            border-radius: 8px;
            margin-top: 16px;
            overflow: hidden;
        }}
        .log-panel-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 12px 16px;
            background: var(--bg-secondary);
            border-bottom: 1px solid var(--border-color);
            cursor: pointer;
            user-select: none;
        }}
        .log-panel-header:hover {{
            background: var(--bg-tertiary);
        }}
        .log-panel-header h4 {{
            font-size: 0.85rem;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .log-panel-header .expand-icon {{
            transition: transform 0.2s;
        }}
        .log-panel-header .expand-icon.collapsed {{
            transform: rotate(-90deg);
        }}
        .log-controls {{
            display: flex;
            gap: 8px;
            padding: 8px 16px;
            background: var(--bg-secondary);
            border-bottom: 1px solid var(--border-color);
        }}
        .log-controls.collapsed {{
            display: none;
        }}
        .log-search {{
            flex: 1;
            padding: 6px 10px;
            border-radius: 6px;
            background: var(--bg-tertiary);
            color: var(--text-primary);
            border: 1px solid var(--border-color);
            font-size: 0.8rem;
            font-family: "SF Mono", Monaco, monospace;
        }}
        .log-filter-btn {{
            padding: 6px 12px;
            border-radius: 6px;
            background: var(--bg-tertiary);
            color: var(--text-secondary);
            border: 1px solid var(--border-color);
            font-size: 0.75rem;
            cursor: pointer;
            transition: all 0.2s;
        }}
        .log-filter-btn:hover {{
            border-color: var(--accent);
        }}
        .log-filter-btn.active {{
            background: var(--accent-dim);
            border-color: var(--accent);
            color: var(--accent);
        }}
        .log-container {{
            max-height: 300px;
            overflow-y: auto;
            font-family: "SF Mono", Monaco, monospace;
            font-size: 0.75rem;
        }}
        .log-container.collapsed {{
            display: none;
        }}
        .log-entry {{
            padding: 6px 16px;
            border-bottom: 1px solid var(--border-color);
            display: grid;
            grid-template-columns: 60px 70px 1fr;
            gap: 12px;
            align-items: start;
        }}
        .log-entry.hidden {{
            display: none;
        }}
        .log-entry:hover {{
            background: var(--bg-secondary);
        }}
        .log-timestamp {{
            color: var(--text-muted);
            white-space: nowrap;
        }}
        .log-level {{
            font-weight: 600;
            white-space: nowrap;
        }}
        .log-level.INFO {{
            color: var(--text-primary);
        }}
        .log-level.WARNING {{
            color: var(--warning);
        }}
        .log-level.ERROR {{
            color: var(--error);
        }}
        .log-level.SUCCESS {{
            color: var(--success);
        }}
        .log-message {{
            color: var(--text-primary);
            word-break: break-word;
        }}
        .log-empty {{
            padding: 24px;
            text-align: center;
            color: var(--text-muted);
            font-size: 0.8rem;
        }}

        /* Keyboard Shortcuts */
        {keyboard_shortcuts_css}

        /* Compact mode: hide chrome to maximize screenshot area */
        body.compact .unified-header {{ display: none; }}
        body.compact .summary-panel {{ display: none; }}
        body.compact .filter-bar {{ display: none; }}
        body.compact .keyboard-hint {{ display: none; }}
        body.compact .container {{ padding: 8px; }}
        body.compact .main-content {{ gap: 8px; }}
        body.compact .task-list {{ max-height: calc(100vh - 40px); }}
        body.compact .task-detail-header {{ padding: 8px 12px; }}
        body.compact .task-detail-header h2 {{ font-size: 0.85rem; margin-bottom: 4px; }}
        body.compact .task-detail-instruction {{ padding: 6px; margin-top: 4px; font-size: 0.8rem; }}
        body.compact .step-viewer {{ padding: 8px; gap: 8px; }}
        body.compact .log-panel {{ display: none; }}
    </style>
</head>
<body{body_class}>
    {shared_header_html}

    <div class="container">
        <!-- Summary Panel -->
        <div class="summary-panel">
            <div class="summary-header">
                <h2>Benchmark Results: {metadata.get("run_name", "Unknown")}</h2>
                <div class="summary-meta">
                    <span>Model: {metadata.get("model_id", "unknown")}</span>
                    <span> | </span>
                    <span>Created: {metadata.get("created_at", "N/A")}</span>
                </div>
            </div>
            <div class="summary-stats">
                <div class="stat-card">
                    <div class="stat-value">{num_tasks}</div>
                    <div class="stat-label">Total Tasks</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{num_outcomes}</div>
                    <div class="stat-label">Measured Outcomes</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value success">{num_success}</div>
                    <div class="stat-label">Passed</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value error">{num_failed}</div>
                    <div class="stat-label">Failed</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value" style="color:#f59e0b;">{num_unscored}</div>
                    <div class="stat-label">Unscored Errors</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value {'success' if success_rate >= 50 else 'error'}">{success_rate:.1f}%</div>
                    <div class="stat-label">Success Rate</div>
                </div>
            </div>
            <div class="domain-breakdown" id="domain-breakdown"></div>
        </div>

        <!-- Filters -->
        <div class="filter-bar">
            <div class="search-container">
                <input
                    type="text"
                    id="search-input"
                    class="search-input"
                    placeholder="Search tasks... (Ctrl+F / Cmd+F)"
                    title="Search by task ID, instruction, or action type"
                />
                <button class="search-clear-btn" id="search-clear-btn" title="Clear search">Clear</button>
            </div>
            <div class="filter-group">
                <span class="filter-label">Domain:</span>
                <select class="filter-select" id="domain-filter">
                    <option value="all">All Domains</option>
                </select>
            </div>
            <div class="filter-group">
                <span class="filter-label">Status:</span>
                <select class="filter-select" id="status-filter">
                    <option value="all">All</option>
                    <option value="success">Passed</option>
                    <option value="fail">Failed</option>
                    <option value="infra">Infra</option>
                    <option value="unscored">Unscored</option>
                </select>
            </div>
            <span class="filter-count" id="filter-count">{num_tasks} tasks</span>
        </div>

        <!-- Main Content -->
        <div class="main-content">
            <!-- Task List -->
            <div class="task-list">
                <div class="task-list-header">
                    <h3>Tasks</h3>
                </div>
                <div id="task-list-items"></div>
            </div>

            <!-- Task Detail Panel -->
            <div class="task-detail" id="task-detail">
                <div class="no-task-selected" id="no-task-selected">
                    <div class="icon">+</div>
                    <p>Select a task from the list to view details</p>
                </div>
                <div id="task-detail-content" style="display:none;"></div>
            </div>
        </div>

        <div class="keyboard-hint">
            Keyboard: Space (play/pause) | ← → (prev/next) | Home/End (first/last) | 1-5 (speed) | <a onclick="KeyboardShortcuts.showShortcutsOverlay()">? (show all shortcuts)</a>
        </div>
    </div>

    <script>
    // Data from Python
    const metadata = {metadata_json};
    const summary = {summary_json};
    const domainStats = {domain_stats_json};
    const tasks = {tasks_json};
    const embedScreenshots = {'true' if embed_screenshots else 'false'};

    let currentTaskIndex = -1;
    let currentStepIndex = 0;
    let isPlaying = false;
    let playInterval = null;
    let playSpeed = 1000;

    // Initialize page
    function init() {{
        renderDomainBreakdown();
        populateDomainFilter();
        renderTaskList();
        setupFilters();
    }}

    function renderDomainBreakdown() {{
        const container = document.getElementById('domain-breakdown');
        let html = '';
        for (const [domain, stats] of Object.entries(domainStats)) {{
            const rate = stats.outcomes > 0 ? (stats.success / stats.outcomes * 100).toFixed(0) : 0;
            html += `
                <div class="domain-tag">
                    <span class="domain-name">${{domain}}</span>
                    <span class="domain-stats">${{stats.success}}/${{stats.outcomes}} (${{rate}}%) · ${{stats.total}} attempts</span>
                </div>
            `;
        }}
        container.innerHTML = html;
    }}

    function populateDomainFilter() {{
        const select = document.getElementById('domain-filter');
        for (const domain of Object.keys(domainStats).sort()) {{
            const option = document.createElement('option');
            option.value = domain;
            option.textContent = domain;
            select.appendChild(option);
        }}
    }}

    function renderTaskList() {{
        const container = document.getElementById('task-list-items');
        let html = '';
        tasks.forEach((task, idx) => {{
            const def = task.definition || {{}};
            const exec = task.execution || {{}};
            const success = exec.success === true;
            const errorType = exec.error_type || '';
            const isInfra = errorType === 'infrastructure';
            const isUnscored = isInfra || errorType === 'evaluation';
            const statusKey = success ? 'success' : (isInfra ? 'infra' : (isUnscored ? 'unscored' : 'fail'));
            const statusLabel = success ? 'PASS' : (isInfra ? 'INFRA' : (isUnscored ? 'UNSCORED' : 'FAIL'));
            const domain = def.domain || 'unknown';
            const numSteps = exec.num_steps || 0;

            html += `
                <div class="task-item" data-idx="${{idx}}" data-domain="${{domain}}" data-status="${{statusKey}}" onclick="selectTask(${{idx}})">
                    <div class="task-header">
                        <span class="task-id">${{task.task_id}}</span>
                        <span class="task-status ${{statusKey}}">${{statusLabel}}</span>
                    </div>
                    <div class="task-info">
                        <span class="task-domain">${{domain}}</span>
                        <span> | ${{numSteps}} steps</span>
                    </div>
                </div>
            `;
        }});
        container.innerHTML = html;
    }}

    function setupFilters() {{
        document.getElementById('domain-filter').addEventListener('change', filterTasks);
        document.getElementById('status-filter').addEventListener('change', filterTasks);
        document.getElementById('search-input').addEventListener('input', filterTasks);
        document.getElementById('search-clear-btn').addEventListener('click', clearSearch);
    }}

    function advancedSearch(items, query, fields = ['task_id', 'instruction']) {{
        if (!query || query.trim() === '') {{
            return items.map((_, i) => i);
        }}

        // Tokenize query
        const queryTokens = query
            .toLowerCase()
            .replace(/[^a-z0-9\\s]/g, ' ')
            .replace(/\\s+/g, ' ')
            .trim()
            .split(' ')
            .filter(t => t.length > 0);

        if (queryTokens.length === 0) {{
            return items.map((_, i) => i);
        }}

        const results = [];

        items.forEach((task, idx) => {{
            // Build searchable text
            const searchParts = [];

            // Add task ID
            searchParts.push(task.task_id);

            // Add instruction
            if (task.definition && task.definition.instruction) {{
                searchParts.push(task.definition.instruction);
            }}

            // Add domain
            if (task.definition && task.definition.domain) {{
                searchParts.push(task.definition.domain);
            }}

            // Add action types from steps
            if (task.execution && task.execution.steps) {{
                task.execution.steps.forEach(step => {{
                    if (step.action && step.action.type) {{
                        searchParts.push(step.action.type);
                    }}
                }});
            }}

            const searchText = searchParts
                .join(' ')
                .toLowerCase()
                .replace(/[^a-z0-9\\s]/g, ' ')
                .replace(/\\s+/g, ' ');

            // All query tokens must match
            const matches = queryTokens.every(token => searchText.includes(token));
            if (matches) {{
                results.push(idx);
            }}
        }});

        return results;
    }}

    function filterTasks() {{
        const domainFilter = document.getElementById('domain-filter').value;
        const statusFilter = document.getElementById('status-filter').value;
        const searchQuery = document.getElementById('search-input').value;

        // Get search matches
        const searchMatches = advancedSearch(tasks, searchQuery);

        let visibleCount = 0;
        document.querySelectorAll('.task-item').forEach(item => {{
            const idx = parseInt(item.dataset.idx);
            const domain = item.dataset.domain;
            const status = item.dataset.status;

            const matchDomain = domainFilter === 'all' || domain === domainFilter;
            const matchStatus = statusFilter === 'all' || status === statusFilter;
            const matchSearch = !searchQuery || searchMatches.includes(idx);

            if (matchDomain && matchStatus && matchSearch) {{
                item.classList.remove('hidden');
                visibleCount++;
            }} else {{
                item.classList.add('hidden');
            }}
        }});

        document.getElementById('filter-count').textContent = `${{visibleCount}} tasks`;
    }}

    function clearSearch() {{
        document.getElementById('search-input').value = '';
        filterTasks();
    }}

    // Keyboard shortcuts for search
    document.addEventListener('keydown', (e) => {{
        // Ctrl+F / Cmd+F to focus search
        if ((e.ctrlKey || e.metaKey) && e.key === 'f' && !e.shiftKey) {{
            e.preventDefault();
            document.getElementById('search-input').focus();
        }}
        // Escape to clear search when focused
        if (e.key === 'Escape' && document.activeElement === document.getElementById('search-input')) {{
            clearSearch();
            document.getElementById('search-input').blur();
        }}
    }});

    function selectTask(idx) {{
        currentTaskIndex = idx;
        currentStepIndex = 0;

        // Update active state in list
        document.querySelectorAll('.task-item').forEach((item, i) => {{
            item.classList.toggle('active', parseInt(item.dataset.idx) === idx);
        }});

        // Show task detail
        document.getElementById('no-task-selected').style.display = 'none';
        document.getElementById('task-detail-content').style.display = 'block';

        renderTaskDetail();
    }}

    function renderTaskDetail() {{
        if (currentTaskIndex < 0) return;

        const task = tasks[currentTaskIndex];
        const def = task.definition || {{}};
        const exec = task.execution || {{}};
        const steps = exec.steps || [];
        const success = exec.success === true;
        const isInfra = (exec.error_type || '') === 'infrastructure';
        const isUnscored = isInfra || (exec.error_type || '') === 'evaluation';
        const statusColor = success ? 'var(--success)' : (isUnscored ? '#f59e0b' : 'var(--error)');
        const statusLabel = success ? 'PASSED' : (isInfra ? 'INFRA ERROR' : (isUnscored ? 'UNSCORED' : 'FAILED'));

        const container = document.getElementById('task-detail-content');
        container.innerHTML = `
            <div class="task-detail-header">
                <h2>${{task.task_id}} - <span style="color: ${{statusColor}}">${{statusLabel}}</span></h2>
                <div class="task-detail-meta">
                    Domain: <strong>${{def.domain || 'unknown'}}</strong> |
                    Steps: <strong>${{exec.num_steps || steps.length}}</strong> |
                    Time: <strong>${{(exec.total_time_seconds || 0).toFixed(1)}}s</strong>
                    ${{exec.error ? `<br>Error: <span style="color:var(--error)">${{exec.error}}</span>` : ''}}
                    ${{exec.error_type ? `<br>Error Type: <strong>${{exec.error_type}}</strong>` : ''}}
                </div>
                <div class="task-detail-instruction">
                    ${{def.instruction || 'No instruction available'}}
                </div>
            </div>
            <div class="step-viewer">
                <div class="screenshot-container" id="screenshot-container">
                    ${{steps.length > 0 ? '<div class="img-wrapper" id="img-wrapper"><img id="screenshot-img" src="" alt="Step screenshot"><div class="click-marker" id="click-marker"></div><canvas class="heatmap-canvas" id="heatmap-canvas" style="display:none;"></canvas></div>' : '<span class="screenshot-placeholder">No screenshots available</span>'}}
                </div>
                <div class="step-sidebar">
                    <div class="step-controls">
                        <button class="step-btn" onclick="prevStep()">Prev</button>
                        <button class="step-btn primary" id="play-btn" onclick="togglePlay()">Play</button>
                        <button class="step-btn" onclick="nextStep()">Next</button>
                        <span class="step-progress" id="step-progress">0 / ${{steps.length}}</span>
                        <div class="speed-control">
                            <label>Speed</label>
                            <select id="speed-select" onchange="changeSpeed(this.value)">
                                <option value="2000">0.5x</option>
                                <option value="1000" selected>1x</option>
                                <option value="500">2x</option>
                                <option value="250">4x</option>
                            </select>
                        </div>
                    </div>
                    <div class="progress-bar" onclick="seekStep(event)">
                        <div class="progress" id="step-progress-bar" style="width: 0%"></div>
                    </div>
                    <div id="action-timeline" class="action-timeline"></div>
                    <div style="display:flex;justify-content:space-between;align-items:center;">
                        <label class="heatmap-toggle"><input type="checkbox" id="heatmap-toggle" onchange="toggleHeatmap(this.checked)"> Click Heatmap</label>
                    </div>
                    <div class="step-list" id="step-list"></div>
                    <div class="action-detail" id="action-detail">
                        <h4>Action</h4>
                        <div class="action-content" id="action-content">-</div>
                    </div>
                    <div class="reasoning-box" id="reasoning-box" style="display:none;">
                        <h4>Reasoning</h4>
                        <div id="reasoning-content"></div>
                    </div>
                    <div class="thinking-panel" id="thinking-panel" style="display:none;">
                        <div class="thinking-header" onclick="toggleThinkingPanel()">
                            <h4>
                                <span class="expand-icon" id="thinking-expand-icon">▶</span>
                                Agent Thinking
                                <span id="thinking-badges" style="margin-left:8px;"></span>
                            </h4>
                        </div>
                        <div class="thinking-body" id="thinking-body" style="display:none;">
                            <div class="thinking-section" id="thinking-timing"></div>
                            <div class="thinking-section" id="thinking-tokens"></div>
                            <div class="thinking-section" id="thinking-parse"></div>
                            <div class="thinking-section" id="thinking-memory" style="display:none;">
                                <h5>Agent Memory</h5>
                                <pre class="thinking-pre" id="thinking-memory-content"></pre>
                            </div>
                            <div class="thinking-section" id="thinking-response" style="display:none;">
                                <h5>LLM Response</h5>
                                <pre class="thinking-pre" id="thinking-response-content"></pre>
                            </div>
                            <div class="thinking-section" id="thinking-prompt" style="display:none;">
                                <h5>Full Prompt Sent</h5>
                                <pre class="thinking-pre thinking-prompt-pre" id="thinking-prompt-content"></pre>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            <div class="log-panel">
                <div class="log-panel-header" onclick="toggleLogPanel()">
                    <h4>
                        <span class="expand-icon" id="log-expand-icon">▼</span>
                        Execution Logs
                        <span id="log-count" style="color: var(--text-muted); font-weight: normal;">(${{(exec.logs || []).length}} entries)</span>
                    </h4>
                </div>
                <div class="log-controls" id="log-controls">
                    <input type="text" class="log-search" id="log-search" placeholder="Search logs..." oninput="filterLogs()">
                    <button class="log-filter-btn active" data-level="all" onclick="setLogLevel('all')">All</button>
                    <button class="log-filter-btn" data-level="INFO" onclick="setLogLevel('INFO')">Info</button>
                    <button class="log-filter-btn" data-level="WARNING" onclick="setLogLevel('WARNING')">Warning</button>
                    <button class="log-filter-btn" data-level="ERROR" onclick="setLogLevel('ERROR')">Error</button>
                    <button class="log-filter-btn" data-level="SUCCESS" onclick="setLogLevel('SUCCESS')">Success</button>
                </div>
                <div class="log-container" id="log-container"></div>
            </div>
        `;

        renderStepList();
        renderActionTimeline();
        renderLogs();
        if (steps.length > 0) {{
            updateStep();
        }}
    }}

    function renderStepList() {{
        if (currentTaskIndex < 0) return;

        const task = tasks[currentTaskIndex];
        const steps = task.execution?.steps || [];
        const container = document.getElementById('step-list');

        let html = '';
        steps.forEach((step, idx) => {{
            const action = step.action || {{}};
            const actionType = action.type || 'unknown';
            html += `
                <div class="step-list-item ${{idx === currentStepIndex ? 'active' : ''}}" onclick="goToStep(${{idx}})">
                    <span class="step-num">#${{idx}}</span>
                    <span class="step-action">${{actionType.toUpperCase()}}</span>
                </div>
            `;
        }});
        container.innerHTML = html || '<div style="padding:12px;color:var(--text-muted);">No steps</div>';
    }}

    function updateStep() {{
        if (currentTaskIndex < 0) return;

        const task = tasks[currentTaskIndex];
        const steps = task.execution?.steps || [];
        const screenshots = task.screenshots || [];

        if (steps.length === 0) return;

        const step = steps[currentStepIndex] || {{}};
        const action = step.action || {{}};

        // Update screenshot
        const img = document.getElementById('screenshot-img');
        const marker = document.getElementById('click-marker');

        if (img) {{
            if (embedScreenshots && task.embedded_screenshots && task.embedded_screenshots[currentStepIndex]) {{
                img.src = task.embedded_screenshots[currentStepIndex];
            }} else if (screenshots[currentStepIndex]) {{
                img.src = screenshots[currentStepIndex];
            }} else if (step.screenshot_path) {{
                img.src = step.screenshot_path;
            }}
        }}

        // Position click marker using percentage coordinates inside img-wrapper.
        // The marker shows where the agent will click on THIS screenshot.
        if (marker) {{
            if (action.x != null && action.y != null) {{
                let normX = action.x;
                let normY = action.y;
                // Backward compat: check if stored action.x/y were normalized correctly.
                // New data (after coordinate fix): action.x * imgWidth ≈ raw pixel x.
                // Old data (before fix): normalized against wrong resolution, so they diverge.
                const raw = action.raw_action;
                if (raw && img && img.naturalWidth > 0) {{
                    const code = typeof raw === 'string' ? raw : (raw.code || '');
                    const m = code.match(/computer\\.(?:click|double_click|right_click)\\((\\d+),\\s*(\\d+)\\)/);
                    if (m) {{
                        const rawX = parseInt(m[1]);
                        const rawY = parseInt(m[2]);
                        const storedPixelX = action.x * img.naturalWidth;
                        const storedPixelY = action.y * img.naturalHeight;
                        const tolerance = 5;
                        if (Math.abs(storedPixelX - rawX) > tolerance || Math.abs(storedPixelY - rawY) > tolerance) {{
                            // Old data: stored coords are wrong, use raw pixels
                            normX = rawX / img.naturalWidth;
                            normY = rawY / img.naturalHeight;
                        }}
                        // Otherwise: new data, stored coords are correct, use as-is
                    }}
                }}
                marker.style.left = (normX * 100) + '%';
                marker.style.top = (normY * 100) + '%';
                marker.style.display = 'block';
            }} else {{
                marker.style.display = 'none';
            }}
        }}

        // Update progress
        document.getElementById('step-progress').textContent = `${{currentStepIndex + 1}} / ${{steps.length}}`;
        const progressPct = steps.length > 1 ? (currentStepIndex / (steps.length - 1)) * 100 : 0;
        document.getElementById('step-progress-bar').style.width = `${{progressPct}}%`;

        // Update action detail
        const actionContent = document.getElementById('action-content');
        let actionText = action.type ? action.type.toUpperCase() : 'unknown';
        if (action.x !== null && action.y !== null && action.x !== undefined && action.y !== undefined) {{
            actionText += ` (${{(action.x * 100).toFixed(1)}}%, ${{(action.y * 100).toFixed(1)}}%)`;
        }}
        if (action.text) {{
            actionText += ` "${{action.text}}"`;
        }}
        if (action.key) {{
            actionText += ` [${{action.key}}]`;
        }}
        actionContent.textContent = actionText;

        // Update reasoning
        const reasoningBox = document.getElementById('reasoning-box');
        const reasoningContent = document.getElementById('reasoning-content');
        if (step.reasoning) {{
            reasoningBox.style.display = 'block';
            reasoningContent.textContent = step.reasoning;
        }} else {{
            reasoningBox.style.display = 'none';
        }}

        // Update Agent Thinking panel
        updateThinkingPanel(step);

        // Update step list active state
        document.querySelectorAll('.step-list-item').forEach((item, idx) => {{
            item.classList.toggle('active', idx === currentStepIndex);
        }});
    }}

    function updateThinkingPanel(step) {{
        const panel = document.getElementById('thinking-panel');
        const logs = step.agent_logs;
        if (!logs) {{
            panel.style.display = 'none';
            return;
        }}
        panel.style.display = 'block';

        // Badges
        const badges = document.getElementById('thinking-badges');
        let badgeHtml = '';
        if (logs.parse_strategy) badgeHtml += `<span class="thinking-badge parse">${{logs.parse_strategy}}</span> `;
        if (logs.demo_included) badgeHtml += `<span class="thinking-badge demo">demo:${{logs.demo_length || '?'}}ch</span> `;
        if (logs.loop_detected) badgeHtml += `<span class="thinking-badge loop">LOOP</span> `;
        if (logs.token_usage) {{
            const tu = logs.token_usage;
            badgeHtml += `<span class="thinking-badge tokens">${{tu.input_tokens || '?'}}→${{tu.output_tokens || '?'}} tok</span> `;
        }}
        if (logs.agent_think_ms) {{
            badgeHtml += `<span class="thinking-badge time">${{(logs.agent_think_ms/1000).toFixed(1)}}s think</span> `;
        }}
        if (logs.env_execute_ms) {{
            badgeHtml += `<span class="thinking-badge time">${{(logs.env_execute_ms/1000).toFixed(1)}}s exec</span> `;
        }}
        badges.innerHTML = badgeHtml;

        // Timing section
        const timingEl = document.getElementById('thinking-timing');
        if (logs.agent_think_ms || logs.env_execute_ms) {{
            const think = logs.agent_think_ms ? (logs.agent_think_ms/1000).toFixed(2) + 's' : '-';
            const exec = logs.env_execute_ms ? (logs.env_execute_ms/1000).toFixed(2) + 's' : '-';
            timingEl.innerHTML = `<span style="font-size:0.75rem;color:var(--text-muted);">Think: ${{think}} | Execute: ${{exec}}</span>`;
            timingEl.style.display = 'block';
        }} else {{
            timingEl.style.display = 'none';
        }}

        // Token section
        const tokenEl = document.getElementById('thinking-tokens');
        if (logs.token_usage) {{
            const tu = logs.token_usage;
            tokenEl.innerHTML = `<span style="font-size:0.75rem;color:var(--text-muted);">Tokens: ${{tu.input_tokens || '?'}} in → ${{tu.output_tokens || '?'}} out</span>`;
            tokenEl.style.display = 'block';
        }} else {{
            tokenEl.style.display = 'none';
        }}

        // Parse strategy
        const parseEl = document.getElementById('thinking-parse');
        if (logs.parse_strategy) {{
            let parseHtml = `<span style="font-size:0.75rem;color:var(--text-muted);">Parse: ${{logs.parse_strategy}}</span>`;
            if (logs.loop_detected) parseHtml += ` <span style="color:#f85149;font-size:0.75rem;">⚠ Loop detected → ${{logs.alternative_action || 'no alt'}}</span>`;
            parseEl.innerHTML = parseHtml;
            parseEl.style.display = 'block';
        }} else {{
            parseEl.style.display = 'none';
        }}

        // Memory block
        const memEl = document.getElementById('thinking-memory');
        const memContent = document.getElementById('thinking-memory-content');
        if (logs.memory_block_text) {{
            memContent.textContent = logs.memory_block_text;
            memEl.style.display = 'block';
        }} else {{
            memEl.style.display = 'none';
        }}

        // LLM Response
        const respEl = document.getElementById('thinking-response');
        const respContent = document.getElementById('thinking-response-content');
        if (logs.plan_result) {{
            respContent.textContent = logs.plan_result;
            respEl.style.display = 'block';
        }} else {{
            respEl.style.display = 'none';
        }}

        // Full prompt
        const promptEl = document.getElementById('thinking-prompt');
        const promptContent = document.getElementById('thinking-prompt-content');
        if (logs.user_question) {{
            promptContent.textContent = logs.user_question;
            promptEl.style.display = 'block';
        }} else {{
            promptEl.style.display = 'none';
        }}
    }}

    function toggleThinkingPanel() {{
        const body = document.getElementById('thinking-body');
        const icon = document.getElementById('thinking-expand-icon');
        if (body.style.display === 'none') {{
            body.style.display = 'block';
            icon.textContent = '▼';
        }} else {{
            body.style.display = 'none';
            icon.textContent = '▶';
        }}
    }}

    function prevStep() {{
        if (currentStepIndex > 0) {{
            currentStepIndex--;
            updateStep();
        }}
    }}

    function nextStep() {{
        const task = tasks[currentTaskIndex];
        const steps = task?.execution?.steps || [];
        if (currentStepIndex < steps.length - 1) {{
            currentStepIndex++;
            updateStep();
        }} else if (isPlaying) {{
            stopPlay();
        }}
    }}

    function goToStep(idx) {{
        currentStepIndex = idx;
        updateStep();
    }}

    function seekStep(event) {{
        const task = tasks[currentTaskIndex];
        const steps = task?.execution?.steps || [];
        if (steps.length === 0) return;

        const bar = event.currentTarget;
        const rect = bar.getBoundingClientRect();
        const pct = (event.clientX - rect.left) / rect.width;
        currentStepIndex = Math.floor(pct * steps.length);
        currentStepIndex = Math.max(0, Math.min(currentStepIndex, steps.length - 1));
        updateStep();
    }}

    function togglePlay() {{
        if (isPlaying) {{
            stopPlay();
        }} else {{
            startPlay();
        }}
    }}

    function startPlay() {{
        isPlaying = true;
        document.getElementById('play-btn').textContent = 'Pause';
        document.getElementById('play-btn').classList.add('active');
        playInterval = setInterval(nextStep, playSpeed);
    }}

    function stopPlay() {{
        isPlaying = false;
        document.getElementById('play-btn').textContent = 'Play';
        document.getElementById('play-btn').classList.remove('active');
        if (playInterval) {{
            clearInterval(playInterval);
            playInterval = null;
        }}
    }}

    function changeSpeed(value) {{
        playSpeed = parseInt(value);
        if (isPlaying) {{
            stopPlay();
            startPlay();
        }}
    }}

    // --- Action Timeline ---
    const timelineColors = {{ click: '#58a6ff', type: '#3fb950', key: '#d29922', done: '#a371f7', scroll: '#f47067', unknown: '#8b949e' }};

    function renderActionTimeline() {{
        const tl = document.getElementById('action-timeline');
        if (!tl) return;
        const task = tasks[currentTaskIndex];
        const steps = task?.execution?.steps || [];
        if (steps.length === 0) {{ tl.innerHTML = ''; return; }}

        // Compute durations; fall back to equal widths
        const durations = steps.map(s => {{
            const al = s.agent_logs || {{}};
            return (al.agent_think_ms || 0) + (al.env_execute_ms || 0);
        }});
        const totalDur = durations.reduce((a, b) => a + b, 0);
        const useDuration = totalDur > 0;

        let html = '';
        steps.forEach((s, i) => {{
            const atype = (s.action?.type || 'unknown').toLowerCase();
            const color = timelineColors[atype] || timelineColors.unknown;
            const pct = useDuration && totalDur > 0
                ? Math.max(1, (durations[i] / totalDur) * 100)
                : (100 / steps.length);
            const label = `#${{i}} ${{atype.toUpperCase()}}${{useDuration ? ' (' + (durations[i]/1000).toFixed(1) + 's)' : ''}}`;
            html += `<div class="timeline-seg${{i === currentStepIndex ? ' active' : ''}}" style="width:${{pct}}%;background:${{color}};" onclick="goToStep(${{i}})"><span class="tl-tip">${{label}}</span></div>`;
        }});
        tl.innerHTML = html;
    }}

    function updateTimelineHighlight() {{
        document.querySelectorAll('.timeline-seg').forEach((seg, i) => {{
            seg.classList.toggle('active', i === currentStepIndex);
        }});
    }}

    // --- Click Heatmap ---
    let heatmapVisible = false;

    function renderHeatmap() {{
        const canvas = document.getElementById('heatmap-canvas');
        const img = document.getElementById('screenshot-img');
        const wrapper = document.getElementById('img-wrapper');
        if (!canvas || !img || !wrapper) return;

        const task = tasks[currentTaskIndex];
        const steps = task?.execution?.steps || [];

        // Size canvas to match the wrapper (which sizes to the rendered image)
        canvas.width = wrapper.clientWidth;
        canvas.height = wrapper.clientHeight;
        canvas.style.width = wrapper.clientWidth + 'px';
        canvas.style.height = wrapper.clientHeight + 'px';

        const ctx = canvas.getContext('2d');
        ctx.clearRect(0, 0, canvas.width, canvas.height);

        // Collect click positions — use raw pixel coords when available
        const natW = img.naturalWidth || canvas.width;
        const natH = img.naturalHeight || canvas.height;
        const clicks = [];
        steps.forEach(s => {{
            const a = s.action || {{}};
            if (a.x != null && a.y != null && (a.type === 'click' || a.type === 'left_click')) {{
                let nx = a.x, ny = a.y;
                const raw = a.raw_action;
                if (raw && natW > 0) {{
                    const code = typeof raw === 'string' ? raw : (raw.code || raw.waa_action || '');
                    const cm = code.match(/computer\\.(?:click|double_click|right_click)\\((\\d+),\\s*(\\d+)\\)/);
                    if (cm) {{
                        const rawX = parseInt(cm[1]), rawY = parseInt(cm[2]);
                        const tol = 5;
                        if (Math.abs(a.x * natW - rawX) > tol || Math.abs(a.y * natH - rawY) > tol) {{
                            nx = rawX / natW; ny = rawY / natH;
                        }}
                    }}
                }}
                clicks.push({{ x: nx * canvas.width, y: ny * canvas.height }});
            }}
        }});

        if (clicks.length === 0) return;

        // Count per cell (grid-based)
        const cellSize = 24;
        const grid = {{}};
        let maxCount = 0;
        clicks.forEach(c => {{
            const gx = Math.floor(c.x / cellSize);
            const gy = Math.floor(c.y / cellSize);
            const key = gx + ',' + gy;
            grid[key] = (grid[key] || 0) + 1;
            if (grid[key] > maxCount) maxCount = grid[key];
        }});

        // Draw heatmap circles
        Object.entries(grid).forEach(([key, count]) => {{
            const [gx, gy] = key.split(',').map(Number);
            const cx = gx * cellSize + cellSize / 2;
            const cy = gy * cellSize + cellSize / 2;
            const intensity = count / maxCount;
            const radius = 12 + intensity * 16;

            const r = Math.round(248 * intensity + 88 * (1 - intensity));
            const g = Math.round(81 * intensity + 166 * (1 - intensity));
            const b = Math.round(73 * intensity + 255 * (1 - intensity));
            const alpha = 0.25 + intensity * 0.45;

            ctx.beginPath();
            ctx.arc(cx, cy, radius, 0, Math.PI * 2);
            ctx.fillStyle = `rgba(${{r}},${{g}},${{b}},${{alpha}})`;
            ctx.fill();

            // Draw count
            if (count > 1) {{
                ctx.fillStyle = '#fff';
                ctx.font = 'bold 10px sans-serif';
                ctx.textAlign = 'center';
                ctx.textBaseline = 'middle';
                ctx.fillText(count.toString(), cx, cy);
            }}
        }});
    }}

    function toggleHeatmap(checked) {{
        heatmapVisible = checked;
        const canvas = document.getElementById('heatmap-canvas');
        if (!canvas) return;
        canvas.style.display = heatmapVisible ? 'block' : 'none';
        if (heatmapVisible) renderHeatmap();
    }}

    // Update timeline and heatmap when step changes
    const _origUpdateStep = updateStep;
    updateStep = function() {{
        _origUpdateStep();
        updateTimelineHighlight();
        if (heatmapVisible) renderHeatmap();
    }};

    // Unified keyboard shortcuts
    {keyboard_shortcuts_js}

    // Initialize keyboard shortcuts with viewer-specific actions
    KeyboardShortcuts.init({{
        togglePlay: togglePlay,
        prevStep: prevStep,
        nextStep: nextStep,
        firstStep: () => goToStep(0),
        lastStep: () => {{
            const task = tasks[currentTaskIndex];
            const steps = task?.execution?.steps || [];
            goToStep(steps.length - 1);
        }},
        setSpeed: (speed) => changeSpeed(speed),
        closeModals: () => {{
            // Close any open modals (log panel, search, etc.)
            const logPanel = document.getElementById('log-container');
            const logControls = document.getElementById('log-controls');
            const logExpandIcon = document.getElementById('log-expand-icon');
            if (logPanel && !logPanel.classList.contains('collapsed')) {{
                toggleLogPanel();
            }}
        }},
        showShortcutsOverlay: () => KeyboardShortcuts.showShortcutsOverlay(),
        focusSearch: () => {{
            const searchInput = document.getElementById('log-search');
            if (searchInput) {{
                searchInput.focus();
            }}
        }}
    }});

    // Log panel state
    let currentLogLevel = 'all';
    let logPanelCollapsed = false;

    function renderLogs() {{
        if (currentTaskIndex < 0) return;

        const task = tasks[currentTaskIndex];
        const logs = task.execution?.logs || [];
        const container = document.getElementById('log-container');

        if (logs.length === 0) {{
            container.innerHTML = '<div class="log-empty">No logs available for this task</div>';
            return;
        }}

        let html = '';
        logs.forEach((log, idx) => {{
            const timestamp = log.timestamp.toFixed(2);
            html += `
                <div class="log-entry" data-level="${{log.level}}" data-message="${{log.message.toLowerCase()}}">
                    <div class="log-timestamp">${{timestamp}}s</div>
                    <div class="log-level ${{log.level}}">${{log.level}}</div>
                    <div class="log-message">${{escapeHtml(log.message)}}</div>
                </div>
            `;
        }});
        container.innerHTML = html;

        // Auto-scroll to bottom
        container.scrollTop = container.scrollHeight;
    }}

    function toggleLogPanel() {{
        logPanelCollapsed = !logPanelCollapsed;
        const container = document.getElementById('log-container');
        const controls = document.getElementById('log-controls');
        const icon = document.getElementById('log-expand-icon');

        if (logPanelCollapsed) {{
            container.classList.add('collapsed');
            controls.classList.add('collapsed');
            icon.classList.add('collapsed');
        }} else {{
            container.classList.remove('collapsed');
            controls.classList.remove('collapsed');
            icon.classList.remove('collapsed');
        }}
    }}

    function setLogLevel(level) {{
        currentLogLevel = level;

        // Update button states
        document.querySelectorAll('.log-filter-btn').forEach(btn => {{
            if (btn.dataset.level === level) {{
                btn.classList.add('active');
            }} else {{
                btn.classList.remove('active');
            }}
        }});

        filterLogs();
    }}

    function filterLogs() {{
        const searchTerm = document.getElementById('log-search').value.toLowerCase();
        const entries = document.querySelectorAll('.log-entry');

        entries.forEach(entry => {{
            const level = entry.dataset.level;
            const message = entry.dataset.message;

            const levelMatch = currentLogLevel === 'all' || level === currentLogLevel;
            const searchMatch = !searchTerm || message.includes(searchTerm);

            if (levelMatch && searchMatch) {{
                entry.classList.remove('hidden');
            }} else {{
                entry.classList.add('hidden');
            }}
        }});
    }}

    function escapeHtml(text) {{
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }}

    // Live monitoring support
    let liveMonitoringEnabled = false;
    let liveMonitoringInterval = null;

    function enableLiveMonitoring() {{
        liveMonitoringEnabled = true;
        startLivePolling();
    }}

    function startLivePolling() {{
        if (!liveMonitoringEnabled) return;

        // Poll /api/benchmark-live every 2 seconds
        liveMonitoringInterval = setInterval(async () => {{
            try {{
                const response = await fetch('/api/benchmark-live');
                if (response.ok) {{
                    const liveData = await response.json();
                    updateLiveData(liveData);
                }}
            }} catch (error) {{
                console.error('Error fetching live data:', error);
                // Don't stop polling on error - server might be starting
            }}
        }}, 2000);
    }}

    function stopLivePolling() {{
        liveMonitoringEnabled = false;
        if (liveMonitoringInterval) {{
            clearInterval(liveMonitoringInterval);
            liveMonitoringInterval = null;
        }}
    }}

    function updateLiveData(liveData) {{
        if (!liveData || liveData.status === 'no_data') return;

        if (liveData.status === 'complete') {{
            stopLivePolling();
            console.log('Live monitoring complete');
            return;
        }}

        if (liveData.status === 'running' && liveData.current_task) {{
            // Show live progress in UI
            const currentTask = liveData.current_task;

            // Update summary stats
            if (liveData.total_tasks) {{
                document.querySelector('.stat-card:nth-child(1) .stat-value').textContent = liveData.total_tasks;
            }}

            // Show live indicator
            let liveIndicator = document.getElementById('live-indicator');
            if (!liveIndicator) {{
                liveIndicator = document.createElement('div');
                liveIndicator.id = 'live-indicator';
                liveIndicator.style.cssText = `
                    position: fixed;
                    top: 20px;
                    right: 20px;
                    padding: 8px 16px;
                    background: var(--accent);
                    color: var(--bg-primary);
                    border-radius: 20px;
                    font-size: 0.8rem;
                    font-weight: 600;
                    z-index: 1000;
                    animation: pulse 2s infinite;
                `;
                liveIndicator.innerHTML = `
                    <style>
                    @keyframes pulse {{
                        0%, 100% {{ opacity: 1; }}
                        50% {{ opacity: 0.7; }}
                    }}
                    </style>
                    LIVE
                `;
                document.body.appendChild(liveIndicator);
            }}

            console.log('Live update:', currentTask.task_id, 'steps:', currentTask.steps.length);
        }}
    }}

    // Try to enable live monitoring if /api/benchmark-live is available
    window.addEventListener('load', async () => {{
        try {{
            const response = await fetch('/api/benchmark-live');
            if (response.ok) {{
                const data = await response.json();
                if (data.status === 'running' || data.status === 'idle') {{
                    console.log('Live monitoring available - enabling auto-refresh');
                    enableLiveMonitoring();
                }}
            }}
        }} catch (error) {{
            // API not available - viewer is in static mode
            console.log('Live monitoring not available (static viewer mode)');
        }}
    }});

    // Initialize on load
    document.addEventListener('DOMContentLoaded', init);
    </script>
</body>
</html>
'''

    return html

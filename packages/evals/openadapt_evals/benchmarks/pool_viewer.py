"""WAA Pool Results Viewer - HTML viewer for parallel benchmark runs.

Parses log files from pool_run_* directories to extract task results and
generates a standalone HTML viewer with summary stats, per-worker breakdown,
and domain analysis.

Usage:
    from openadapt_evals.benchmarks.pool_viewer import generate_pool_results_viewer

    generate_pool_results_viewer(
        pool_dir=Path("benchmark_results/pool_run_20260204"),
        output_path=Path("benchmark_results/pool_run_20260204/results.html"),
    )
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from openadapt_evals.adapters.base import (
    UNSCORED_BENCHMARK_ERROR_TYPES,
    normalize_benchmark_result_artifact,
)


def _normalize_pool_task_artifact(task: object) -> tuple[dict[str, Any], bool]:
    """Return a display row and whether it is a measured outcome."""
    raw = dict(task) if isinstance(task, dict) else {}
    if "score" not in raw and "result" in raw:
        raw["score"] = raw["result"]
    task_id = str(raw.get("task_id") or "unknown")
    result = normalize_benchmark_result_artifact(
        raw,
        expected_task_id=task_id,
        context=f"pool artifact for {task_id}",
    )
    raw.update(
        {
            "task_id": result.task_id,
            "result": result.score,
            "score": result.score,
            "success": result.success,
            "error": result.error,
            "reason": result.reason,
            "error_type": result.error_type,
        }
    )
    return raw, result.error_type not in UNSCORED_BENCHMARK_ERROR_TYPES

def parse_pool_logs(pool_dir: Path) -> dict[str, Any]:
    """Parse WAA pool log files to extract task results.

    Args:
        pool_dir: Directory containing waa-pool-*.log files

    Returns:
        Dictionary with:
            - tasks: List of task results
            - workers: Per-worker stats
            - metadata: Run metadata (timestamps, model, etc.)
    """
    log_files = sorted(pool_dir.glob("waa-pool-*.log"))
    if not log_files:
        return {"tasks": [], "workers": {}, "metadata": {}}

    tasks = []
    workers = {}
    metadata = {
        "run_name": pool_dir.name,
        "log_count": len(log_files),
        "first_timestamp": None,
        "last_timestamp": None,
        "model": None,
        "num_workers": None,
    }

    # Regex patterns
    timestamp_re = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
    domain_re = re.compile(r"\[Domain\]: (\S+)")
    example_re = re.compile(r"\[Example ID\]: (\S+)")
    instruction_re = re.compile(r"\[Instruction\]: (.+)")
    finished_re = re.compile(r"Finished (\S+)/(\S+)")
    result_re = re.compile(r"Result: ([0-9.]+)")
    model_re = re.compile(r"model='([^']+)'")
    num_workers_re = re.compile(r"num_workers=(\d+)")
    step_re = re.compile(r"Step (\d+):")

    for log_file in log_files:
        worker_id = log_file.stem.replace("waa-pool-", "")
        workers[worker_id] = {
            "tasks": 0,
            "outcomes": 0,
            "successes": 0,
            "failures": 0,
            "unscored": 0,
        }

        current_task = None
        last_result = None

        with open(log_file, "r", errors="ignore") as f:
            for line in f:
                # Strip ANSI codes
                clean = re.sub(r"\x1b\[[0-9;]*m", "", line)

                # Extract timestamp
                ts_match = timestamp_re.search(clean)
                if ts_match:
                    ts_str = ts_match.group(1)
                    if metadata["first_timestamp"] is None:
                        metadata["first_timestamp"] = ts_str
                    metadata["last_timestamp"] = ts_str

                # Extract model name
                if metadata["model"] is None:
                    model_match = model_re.search(clean)
                    if model_match:
                        metadata["model"] = model_match.group(1)

                # Extract num workers
                if metadata["num_workers"] is None:
                    nw_match = num_workers_re.search(clean)
                    if nw_match:
                        metadata["num_workers"] = int(nw_match.group(1))

                # Domain (comes before Example ID)
                domain_match = domain_re.search(clean)
                if domain_match:
                    if current_task is None:
                        current_task = {"worker_id": worker_id, "steps": 0}
                    current_task["domain"] = domain_match.group(1)

                # Example ID
                example_match = example_re.search(clean)
                if example_match:
                    if current_task is None:
                        current_task = {"worker_id": worker_id, "steps": 0}
                    current_task["task_id"] = example_match.group(1)

                # Instruction
                instr_match = instruction_re.search(clean)
                if instr_match and current_task:
                    current_task["instruction"] = instr_match.group(1)

                # Step count
                step_match = step_re.search(clean)
                if step_match and current_task:
                    step_num = int(step_match.group(1))
                    if step_num > current_task.get("steps", 0):
                        current_task["steps"] = step_num

                # Result line
                result_match = result_re.search(clean)
                if result_match:
                    last_result = float(result_match.group(1))

                # Finished line - finalize task
                finished_match = finished_re.search(clean)
                if finished_match:
                    domain = finished_match.group(1)
                    task_id = finished_match.group(2)

                    if current_task is None:
                        current_task = {"worker_id": worker_id, "steps": 0}

                    current_task["domain"] = domain
                    current_task["task_id"] = task_id
                    current_task.update(
                        {
                            "result": last_result,
                            "score": last_result,
                            "success": last_result is not None and last_result >= 1.0,
                            "error_type": (
                                None if last_result is not None else "evaluation"
                            ),
                            "reason": (
                                None
                                if last_result is not None
                                else "pool log did not contain a result"
                            ),
                        }
                    )
                    current_task["timestamp"] = metadata["last_timestamp"]
                    current_task, is_scored = _normalize_pool_task_artifact(
                        current_task
                    )

                    # Update worker stats
                    workers[worker_id]["tasks"] += 1
                    if not is_scored:
                        workers[worker_id]["unscored"] += 1
                    elif current_task["success"]:
                        workers[worker_id]["outcomes"] += 1
                        workers[worker_id]["successes"] += 1
                    else:
                        workers[worker_id]["outcomes"] += 1
                        workers[worker_id]["failures"] += 1

                    tasks.append(current_task)
                    current_task = None
                    last_result = None

    return {
        "tasks": tasks,
        "workers": workers,
        "metadata": metadata,
    }


def get_domain_stats(tasks: list[dict]) -> dict[str, dict[str, int]]:
    """Calculate per-domain statistics."""
    domain_stats = {}

    for task in tasks:
        domain = task.get("domain", "unknown")
        if domain not in domain_stats:
            domain_stats[domain] = {
                "total": 0,
                "outcomes": 0,
                "success": 0,
                "fail": 0,
                "unscored": 0,
            }

        domain_stats[domain]["total"] += 1
        result, is_scored = _normalize_pool_task_artifact(task)
        if not is_scored:
            domain_stats[domain]["unscored"] += 1
        elif result["success"]:
            domain_stats[domain]["outcomes"] += 1
            domain_stats[domain]["success"] += 1
        else:
            domain_stats[domain]["outcomes"] += 1
            domain_stats[domain]["fail"] += 1

    return domain_stats


def generate_pool_results_viewer(
    pool_dir: Path,
    output_path: Path | None = None,
) -> Path:
    """Generate HTML viewer for WAA pool benchmark results.

    Args:
        pool_dir: Directory containing waa-pool-*.log files
        output_path: Output HTML path. Defaults to pool_dir/results.html

    Returns:
        Path to generated HTML file.
    """
    pool_dir = Path(pool_dir)
    if output_path is None:
        output_path = pool_dir / "results.html"

    # Parse logs
    data = parse_pool_logs(pool_dir)
    tasks = [_normalize_pool_task_artifact(task)[0] for task in data["tasks"]]
    workers = data["workers"]
    metadata = data["metadata"]

    # Calculate stats
    num_tasks = len(tasks)
    outcome_tasks = [
        task
        for task in tasks
        if task.get("error_type") not in UNSCORED_BENCHMARK_ERROR_TYPES
    ]
    num_outcomes = len(outcome_tasks)
    num_unscored = num_tasks - num_outcomes
    num_success = sum(1 for task in outcome_tasks if task["success"] is True)
    success_rate = (
        num_success / num_outcomes * 100 if num_outcomes > 0 else 0
    )

    # Domain stats
    domain_stats = get_domain_stats(tasks)

    # Calculate elapsed time
    elapsed_str = "N/A"
    if metadata.get("first_timestamp") and metadata.get("last_timestamp"):
        try:
            fmt = "%Y-%m-%d %H:%M:%S"
            start = datetime.strptime(metadata["first_timestamp"], fmt)
            end = datetime.strptime(metadata["last_timestamp"], fmt)
            elapsed = end - start
            hours, remainder = divmod(int(elapsed.total_seconds()), 3600)
            minutes, seconds = divmod(remainder, 60)
            if hours > 0:
                elapsed_str = f"{hours}h {minutes}m {seconds}s"
            elif minutes > 0:
                elapsed_str = f"{minutes}m {seconds}s"
            else:
                elapsed_str = f"{seconds}s"
        except Exception:
            pass

    # Avg time per task
    avg_time_str = "N/A"
    if num_tasks > 0 and metadata.get("first_timestamp") and metadata.get("last_timestamp"):
        try:
            fmt = "%Y-%m-%d %H:%M:%S"
            start = datetime.strptime(metadata["first_timestamp"], fmt)
            end = datetime.strptime(metadata["last_timestamp"], fmt)
            elapsed = end - start
            avg_seconds = elapsed.total_seconds() / num_tasks
            if avg_seconds >= 60:
                avg_time_str = f"{avg_seconds / 60:.1f}m"
            else:
                avg_time_str = f"{avg_seconds:.0f}s"
        except Exception:
            pass

    # Generate HTML
    html = _generate_pool_viewer_html(
        tasks=tasks,
        workers=workers,
        metadata=metadata,
        domain_stats=domain_stats,
        num_tasks=num_tasks,
        num_outcomes=num_outcomes,
        num_unscored=num_unscored,
        num_success=num_success,
        success_rate=success_rate,
        elapsed_str=elapsed_str,
        avg_time_str=avg_time_str,
    )

    # Write output
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html)

    return output_path


def _generate_pool_viewer_html(
    tasks: list[dict],
    workers: dict,
    metadata: dict,
    domain_stats: dict,
    num_tasks: int,
    num_outcomes: int,
    num_unscored: int,
    num_success: int,
    success_rate: float,
    elapsed_str: str,
    avg_time_str: str,
) -> str:
    """Generate HTML content for pool results viewer."""

    # Worker rows HTML
    worker_rows = ""
    for worker_id, stats in sorted(workers.items()):
        rate = (
            stats["successes"] / stats["outcomes"] * 100
            if stats["outcomes"] > 0
            else 0
        )
        worker_rows += f"""
            <tr>
                <td>Worker {worker_id}</td>
                <td>{stats["tasks"]}</td>
                <td class="success">{stats["successes"]}</td>
                <td class="error">{stats["failures"]}</td>
                <td>{stats["unscored"]}</td>
                <td>{rate:.1f}%</td>
            </tr>
        """

    # Domain breakdown HTML
    domain_tags = ""
    for domain in sorted(domain_stats.keys()):
        stats = domain_stats[domain]
        rate = (
            stats["success"] / stats["outcomes"] * 100
            if stats["outcomes"] > 0
            else 0
        )
        domain_tags += f"""
            <div class="domain-tag">
                <span class="domain-name">{domain}</span>
                <span class="domain-stats">{stats["success"]}/{stats["outcomes"]} ({rate:.0f}%) · {stats["total"]} attempts</span>
            </div>
        """

    # Task rows HTML
    task_rows = ""
    for i, task in enumerate(tasks):
        is_unscored = task.get("error_type") in UNSCORED_BENCHMARK_ERROR_TYPES
        status_class = (
            "success"
            if task["success"] is True
            else ("unscored" if is_unscored else "fail")
        )
        status_text = (
            "PASS"
            if task["success"] is True
            else ("UNSCORED" if is_unscored else "FAIL")
        )
        result = task.get("result", 0)
        task_rows += f"""
            <tr class="task-row" data-domain="{task.get("domain", "unknown")}" data-status="{status_class}">
                <td class="task-id">{task.get("task_id", "N/A")}</td>
                <td><span class="domain-badge">{task.get("domain", "unknown")}</span></td>
                <td><span class="status-badge {status_class}">{status_text}</span></td>
                <td>{result:.2f}</td>
                <td>{task.get("steps", 0)}</td>
                <td>Worker {task.get("worker_id", "?")}</td>
            </tr>
        """

    # Domain filter options
    domain_options = '<option value="all">All Domains</option>'
    for domain in sorted(domain_stats.keys()):
        domain_options += f'<option value="{domain}">{domain}</option>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>WAA Pool Results - {metadata.get("run_name", "Unknown")}</title>
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
            max-width: 1400px;
            margin: 0 auto;
            padding: 24px;
        }}
        h1 {{
            font-size: 1.5rem;
            font-weight: 600;
            margin-bottom: 8px;
        }}
        .meta-info {{
            font-size: 0.8rem;
            color: var(--text-secondary);
            margin-bottom: 24px;
            font-family: "SF Mono", Monaco, monospace;
        }}

        /* Summary Panel */
        .summary-panel {{
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 24px;
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
        .domain-tag .domain-name {{ color: var(--text-primary); }}
        .domain-tag .domain-stats {{
            font-family: "SF Mono", Monaco, monospace;
            color: var(--text-secondary);
        }}

        /* Section headers */
        .section-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 16px;
        }}
        .section-header h2 {{
            font-size: 1rem;
            font-weight: 600;
        }}

        /* Tables */
        table {{
            width: 100%;
            border-collapse: collapse;
        }}
        th, td {{
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid var(--border-color);
        }}
        th {{
            font-size: 0.7rem;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            font-weight: 500;
        }}
        td {{
            font-size: 0.85rem;
        }}
        td.success {{ color: var(--success); }}
        td.error {{ color: var(--error); }}
        tr:hover {{ background: var(--bg-tertiary); }}

        /* Worker table */
        .worker-panel {{
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 24px;
        }}

        /* Task list */
        .task-panel {{
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 20px;
        }}
        .task-id {{
            font-family: "SF Mono", Monaco, monospace;
            font-size: 0.8rem;
        }}
        .status-badge {{
            font-size: 0.7rem;
            font-weight: 600;
            padding: 2px 8px;
            border-radius: 4px;
        }}
        .status-badge.success {{
            background: rgba(52, 211, 153, 0.2);
            color: var(--success);
        }}
        .status-badge.fail {{
            background: rgba(255, 95, 95, 0.2);
            color: var(--error);
        }}
        .status-badge.unscored {{
            background: rgba(245, 158, 11, 0.2);
            color: #f59e0b;
        }}
        .domain-badge {{
            font-size: 0.75rem;
            color: var(--accent);
        }}

        /* Filters */
        .filter-bar {{
            display: flex;
            gap: 16px;
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

        /* Hidden rows */
        .task-row.hidden {{ display: none; }}

        /* Max height for task list */
        .task-scroll {{
            max-height: 600px;
            overflow-y: auto;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>WAA Pool Results</h1>
        <div class="meta-info">
            Run: {metadata.get("run_name", "Unknown")} |
            Model: {metadata.get("model", "N/A")} |
            Workers: {metadata.get("num_workers", len(workers))} |
            Time: {elapsed_str}
        </div>

        <!-- Summary Panel -->
        <div class="summary-panel">
            <div class="section-header">
                <h2>Summary</h2>
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
                    <div class="stat-value error">{num_outcomes - num_success}</div>
                    <div class="stat-label">Failed</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value error">{num_unscored}</div>
                    <div class="stat-label">Unscored Errors</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value {"success" if success_rate >= 50 else "error"}">{success_rate:.1f}%</div>
                    <div class="stat-label">Success Rate</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{avg_time_str}</div>
                    <div class="stat-label">Avg Time/Task</div>
                </div>
            </div>
            <div class="domain-breakdown">
                {domain_tags}
            </div>
        </div>

        <!-- Worker Panel -->
        <div class="worker-panel">
            <div class="section-header">
                <h2>Per-Worker Breakdown</h2>
            </div>
            <table>
                <thead>
                    <tr>
                        <th>Worker</th>
                        <th>Tasks</th>
                        <th>Passed</th>
                        <th>Failed</th>
                        <th>Unscored</th>
                        <th>Success Rate</th>
                    </tr>
                </thead>
                <tbody>
                    {worker_rows}
                </tbody>
            </table>
        </div>

        <!-- Task List Panel -->
        <div class="task-panel">
            <div class="section-header">
                <h2>Task Results</h2>
            </div>
            <div class="filter-bar">
                <div class="filter-group">
                    <span class="filter-label">Domain:</span>
                    <select class="filter-select" id="domain-filter" onchange="filterTasks()">
                        {domain_options}
                    </select>
                </div>
                <div class="filter-group">
                    <span class="filter-label">Status:</span>
                    <select class="filter-select" id="status-filter" onchange="filterTasks()">
                        <option value="all">All</option>
                        <option value="success">Passed</option>
                        <option value="fail">Failed</option>
                        <option value="unscored">Unscored</option>
                    </select>
                </div>
                <span class="filter-count" id="filter-count">{num_tasks} tasks</span>
            </div>
            <div class="task-scroll">
                <table>
                    <thead>
                        <tr>
                            <th>Task ID</th>
                            <th>Domain</th>
                            <th>Status</th>
                            <th>Result</th>
                            <th>Steps</th>
                            <th>Worker</th>
                        </tr>
                    </thead>
                    <tbody id="task-body">
                        {task_rows}
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <script>
        function filterTasks() {{
            const domainFilter = document.getElementById('domain-filter').value;
            const statusFilter = document.getElementById('status-filter').value;

            let visibleCount = 0;
            document.querySelectorAll('.task-row').forEach(row => {{
                const domain = row.dataset.domain;
                const status = row.dataset.status;

                const matchDomain = domainFilter === 'all' || domain === domainFilter;
                const matchStatus = statusFilter === 'all' || status === statusFilter;

                if (matchDomain && matchStatus) {{
                    row.classList.remove('hidden');
                    visibleCount++;
                }} else {{
                    row.classList.add('hidden');
                }}
            }});

            document.getElementById('filter-count').textContent = `${{visibleCount}} tasks`;
        }}
    </script>
</body>
</html>
"""

    return html

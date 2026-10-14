#!/usr/bin/env python3
"""Full WAA evaluation runner with resume support and pool integration.

Runs PlannerGrounderAgent (or other agents) against all WAA tasks with:
- Incremental JSONL results (safe to interrupt and resume)
- Per-task error isolation (one failure never crashes the whole run)
- SSH tunnel health checks with exponential backoff retry
- Screenshot saving per task
- Progress display with ETA
- Summary report at the end
- Parallel execution across a VM pool (--parallel N)
- Dry-run mode

Usage:
    # Single VM, all WAA tasks
    python scripts/run_full_eval.py \
        --server-url http://localhost:5001 \
        --grounder-model gpt-4.1-mini

    # Specific tasks
    python scripts/run_full_eval.py \
        --server-url http://localhost:5001 \
        --grounder-endpoint http://gpu-host:8000/v1 \
        --task-ids 04d9aeaf-7bed-4024-bedb-e10e6f00eb7f-WOS,0bf05a7d-b28b-44d2-955a-50b41e24012a-WOS

    # Resume a previous run
    python scripts/run_full_eval.py \
        --server-url http://localhost:5001 \
        --grounder-model gpt-4.1-mini \
        --resume --output results/eval_20260320_120000.jsonl

    # Dry run (list tasks without executing)
    python scripts/run_full_eval.py --dry-run --server-url http://localhost:5001

    # Parallel across pool VMs
    python scripts/run_full_eval.py \
        --grounder-model gpt-4.1-mini \
        --parallel 3

Prerequisites:
    - WAA VM running with SSH tunnel (port 5001 -> VM port 5000)
    - For HTTP grounder: UI-Venus serving via `bash scripts/serve_grounder.sh`
    - ANTHROPIC_API_KEY set (for Claude planner)
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run_full_eval")

# Graceful shutdown flag
_shutdown_requested = False


def _signal_handler(signum, frame):
    """Handle SIGINT/SIGTERM gracefully."""
    global _shutdown_requested
    if _shutdown_requested:
        logger.warning("Second interrupt received, forcing exit")
        sys.exit(1)
    _shutdown_requested = True
    logger.warning("Shutdown requested, finishing current task...")


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# ---------------------------------------------------------------------------
# WAA task discovery
# ---------------------------------------------------------------------------

# Default WAA task IDs: these are loaded from the WAA server's test_all.json
# via the /tasks endpoint, or fall back to the hardcoded list if server is
# unreachable. The hardcoded list is the 154-task benchmark set.
_DEFAULT_TASK_FILE = "test_all.json"


def discover_tasks_from_server(server_url: str) -> list[str]:
    """Fetch available task IDs from the WAA server.

    Tries the /tasks endpoint first, then falls back to reading
    test_all.json via /execute.

    Returns:
        List of task ID strings, or empty list on failure.
    """
    import requests

    # Try /tasks endpoint
    try:
        resp = requests.get(f"{server_url}/tasks", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                logger.info("Discovered %d tasks from /tasks endpoint", len(data))
                return data
            elif isinstance(data, dict):
                # WAA format: {"domain": ["task_id", ...], ...}
                task_ids = []
                for domain_tasks in data.values():
                    if isinstance(domain_tasks, list):
                        task_ids.extend(domain_tasks)
                if task_ids:
                    logger.info(
                        "Discovered %d tasks from /tasks endpoint", len(task_ids)
                    )
                    return task_ids
    except Exception as e:
        logger.debug("Could not fetch /tasks: %s", e)

    # Try reading test_all.json from container via /execute
    try:
        resp = requests.post(
            f"{server_url}/execute",
            json={
                "command": (
                    'python -c "'
                    "import json; "
                    "d=json.load(open('/client/evaluation_examples_windows/test_all.json')); "
                    "ids=[t for domain in d for t in d[domain]]; "
                    'print(json.dumps(ids))"'
                )
            },
            timeout=30,
        )
        if resp.status_code == 200:
            output = resp.json().get("output", "").strip()
            if output:
                task_ids = json.loads(output)
                logger.info(
                    "Discovered %d tasks from test_all.json via /execute",
                    len(task_ids),
                )
                return task_ids
    except Exception as e:
        logger.debug("Could not read test_all.json via /execute: %s", e)

    return []


# ---------------------------------------------------------------------------
# JSONL result file helpers
# ---------------------------------------------------------------------------


def _results_path(output: str | None) -> Path:
    """Return the output JSONL path, creating parent dirs."""
    if output:
        p = Path(output)
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        p = Path(f"benchmark_results/full_eval_{ts}.jsonl")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_completed_task_ids(path: Path) -> set[str]:
    """Load task IDs already completed from a JSONL file."""
    completed = set()
    if not path.exists():
        return completed
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                tid = record.get("task_id")
                if tid:
                    completed.add(tid)
            except json.JSONDecodeError:
                continue
    return completed


def _append_result(path: Path, record: dict) -> None:
    """Append a single result record to the JSONL file."""
    with open(path, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


# ---------------------------------------------------------------------------
# Server health / SSH tunnel helpers
# ---------------------------------------------------------------------------


def check_server_health(server_url: str, timeout: float = 10.0) -> bool:
    """Check if WAA server is reachable."""
    import requests

    try:
        resp = requests.get(f"{server_url}/probe", timeout=timeout)
        return resp.status_code == 200
    except Exception:
        return False


def wait_for_server(
    server_url: str,
    max_retries: int = 5,
    base_delay: float = 5.0,
    max_delay: float = 60.0,
) -> bool:
    """Wait for WAA server with exponential backoff.

    Returns True if server becomes reachable, False if all retries exhausted.
    """
    for attempt in range(max_retries):
        if check_server_health(server_url):
            if attempt > 0:
                logger.info("Server is back (attempt %d/%d)", attempt + 1, max_retries)
            return True
        delay = min(base_delay * (2**attempt), max_delay)
        logger.warning(
            "Server unreachable (attempt %d/%d), retrying in %.0fs...",
            attempt + 1,
            max_retries,
            delay,
        )
        time.sleep(delay)
    return False


# ---------------------------------------------------------------------------
# Agent creation helpers
# ---------------------------------------------------------------------------


def create_planner_grounder_agent(args: argparse.Namespace):
    """Create a PlannerGrounderAgent from CLI args."""
    from openadapt_evals.agents.planner_grounder_agent import PlannerGrounderAgent

    if args.grounder_endpoint:
        agent = PlannerGrounderAgent(
            planner=args.planner_model,
            grounder="http",
            planner_provider=args.planner_provider,
            grounder_provider="http",
            grounder_endpoint=args.grounder_endpoint,
        )
    elif args.grounder_model:
        agent = PlannerGrounderAgent(
            planner=args.planner_model,
            grounder=args.grounder_model,
            planner_provider=args.planner_provider,
            grounder_provider=args.grounder_provider,
        )
    else:
        raise ValueError(
            "Specify either --grounder-endpoint (HTTP/vLLM) or --grounder-model (API)"
        )

    return agent


# ---------------------------------------------------------------------------
# Single-task execution
# ---------------------------------------------------------------------------


def run_single_task(
    task_id: str,
    agent,
    server_url: str,
    max_steps: int,
    save_screenshots: bool,
    screenshots_dir: Path | None,
    task_config=None,
) -> dict:
    """Run a single task and return a result dict.

    Never raises -- all errors are caught and returned in the result.
    """
    from openadapt_evals.adapters.base import BenchmarkTask
    from openadapt_evals.adapters.rl_env import ResetConfig, RLEnvironment
    from openadapt_evals.adapters.waa.live import WAALiveAdapter, WAALiveConfig

    start_time = time.time()
    result: dict[str, Any] = {
        "task_id": task_id,
        "started_at": datetime.now().isoformat(),
        "score": 0.0,
        "success": False,
        "steps": 0,
        "error": None,
        "error_type": None,
    }

    try:
        adapter = WAALiveAdapter(WAALiveConfig(server_url=server_url))
        env = RLEnvironment(adapter, task_config=task_config)

        obs = env.reset(config=ResetConfig(task_id=task_id))

        task = BenchmarkTask(
            task_id=task_id,
            instruction=(
                task_config.name
                if task_config
                else f"Task {task_id}"
            ),
            domain="desktop",
        )

        # Save reset screenshot
        task_screenshot_dir = None
        if save_screenshots and screenshots_dir:
            task_screenshot_dir = screenshots_dir / task_id[:12]
            task_screenshot_dir.mkdir(parents=True, exist_ok=True)
            if obs.screenshot:
                (task_screenshot_dir / "step_00_reset.png").write_bytes(
                    obs.screenshot
                )

        for step in range(max_steps):
            if _shutdown_requested:
                result["error"] = "shutdown_requested"
                result["error_type"] = "interrupted"
                break

            action = agent.act(obs, task)

            if action.type == "done":
                logger.info("Task %s: agent signaled DONE at step %d", task_id[:8], step + 1)
                break

            if action.type == "error":
                result["error"] = str(action.raw_action)
                result["error_type"] = "agent"
                logger.error("Task %s: agent error at step %d", task_id[:8], step + 1)
                break

            # Execute action
            if action.x is not None and action.y is not None:
                x = float(action.x)
                y = float(action.y)
                if 0 <= x <= 1 and 0 <= y <= 1:
                    step_result = env.pixel_action(
                        x_frac=x,
                        y_frac=y,
                        action_type=action.type,
                        text=action.text,
                        key=action.key,
                    )
                else:
                    step_result = env.pixel_action(
                        x=int(x),
                        y=int(y),
                        action_type=action.type,
                        text=action.text,
                        key=action.key,
                    )
            else:
                step_result = env.step(action)

            obs = step_result.observation

            if task_screenshot_dir and obs.screenshot:
                (task_screenshot_dir / f"step_{step + 1:02d}.png").write_bytes(
                    obs.screenshot
                )

            if step_result.done:
                logger.info(
                    "Task %s: env signaled done at step %d", task_id[:8], step + 1
                )
                break

        # Evaluate
        result["steps"] = env.step_count

        if task_config and task_config.milestones:
            score = env.evaluate_dense()
            last = env.trajectory[-1] if env.trajectory else None
            info = last.info if last else {}
            result["milestones_passed"] = info.get("milestones_passed", 0)
            result["milestones_total"] = info.get("milestones_total", 0)
        else:
            score = env.evaluate()

        result["score"] = score
        result["success"] = score > 0

    except Exception as e:
        result["error"] = str(e)
        result["error_type"] = "infrastructure"
        result["traceback"] = traceback.format_exc()
        logger.error("Task %s failed: %s", task_id[:8], e)

    elapsed = time.time() - start_time
    result["elapsed_seconds"] = round(elapsed, 2)
    result["finished_at"] = datetime.now().isoformat()
    return result


# ---------------------------------------------------------------------------
# Summary report
# ---------------------------------------------------------------------------


def print_summary(results: list[dict], total_elapsed: float) -> None:
    """Print a summary table of all results."""
    if not results:
        print("\nNo results to summarize.")
        return

    total = len(results)
    successes = sum(1 for r in results if r.get("success"))
    errors = sum(1 for r in results if r.get("error"))
    infra_errors = sum(
        1 for r in results if r.get("error_type") == "infrastructure"
    )
    agent_errors = sum(1 for r in results if r.get("error_type") == "agent")
    scores = [r.get("score", 0.0) for r in results]
    avg_score = sum(scores) / total if total else 0.0
    total_steps = sum(r.get("steps", 0) for r in results)
    avg_steps = total_steps / total if total else 0.0
    task_times = [r.get("elapsed_seconds", 0.0) for r in results]
    avg_time = sum(task_times) / total if total else 0.0

    non_infra = [r for r in results if r.get("error_type") != "infrastructure"]
    non_infra_success = sum(1 for r in non_infra if r.get("success"))
    adj_rate = non_infra_success / len(non_infra) if non_infra else 0.0

    print("\n" + "=" * 70)
    print("EVALUATION SUMMARY")
    print("=" * 70)
    print(f"  Total tasks:        {total}")
    print(f"  Passed:             {successes} ({successes / total:.1%})")
    print(f"  Failed:             {total - successes}")
    print(f"  Avg score:          {avg_score:.3f}")
    print(f"  Avg steps:          {avg_steps:.1f}")
    print(f"  Avg task time:      {avg_time:.1f}s")
    print(f"  Total time:         {total_elapsed / 60:.1f} min")
    # `errors` counts every result carrying an `error`. It is printed
    # separately because `infra_errors + agent_errors` misses any error with no
    # `error_type`, which the per-task table shows as ERROR but which was
    # previously absent from the aggregate summary.
    if errors:
        print(f"  Errors:             {errors}")
    if infra_errors:
        print(f"  Infra errors:       {infra_errors}")
        print(f"  Adj success rate:   {adj_rate:.1%} (excluding infra)")
    if agent_errors:
        print(f"  Agent errors:       {agent_errors}")

    # Per-task results
    print()
    print(f"{'Task ID':>14s}  {'Score':>6s}  {'Steps':>5s}  {'Time':>7s}  Status")
    print("-" * 60)
    for r in results:
        tid = r["task_id"][:12] + ".."
        score = r.get("score", 0.0)
        steps = r.get("steps", 0)
        t = r.get("elapsed_seconds", 0.0)
        if r.get("success"):
            status = "PASS"
        elif r.get("error_type") == "infrastructure":
            status = "INFRA"
        elif r.get("error"):
            status = "ERROR"
        else:
            status = "FAIL"
        print(f"  {tid:>14s}  {score:6.2f}  {steps:5d}  {t:6.1f}s  {status}")
    print("=" * 70)


# ---------------------------------------------------------------------------
# Parallel pool execution
# ---------------------------------------------------------------------------


def run_parallel_pool(args: argparse.Namespace, task_ids: list[str]) -> int:
    """Run evaluation distributed across pool VMs."""
    from openadapt_evals.infrastructure.pool import PoolManager

    cloud = getattr(args, "cloud", None)
    if cloud == "aws":
        from openadapt_evals.infrastructure.aws_vm import AWSVMManager
        vm_manager = AWSVMManager()
    else:
        from openadapt_evals.infrastructure.azure_vm import AzureVMManager
        vm_manager = AzureVMManager()

    manager = PoolManager(vm_manager=vm_manager)

    def agent_factory():
        return create_planner_grounder_agent(args)

    try:
        result = manager.run(
            tasks=len(task_ids),
            agent_factory=agent_factory,
        )
        print(f"\nPool run complete: {result.completed} completed, {result.failed} failed")
        return 0 if result.failed == 0 else 1
    except Exception as e:
        logger.error("Pool run failed: %s", e)
        return 1


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Full WAA evaluation runner with resume support",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Server / connectivity
    parser.add_argument(
        "--server-url",
        default="http://localhost:5001",
        help="WAA server URL (default: http://localhost:5001)",
    )

    # Task selection
    parser.add_argument(
        "--task-ids",
        default=None,
        help="Comma-separated task IDs (default: all WAA tasks from server)",
    )

    # Resume / output
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from last checkpoint (skips completed tasks in output file)",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Output JSONL file path (default: benchmark_results/full_eval_<timestamp>.jsonl)",
    )

    # Execution
    parser.add_argument("--max-steps", type=int, default=15)
    parser.add_argument(
        "--save-screenshots",
        action="store_true",
        help="Save screenshots per task to screenshots/ subdirectory",
    )
    parser.add_argument(
        "--screenshots-dir",
        default=None,
        help="Directory for screenshots (default: <output_dir>/screenshots)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List tasks without running them",
    )

    # Planner config
    parser.add_argument(
        "--planner-model",
        default="claude-sonnet-4-6",
    )
    parser.add_argument(
        "--planner-provider",
        default="anthropic",
    )

    # Grounder config
    parser.add_argument(
        "--grounder-endpoint",
        default=None,
        help="HTTP endpoint for grounder (e.g., vLLM serving UI-Venus)",
    )
    parser.add_argument(
        "--grounder-model",
        default=None,
        help="API model name for grounder (e.g., gpt-4.1-mini)",
    )
    parser.add_argument(
        "--grounder-provider",
        default="openai",
        help="Provider for API grounder model (default: openai)",
    )

    # Parallelism
    parser.add_argument(
        "--parallel",
        type=int,
        default=0,
        help="Number of pool VMs for parallel execution (0 = sequential single VM)",
    )
    parser.add_argument(
        "--cloud",
        choices=["azure", "aws"],
        default=None,
        help="Cloud provider for pool VMs",
    )

    # Retry settings
    parser.add_argument(
        "--max-server-retries",
        type=int,
        default=5,
        help="Max retries when server is unreachable (default: 5)",
    )
    parser.add_argument(
        "--retry-base-delay",
        type=float,
        default=5.0,
        help="Base delay (seconds) for exponential backoff (default: 5)",
    )

    args = parser.parse_args()

    # Validate grounder config
    if not args.dry_run and not args.grounder_endpoint and not args.grounder_model:
        parser.error(
            "Specify either --grounder-endpoint (HTTP/vLLM) or --grounder-model (API)"
        )

    # -------------------------------------------------------------------
    # Resolve task list
    # -------------------------------------------------------------------
    if args.task_ids:
        task_ids = [t.strip() for t in args.task_ids.split(",") if t.strip()]
    else:
        logger.info("Discovering tasks from server %s...", args.server_url)
        task_ids = discover_tasks_from_server(args.server_url)
        if not task_ids:
            logger.error(
                "Could not discover tasks from server. "
                "Provide --task-ids explicitly or ensure server is reachable."
            )
            return 1

    # -------------------------------------------------------------------
    # Dry run
    # -------------------------------------------------------------------
    if args.dry_run:
        print(f"\nDry run: {len(task_ids)} tasks would be evaluated\n")
        print(f"Server: {args.server_url}")
        if not args.dry_run or (args.grounder_endpoint or args.grounder_model):
            print(f"Planner: {args.planner_model} ({args.planner_provider})")
            print(
                f"Grounder: {args.grounder_endpoint or args.grounder_model} "
                f"({args.grounder_provider})"
            )
        print(f"Max steps: {args.max_steps}")
        if args.parallel:
            print(f"Parallel: {args.parallel} workers")
        print("\nTasks:")
        for i, tid in enumerate(task_ids, 1):
            print(f"  {i:3d}. {tid}")
        return 0

    # -------------------------------------------------------------------
    # Parallel pool mode
    # -------------------------------------------------------------------
    if args.parallel > 0:
        return run_parallel_pool(args, task_ids)

    # -------------------------------------------------------------------
    # Sequential single-VM mode
    # -------------------------------------------------------------------

    # Resolve output path
    output_path = _results_path(args.output)
    logger.info("Results will be saved to: %s", output_path)

    # Resume: skip already-completed tasks
    completed_ids = set()
    if args.resume:
        completed_ids = _load_completed_task_ids(output_path)
        if completed_ids:
            logger.info(
                "Resuming: %d tasks already completed, %d remaining",
                len(completed_ids),
                len(task_ids) - len(completed_ids),
            )

    remaining_tasks = [t for t in task_ids if t not in completed_ids]

    if not remaining_tasks:
        logger.info("All tasks already completed!")
        # Load and print summary from existing file
        all_results = []
        with open(output_path, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        all_results.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        print_summary(all_results, 0.0)
        return 0

    # Check server connectivity
    logger.info("Checking server at %s...", args.server_url)
    if not wait_for_server(
        args.server_url,
        max_retries=args.max_server_retries,
        base_delay=args.retry_base_delay,
    ):
        logger.error(
            "Cannot reach server at %s after %d retries. "
            "Ensure SSH tunnel is active and WAA is running.",
            args.server_url,
            args.max_server_retries,
        )
        return 1

    # Create agent
    logger.info(
        "Creating PlannerGrounderAgent (planner=%s, grounder=%s)",
        args.planner_model,
        args.grounder_endpoint or args.grounder_model,
    )
    agent = create_planner_grounder_agent(args)

    # Screenshots directory
    screenshots_dir = None
    if args.save_screenshots:
        if args.screenshots_dir:
            screenshots_dir = Path(args.screenshots_dir)
        else:
            screenshots_dir = output_path.parent / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Screenshots will be saved to: %s", screenshots_dir)

    # -------------------------------------------------------------------
    # Main evaluation loop
    # -------------------------------------------------------------------
    total_tasks = len(remaining_tasks)
    all_results: list[dict] = []
    run_start = time.time()

    # Write run metadata header
    meta = {
        "_meta": True,
        "run_started": datetime.now().isoformat(),
        "planner_model": args.planner_model,
        "planner_provider": args.planner_provider,
        "grounder_model": args.grounder_model,
        "grounder_endpoint": args.grounder_endpoint,
        "grounder_provider": args.grounder_provider,
        "server_url": args.server_url,
        "max_steps": args.max_steps,
        "total_tasks": len(task_ids),
        "remaining_tasks": total_tasks,
        "resumed": args.resume,
    }
    _append_result(output_path, meta)

    logger.info(
        "Starting evaluation: %d tasks, max %d steps each",
        total_tasks,
        args.max_steps,
    )

    for i, task_id in enumerate(remaining_tasks):
        if _shutdown_requested:
            logger.warning("Shutdown requested, stopping after %d/%d tasks", i, total_tasks)
            break

        # Progress header
        elapsed = time.time() - run_start
        if i > 0 and elapsed > 0:
            rate = elapsed / i
            eta_seconds = rate * (total_tasks - i)
            eta_str = f"{eta_seconds / 60:.1f}m remaining"
        else:
            eta_str = "estimating..."

        logger.info(
            "=== Task %d/%d [%s] (%s) ===",
            i + 1,
            total_tasks,
            task_id[:12],
            eta_str,
        )

        # Health check with retry before each task
        if not check_server_health(args.server_url):
            logger.warning("Server unreachable, attempting reconnect...")
            if not wait_for_server(
                args.server_url,
                max_retries=args.max_server_retries,
                base_delay=args.retry_base_delay,
            ):
                result = {
                    "task_id": task_id,
                    "score": 0.0,
                    "success": False,
                    "steps": 0,
                    "error": "Server unreachable after retries",
                    "error_type": "infrastructure",
                    "elapsed_seconds": 0.0,
                    "finished_at": datetime.now().isoformat(),
                }
                _append_result(output_path, result)
                all_results.append(result)
                logger.error("Skipping task %s: server unreachable", task_id[:8])
                continue

        # Reset agent for new task
        agent.reset()

        # Run the task
        result = run_single_task(
            task_id=task_id,
            agent=agent,
            server_url=args.server_url,
            max_steps=args.max_steps,
            save_screenshots=args.save_screenshots,
            screenshots_dir=screenshots_dir,
        )

        # Save immediately
        _append_result(output_path, result)
        all_results.append(result)

        # Progress report
        status = "PASS" if result.get("success") else "FAIL"
        logger.info(
            "Task %s: %s (score=%.2f, steps=%d, time=%.1fs)",
            task_id[:8],
            status,
            result.get("score", 0.0),
            result.get("steps", 0),
            result.get("elapsed_seconds", 0.0),
        )

    total_elapsed = time.time() - run_start

    # Include previously completed results in summary if resuming
    if args.resume and completed_ids:
        prev_results = []
        with open(output_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    if record.get("_meta"):
                        continue
                    if record.get("task_id") in completed_ids:
                        prev_results.append(record)
                except json.JSONDecodeError:
                    pass
        all_results = prev_results + all_results

    print_summary(all_results, total_elapsed)
    print(f"\nResults saved to: {output_path}")

    if _shutdown_requested:
        print("\nRun was interrupted. Resume with:")
        print(f"  python scripts/run_full_eval.py --resume --output {output_path} \\")
        print(f"    --server-url {args.server_url} \\")
        if args.grounder_endpoint:
            print(f"    --grounder-endpoint {args.grounder_endpoint}")
        elif args.grounder_model:
            print(f"    --grounder-model {args.grounder_model}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

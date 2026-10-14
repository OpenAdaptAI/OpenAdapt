#!/usr/bin/env python3
"""Run the planner-grounder agent against a WAA VM.

Usage:
    # With Claude planner + UI-Venus grounder (served via vLLM):
    python scripts/run_planner_grounder.py \
        --server-url http://localhost:5001 \
        --grounder-endpoint http://gpu-host:8000/v1 \
        --task-id custom-notepad-hello \
        --task-config example_tasks/notepad-hello.yaml

    # With Claude planner + OpenAI grounder (API, no GPU needed):
    python scripts/run_planner_grounder.py \
        --server-url http://localhost:5001 \
        --grounder-model gpt-4.1-mini \
        --grounder-provider openai \
        --task-id custom-notepad-hello

Prerequisites:
    - WAA VM running with SSH tunnel (port 5001 → VM port 5000)
    - For HTTP grounder: UI-Venus serving via `bash scripts/serve_grounder.sh`
    - ANTHROPIC_API_KEY set (for Claude planner)
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run_planner_grounder")


def main():
    parser = argparse.ArgumentParser(
        description="Run planner-grounder agent against WAA VM"
    )
    parser.add_argument("--server-url", default="http://localhost:5001",
                        help="WAA server URL")
    parser.add_argument("--task-id", required=True,
                        help="Task ID to run")
    parser.add_argument("--task-config", default=None,
                        help="Path to task YAML config (for milestones/eval)")
    parser.add_argument("--max-steps", type=int, default=15)
    parser.add_argument("--save-screenshots", default=None,
                        help="Directory to save screenshots at each step")
    parser.add_argument("--generate-trace", action="store_true",
                        help="Auto-generate a markdown trace report after the run "
                             "(implies --save-screenshots to a temp dir if not set)")

    # Planner config
    parser.add_argument("--planner-model", default="claude-sonnet-4-6")
    parser.add_argument("--planner-provider", default="anthropic")

    # Grounder config (choose one mode)
    parser.add_argument("--grounder-endpoint", default=None,
                        help="HTTP endpoint for grounder (e.g., vLLM serving UI-Venus)")
    parser.add_argument("--grounder-model", default=None,
                        help="API model name for grounder (e.g., gpt-4.1-mini)")
    parser.add_argument("--grounder-provider", default="openai",
                        help="Provider for API grounder model")

    args = parser.parse_args()

    # Validate grounder config
    if not args.grounder_endpoint and not args.grounder_model:
        parser.error("Specify either --grounder-endpoint (HTTP/vLLM) or --grounder-model (API)")

    # --generate-trace implies --save-screenshots
    if args.generate_trace and not args.save_screenshots:
        args.save_screenshots = tempfile.mkdtemp(prefix="trace_screenshots_")
        logger.info("--generate-trace: screenshots will be saved to %s", args.save_screenshots)

    # Load task config if provided
    task_config = None
    if args.task_config:
        from openadapt_evals.task_config import TaskConfig
        task_config = TaskConfig.from_yaml(args.task_config)
        logger.info("Loaded task config: %s (%d milestones)",
                     task_config.name, len(task_config.milestones))

    # Create adapter
    from openadapt_evals.adapters.waa.live import WAALiveAdapter, WAALiveConfig
    adapter = WAALiveAdapter(WAALiveConfig(server_url=args.server_url))

    # Create agent
    from openadapt_evals.agents.planner_grounder_agent import PlannerGrounderAgent

    # Set up trajectory logger for trace generation
    trajectory_logger = None
    if args.generate_trace:
        from openadapt_evals.training.trajectory_logger import PlannerTrajectoryLogger
        trajectory_logger = PlannerTrajectoryLogger(output_dir=args.save_screenshots)

    if args.grounder_endpoint:
        agent = PlannerGrounderAgent(
            planner=args.planner_model,
            grounder="http",
            planner_provider=args.planner_provider,
            grounder_provider="http",
            grounder_endpoint=args.grounder_endpoint,
            trajectory_logger=trajectory_logger,
        )
        logger.info("Grounder: HTTP endpoint at %s", args.grounder_endpoint)
    else:
        agent = PlannerGrounderAgent(
            planner=args.planner_model,
            grounder=args.grounder_model,
            planner_provider=args.planner_provider,
            grounder_provider=args.grounder_provider,
            trajectory_logger=trajectory_logger,
        )
        logger.info("Grounder: %s via %s", args.grounder_model, args.grounder_provider)

    logger.info("Planner: %s via %s", args.planner_model, args.planner_provider)

    # Create environment
    from openadapt_evals.adapters.rl_env import ResetConfig, RLEnvironment

    env = RLEnvironment(adapter, task_config=task_config)

    # Run episode
    logger.info("=== Starting episode: %s ===", args.task_id)
    start_time = time.time()

    obs = env.reset(config=ResetConfig(task_id=args.task_id))
    logger.info("Environment reset complete. Screenshot: %d bytes",
                 len(obs.screenshot or b""))

    from openadapt_evals.adapters.base import BenchmarkTask
    task = BenchmarkTask(
        task_id=args.task_id,
        instruction=task_config.name if task_config else f"Task {args.task_id}",
        domain="desktop",
    )

    # Screenshot saving
    screenshot_dir = None
    if args.save_screenshots:
        screenshot_dir = Path(args.save_screenshots)
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        if obs.screenshot:
            (screenshot_dir / "step_00_reset.png").write_bytes(obs.screenshot)

    for step in range(args.max_steps):
        logger.info("--- Step %d ---", step + 1)

        action = agent.act(obs, task)
        planner_out = (action.raw_action or {}).get("planner_output", {})
        logger.info("Planner instruction: %s",
                     planner_out.get("instruction", "?")[:100]
                     if isinstance(planner_out, dict) else "?")
        logger.info("Action: type=%s x=%s y=%s text=%s",
                     action.type, action.x, action.y, action.text)

        if action.type == "done":
            logger.info("Agent signaled DONE at step %d", step + 1)
            break

        if action.type == "error":
            logger.error("Agent signaled FAIL at step %d", step + 1)
            break

        # Execute action
        if action.x is not None and action.y is not None:
            x = float(action.x)
            y = float(action.y)
            if 0 <= x <= 1 and 0 <= y <= 1:
                step_result = env.pixel_action(
                    x_frac=x, y_frac=y,
                    action_type=action.type, text=action.text, key=action.key,
                )
            else:
                step_result = env.pixel_action(
                    x=int(x), y=int(y),
                    action_type=action.type, text=action.text, key=action.key,
                )
        else:
            step_result = env.step(action)

        obs = step_result.observation
        if screenshot_dir and obs.screenshot:
            (screenshot_dir / f"step_{step+1:02d}.png").write_bytes(obs.screenshot)
        if step_result.done:
            logger.info("Environment signaled done at step %d", step + 1)
            break

    # Evaluate
    elapsed = time.time() - start_time
    logger.info("=== Episode complete: %d steps in %.1fs ===", env.step_count, elapsed)

    if task_config and task_config.milestones:
        score = env.evaluate_dense()
        last = env.trajectory[-1] if env.trajectory else None
        info = last.info if last else {}
        logger.info("Dense evaluation: score=%.2f (milestones=%d/%d, binary=%.2f)",
                     score,
                     info.get("milestones_passed", 0),
                     info.get("milestones_total", 0),
                     info.get("binary_score", 0.0))
    else:
        score = env.evaluate()
        logger.info("Binary evaluation: score=%.2f", score)

    # Summary
    print("\n" + "=" * 60)
    print(f"Task: {task_config.name if task_config else args.task_id}")
    print(f"Steps: {env.step_count}")
    print(f"Score: {score}")
    print(f"Time: {elapsed:.1f}s")
    print(f"Planner: {args.planner_model}")
    print(f"Grounder: {args.grounder_endpoint or args.grounder_model}")
    print("=" * 60)

    # Generate trace report
    if args.generate_trace and screenshot_dir:
        from scripts.generate_trace_report import generate_report

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        task_name = task_config.name if task_config else args.task_id
        traces_dir = Path("docs/traces")
        traces_dir.mkdir(parents=True, exist_ok=True)
        output_path = traces_dir / f"{args.task_id}_{timestamp}.md"

        trajectory_path = None
        if trajectory_logger:
            trajectory_path = trajectory_logger.jsonl_path

        report = generate_report(
            screenshots_dir=screenshot_dir,
            trajectory_path=trajectory_path,
            output_path=output_path,
            task_name=task_name,
            score=score,
            run_date=datetime.now().strftime("%Y-%m-%d"),
        )
        print(f"Trace report: {report}")

    return 0 if score > 0 else 1


if __name__ == "__main__":
    sys.exit(main())

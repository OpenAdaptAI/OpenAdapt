#!/usr/bin/env python3
"""Systematic model comparison framework for WAA tasks.

Supports API models (OpenAI, Anthropic) and local models served via SGLang
on a remote GPU host. SGLang models are served over SSH and tunneled back
to localhost as OpenAI-compatible endpoints.

Usage:
    # Basic comparison (VM/tunnels already running):
    python scripts/compare_models.py --config example_comparisons/api_models.yaml

    # With full VM lifecycle management:
    python scripts/compare_models.py \\
        --config example_comparisons/api_models.yaml \\
        --manage-vm --setup-tunnels

    # Compare API + local models (SGLang on GPU host):
    python scripts/compare_models.py \\
        --config example_comparisons/unified_agents.yaml \\
        --gpu-host user@gpu-server

    # Override server URL:
    python scripts/compare_models.py \\
        --config example_comparisons/api_models.yaml \\
        --server-url http://localhost:5002

    # Resume an interrupted comparison:
    python scripts/compare_models.py \\
        --config example_comparisons/api_models.yaml --resume

Prerequisites:
    - WAA VM running with SSH tunnel (port 5001 -> VM port 5000), OR
    - Use --manage-vm --setup-tunnels for automatic lifecycle
    - API keys set (OPENAI_API_KEY, ANTHROPIC_API_KEY as needed)
    - For SGLang models: --gpu-host with SSH access to a machine with GPU(s)
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import signal
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("compare_models")

_shutdown_requested = False


def _signal_handler(signum, frame):
    global _shutdown_requested
    if _shutdown_requested:
        logger.warning("Second interrupt received, forcing exit")
        sys.exit(1)
    _shutdown_requested = True
    logger.warning("Shutdown requested, finishing current run...")


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


def _restore_env_var(key: str, original_value: str | None) -> None:
    """Restore an environment variable to its original value."""
    if original_value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = original_value


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


@dataclass
class ModelConfig:
    """A single model to compare."""

    name: str
    provider: str  # "openai", "anthropic", "sglang"
    type: str = "unified"  # "unified" (Phase 1), "split" (Phase 2+)
    max_new_tokens: int | None = None
    # Phase 2 fields (local models via vLLM):
    model_id: str | None = None
    serve_via: str | None = None
    vllm_args: dict[str, Any] = field(default_factory=dict)
    # SGLang serving config (only used when provider == "sglang"):
    serve: dict[str, Any] | None = None


@dataclass
class ComparisonConfig:
    """Full comparison specification loaded from YAML."""

    name: str
    description: str
    tasks: list[str]
    models: list[ModelConfig]
    server_url: str
    max_steps: int
    runs_per_config: int
    save_screenshots: bool
    output_dir: str


def load_config(path: str) -> ComparisonConfig:
    """Load a comparison config from a YAML file."""
    with open(path) as f:
        raw = yaml.safe_load(f)

    models = []
    for m in raw.get("models", []):
        models.append(
            ModelConfig(
                name=m["name"],
                provider=m.get("provider", "openai"),
                type=m.get("type", "unified"),
                max_new_tokens=m.get("max_new_tokens"),
                model_id=m.get("model_id"),
                serve_via=m.get("serve_via"),
                vllm_args=m.get("vllm_args", {}),
                serve=m.get("serve"),
            )
        )

    return ComparisonConfig(
        name=raw.get("name", "Unnamed Comparison"),
        description=raw.get("description", ""),
        tasks=raw.get("tasks", []),
        models=models,
        server_url=raw.get("server_url", "http://localhost:5001"),
        max_steps=raw.get("max_steps", 15),
        runs_per_config=raw.get("runs_per_config", 1),
        save_screenshots=raw.get("save_screenshots", True),
        output_dir=raw.get("output_dir", "comparison_results/"),
    )


# ---------------------------------------------------------------------------
# Infrastructure helpers (reused from run_correction_flywheel.py)
# ---------------------------------------------------------------------------


def _manage_vm_lifecycle(vm_name: str, resource_group: str) -> str | None:
    """Start the VM and return its public IP."""
    from scripts.run_correction_flywheel import get_vm_ip, start_vm, wait_for_ssh

    if not start_vm(vm_name, resource_group):
        return None
    ip = get_vm_ip(vm_name, resource_group)
    if not ip:
        logger.error("Could not get IP for VM %s", vm_name)
        return None
    if not wait_for_ssh(ip):
        return None
    return ip


def _setup_tunnels(vm_ip: str) -> dict[str, int | None]:
    """Set up SSH tunnels to the VM. Returns tunnel PIDs."""
    from scripts.run_correction_flywheel import setup_tunnels

    return setup_tunnels(vm_ip)


def _wait_for_waa(server_url: str, timeout: int = 600) -> bool:
    """Wait for WAA server to respond."""
    from scripts.run_correction_flywheel import wait_for_waa

    return wait_for_waa(server_url, timeout=timeout)


# ---------------------------------------------------------------------------
# Agent creation
# ---------------------------------------------------------------------------


def create_unified_agent(
    model_config: ModelConfig,
    sglang_endpoint: str | None = None,
):
    """Create a unified agent (same model plans + grounds).

    For SGLang-served models, the agent uses the tunneled endpoint as an
    OpenAI-compatible API with the provider set to "openai".
    """
    from openadapt_evals.agents.planner_grounder_agent import PlannerGrounderAgent

    if model_config.provider == "sglang" and sglang_endpoint:
        os.environ["OPENAI_BASE_URL"] = sglang_endpoint
        if not os.environ.get("OPENAI_API_KEY"):
            os.environ["OPENAI_API_KEY"] = "sglang-local"
        agent = PlannerGrounderAgent(
            planner=model_config.name,
            grounder=model_config.name,
            planner_provider="openai",
            grounder_provider="openai",
            grounder_endpoint=sglang_endpoint,
        )
    else:
        agent = PlannerGrounderAgent(
            planner=model_config.name,
            grounder=model_config.name,
            planner_provider=model_config.provider,
            grounder_provider=model_config.provider,
        )
    return agent


# ---------------------------------------------------------------------------
# Single run execution
# ---------------------------------------------------------------------------


def run_single_task(
    task_path: str,
    agent,
    server_url: str,
    max_steps: int,
    screenshots_dir: Path | None,
) -> dict[str, Any]:
    """Run a single task and return a result dict. Never raises."""
    from openadapt_evals.adapters.base import BenchmarkTask, EvaluationUnavailableError
    from openadapt_evals.adapters.rl_env import ResetConfig, RLEnvironment
    from openadapt_evals.adapters.waa.live import WAALiveAdapter, WAALiveConfig
    from openadapt_evals.task_config import TaskConfig

    start_time = time.time()
    task_config = TaskConfig.from_yaml(task_path)
    result: dict[str, Any] = {
        "task_id": task_config.id,
        "task_name": task_config.name,
        "task_path": task_path,
        "started_at": datetime.now().isoformat(),
        "score": 0.0,
        "milestones_passed": 0,
        "milestones_total": 0,
        "steps": 0,
        "actions": [],
        "error": None,
        # None means "this row is a measurement". Anything else means the task
        # was not scored and `score` must be excluded from the matrix rather
        # than rendered as a 0.00 cell in a head-to-head model comparison.
        "error_type": None,
    }
    try:
        adapter = WAALiveAdapter(WAALiveConfig(server_url=server_url))
        env = RLEnvironment(adapter, task_config=task_config)
        obs = env.reset(config=ResetConfig(task_id=task_config.id))
        task = BenchmarkTask(
            task_id=task_config.id,
            instruction=task_config.name,
            domain="desktop",
        )
        task_screenshot_dir = None
        if screenshots_dir:
            task_screenshot_dir = screenshots_dir
            task_screenshot_dir.mkdir(parents=True, exist_ok=True)
            if obs.screenshot:
                (task_screenshot_dir / "step_00_reset.png").write_bytes(obs.screenshot)

        actions_log = []
        for step in range(max_steps):
            if _shutdown_requested:
                result["error"] = "shutdown_requested"
                break
            action = agent.act(obs, task)
            actions_log.append({
                "step": step + 1,
                "type": action.type,
                "x": action.x,
                "y": action.y,
                "text": action.text,
            })
            if action.type == "done":
                break
            if action.type == "error":
                result["error"] = str(action.raw_action)
                break
            if action.x is not None and action.y is not None:
                x = float(action.x)
                y = float(action.y)
                if 0 <= x <= 1 and 0 <= y <= 1:
                    step_result = env.pixel_action(
                        x_frac=x, y_frac=y, action_type=action.type,
                        text=action.text, key=action.key,
                    )
                else:
                    step_result = env.pixel_action(
                        x=int(x), y=int(y), action_type=action.type,
                        text=action.text, key=action.key,
                    )
            else:
                step_result = env.step(action)
            obs = step_result.observation
            if task_screenshot_dir and obs.screenshot:
                (task_screenshot_dir / f"step_{step + 1:02d}.png").write_bytes(obs.screenshot)
            if step_result.done:
                break

        result["steps"] = env.step_count
        result["actions"] = actions_log
        if task_config.milestones:
            score = env.evaluate_dense()
            last = env.trajectory[-1] if env.trajectory else None
            info = last.info if last else {}
            result["milestones_passed"] = info.get("milestones_passed", 0)
            result["milestones_total"] = info.get("milestones_total", 0)
        else:
            score = env.evaluate()
        result["score"] = score
    except EvaluationUnavailableError as e:
        result["error"] = str(e)
        result["error_type"] = e.error_type
        result["traceback"] = traceback.format_exc()
        logger.error("Task %s was not scored: %s", task_path, e)
    except Exception as e:
        result["error"] = str(e)
        result["error_type"] = "infrastructure"
        result["traceback"] = traceback.format_exc()
        logger.error("Task %s failed: %s", task_path, e)

    result["elapsed_seconds"] = round(time.time() - start_time, 2)
    result["finished_at"] = datetime.now().isoformat()
    return result


# ---------------------------------------------------------------------------
# Comparison orchestrator
# ---------------------------------------------------------------------------


def run_comparison(
    config: ComparisonConfig,
    server_url_override: str | None = None,
    resume: bool = False,
    gpu_host: str | None = None,
    ssh_key: str | None = None,
) -> list[dict[str, Any]]:
    """Run all (model, task) combinations and collect results."""
    server_url = server_url_override or config.server_url
    output_dir = Path(config.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    results_path = output_dir / "results.jsonl"

    completed_keys: set[str] = set()
    existing_results: list[dict] = []
    if resume and results_path.exists():
        with open(results_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("_meta"):
                        continue
                    key = f"{rec.get('model_name')}::{rec.get('task_path')}"
                    completed_keys.add(key)
                    existing_results.append(rec)
                except json.JSONDecodeError:
                    continue
        if completed_keys:
            logger.info("Resume: %d runs already completed", len(completed_keys))

    meta = {
        "_meta": True,
        "comparison_name": config.name,
        "description": config.description,
        "started_at": datetime.now().isoformat(),
        "models": [m.name for m in config.models],
        "tasks": config.tasks,
        "max_steps": config.max_steps,
        "server_url": server_url,
    }
    with open(results_path, "a") as f:
        f.write(json.dumps(meta, default=str) + "\n")

    all_results = list(existing_results)
    total_runs = len(config.models) * len(config.tasks) * config.runs_per_config
    completed = len(completed_keys)

    logger.info(
        "Starting comparison: %d models x %d tasks x %d runs = %d total (%d remaining)",
        len(config.models), len(config.tasks), config.runs_per_config,
        total_runs, total_runs - completed,
    )

    for model_config in config.models:
        if _shutdown_requested:
            break

        logger.info("=" * 60)
        logger.info("MODEL: %s (%s, %s)", model_config.name, model_config.provider, model_config.type)
        logger.info("=" * 60)

        if model_config.type != "unified":
            logger.warning("Skipping %s: only 'unified' type supported in Phase 1", model_config.name)
            continue

        # --- SGLang server lifecycle ---
        sglang_manager = None
        sglang_endpoint = None
        saved_base_url = os.environ.get("OPENAI_BASE_URL")
        saved_api_key = os.environ.get("OPENAI_API_KEY")

        if model_config.provider == "sglang":
            if not gpu_host:
                logger.warning(
                    "Skipping %s: provider is 'sglang' but --gpu-host not provided",
                    model_config.name,
                )
                for task_path in config.tasks:
                    result = {
                        "model_name": model_config.name,
                        "model_type": model_config.type,
                        "model_provider": model_config.provider,
                        "task_path": task_path,
                        "score": 0.0,
                        "error": "Skipped: --gpu-host not provided for sglang model",
                        "elapsed_seconds": 0.0,
                        "finished_at": datetime.now().isoformat(),
                    }
                    all_results.append(result)
                    with open(results_path, "a") as f:
                        f.write(json.dumps(result, default=str) + "\n")
                continue

            from scripts.sglang_server import SGLangServeConfig, setup_sglang_server

            serve_raw = model_config.serve or {}
            serve_cfg = SGLangServeConfig(
                engine=serve_raw.get("engine", "sglang"),
                port=serve_raw.get("port", 8080),
                args=serve_raw.get("args", ""),
            )
            logger.info("Setting up SGLang server for %s on %s...", model_config.name, gpu_host)
            sglang_manager = setup_sglang_server(
                model_name=model_config.name,
                gpu_host=gpu_host,
                serve_config=serve_cfg,
                ssh_key=ssh_key,
            )
            if not sglang_manager:
                logger.error("Failed to set up SGLang server for %s", model_config.name)
                for task_path in config.tasks:
                    result = {
                        "model_name": model_config.name,
                        "model_type": model_config.type,
                        "model_provider": model_config.provider,
                        "task_path": task_path,
                        "score": 0.0,
                        "error": "SGLang server setup failed",
                        "elapsed_seconds": 0.0,
                        "finished_at": datetime.now().isoformat(),
                    }
                    all_results.append(result)
                    with open(results_path, "a") as f:
                        f.write(json.dumps(result, default=str) + "\n")
                continue
            sglang_endpoint = sglang_manager.endpoint
            logger.info("SGLang endpoint ready: %s", sglang_endpoint)

        # Create agent
        try:
            agent = create_unified_agent(model_config, sglang_endpoint=sglang_endpoint)
        except Exception as e:
            logger.error("Failed to create agent for %s: %s", model_config.name, e)
            for task_path in config.tasks:
                result = {
                    "model_name": model_config.name,
                    "model_type": model_config.type,
                    "model_provider": model_config.provider,
                    "task_path": task_path,
                    "score": 0.0,
                    "error": f"Agent creation failed: {e}",
                    "elapsed_seconds": 0.0,
                    "finished_at": datetime.now().isoformat(),
                }
                all_results.append(result)
                with open(results_path, "a") as f:
                    f.write(json.dumps(result, default=str) + "\n")
            if sglang_manager:
                sglang_manager.stop()
            _restore_env_var("OPENAI_BASE_URL", saved_base_url)
            _restore_env_var("OPENAI_API_KEY", saved_api_key)
            continue

        try:
            for task_path in config.tasks:
                if _shutdown_requested:
                    break
                for run_idx in range(config.runs_per_config):
                    run_key = f"{model_config.name}::{task_path}"
                    if run_key in completed_keys:
                        logger.info("Skipping %s on %s (already completed)", model_config.name, task_path)
                        continue
                    completed += 1
                    logger.info(
                        "--- Run %d/%d: %s on %s (run %d) ---",
                        completed, total_runs, model_config.name, task_path, run_idx + 1,
                    )
                    agent.reset()
                    task_slug = Path(task_path).stem
                    screenshots_dir = None
                    if config.save_screenshots:
                        screenshots_dir = Path(config.output_dir) / model_config.name / task_slug / "screenshots"

                    result = run_single_task(
                        task_path=task_path, agent=agent, server_url=server_url,
                        max_steps=config.max_steps, screenshots_dir=screenshots_dir,
                    )
                    result["model_name"] = model_config.name
                    result["model_type"] = model_config.type
                    result["model_provider"] = model_config.provider
                    result["run_index"] = run_idx
                    all_results.append(result)
                    with open(results_path, "a") as f:
                        f.write(json.dumps(result, default=str) + "\n")
                    status = "PASS" if result.get("score", 0) > 0 else "FAIL"
                    logger.info(
                        "  Result: %s (score=%.2f, steps=%d, time=%.1fs)",
                        status, result.get("score", 0.0), result.get("steps", 0),
                        result.get("elapsed_seconds", 0.0),
                    )
        finally:
            if sglang_manager:
                logger.info("Stopping SGLang server for %s...", model_config.name)
                sglang_manager.stop()
            _restore_env_var("OPENAI_BASE_URL", saved_base_url)
            _restore_env_var("OPENAI_API_KEY", saved_api_key)

    return [r for r in all_results if not r.get("_meta")]


# ---------------------------------------------------------------------------
# Summary and report generation
# ---------------------------------------------------------------------------


def build_summary(results: list[dict], config: ComparisonConfig) -> dict:
    """Build a summary JSON with score matrix."""
    matrix: dict[str, dict[str, float]] = {}
    steps_matrix: dict[str, dict[str, int]] = {}
    time_matrix: dict[str, dict[str, float]] = {}
    # Rows that were never scored. Averaging their 0.0 into avg_score would
    # make a model whose SGLang server failed to start look worse than one
    # whose server came up -- a pure-infrastructure delta published as a
    # capability delta.
    unscored: dict[str, dict[str, str]] = {}
    for r in results:
        model = r.get("model_name", "unknown")
        task = r.get("task_path", "unknown")
        if model not in matrix:
            matrix[model] = {}
            steps_matrix[model] = {}
            time_matrix[model] = {}
            unscored[model] = {}
        if r.get("error_type"):
            unscored[model][task] = str(r.get("error_type"))
            continue
        matrix[model][task] = r.get("score", 0.0)
        steps_matrix[model][task] = r.get("steps", 0)
        time_matrix[model][task] = r.get("elapsed_seconds", 0.0)
    model_averages = {}
    for model, scores in matrix.items():
        vals = list(scores.values())
        model_averages[model] = {
            # None, not 0.0: no task of this model's produced a measurement.
            "avg_score": sum(vals) / len(vals) if vals else None,
            "avg_steps": sum(steps_matrix[model].values()) / len(vals) if vals else None,
            "avg_time": sum(time_matrix[model].values()) / len(vals) if vals else None,
            "total_runs": len(vals),
            "unscored_runs": len(unscored.get(model, {})),
            "unscored_tasks": unscored.get(model, {}),
        }
    return {
        "comparison_name": config.name,
        "description": config.description,
        "generated_at": datetime.now().isoformat(),
        "tasks": config.tasks,
        "models": [m.name for m in config.models],
        "score_matrix": matrix,
        "steps_matrix": steps_matrix,
        "time_matrix": time_matrix,
        "model_averages": model_averages,
    }


def _fmt_avg(value: float | None, width: int, precision: int, suffix: str = "") -> str:
    """Render an average, or `--` when nothing was scored.

    Printing 0.00 for "no task of this model produced a measurement" is the
    defect this whole module is being repaired for.
    """
    if value is None:
        return f"{'--':>{width}}{suffix}"
    return f"{value:>{width}.{precision}f}{suffix}"


def print_summary_table(summary: dict) -> None:
    """Print a formatted comparison table to stdout."""
    tasks = summary["tasks"]
    models = summary["models"]
    score_matrix = summary["score_matrix"]
    averages = summary["model_averages"]
    task_labels = [Path(t).stem for t in tasks]
    model_col_w = max(len(m) for m in models) + 2
    task_col_w = max(max((len(t) for t in task_labels), default=6), 8)
    print()
    print("=" * 70)
    print(f"COMPARISON: {summary['comparison_name']}")
    print(f"Tasks: {len(tasks)}  |  Models: {len(models)}")
    print("=" * 70)
    header = f"{'Model':<{model_col_w}}"
    for label in task_labels:
        header += f"  {label:>{task_col_w}}"
    header += f"  {'Avg':>6}  {'Steps':>5}  {'Time':>6}"
    print(header)
    print("-" * len(header))
    for model in models:
        row = f"{model:<{model_col_w}}"
        for task in tasks:
            score = score_matrix.get(model, {}).get(task, None)
            if score is not None:
                row += f"  {score:>{task_col_w}.2f}"
            else:
                row += f"  {'--':>{task_col_w}}"
        avg = averages.get(model, {})
        row += "  " + _fmt_avg(avg.get("avg_score"), 6, 2)
        row += "  " + _fmt_avg(avg.get("avg_steps"), 5, 1)
        row += "  " + _fmt_avg(avg.get("avg_time"), 5, 1, "s")
        unscored_n = avg.get("unscored_runs", 0)
        if unscored_n:
            row += f"  [{unscored_n} unscored]"
        print(row)
    print("=" * 70)


def generate_html_report(summary: dict, results: list[dict], output_path: Path) -> Path:
    """Generate an HTML comparison report."""
    tasks = summary["tasks"]
    models = summary["models"]
    score_matrix = summary["score_matrix"]
    averages = summary["model_averages"]
    task_labels = [Path(t).stem for t in tasks]
    rows_html = ""
    for model in models:
        cells = f"<td><strong>{html.escape(model)}</strong></td>"
        for task in tasks:
            score = score_matrix.get(model, {}).get(task, None)
            if score is not None:
                css = "pass" if score > 0 else "fail"
                cells += f'<td class="{css}">{score:.2f}</td>'
            else:
                cells += "<td>--</td>"
        avg = averages.get(model, {})
        cells += (
            f"<td><strong>{_fmt_avg(avg.get('avg_score'), 0, 2).strip()}"
            f"</strong></td>"
        )
        cells += f"<td>{_fmt_avg(avg.get('avg_steps'), 0, 1).strip()}</td>"
        cells += f"<td>{_fmt_avg(avg.get('avg_time'), 0, 1, 's').strip()}</td>"
        rows_html += f"<tr>{cells}</tr>\n"
    task_headers = "".join(f"<th>{html.escape(t)}</th>" for t in task_labels)
    details_html = ""
    for r in results:
        model = r.get("model_name", "?")
        task_name = r.get("task_name", r.get("task_path", "?"))
        unscored_reason = r.get("error_type")
        score = r.get("score", 0.0)
        steps = r.get("steps", 0)
        elapsed = r.get("elapsed_seconds", 0.0)
        error = r.get("error", "")
        actions = r.get("actions", [])
        actions_list = ""
        for a in actions:
            actions_list += (
                f"<li>Step {a.get('step', '?')}: "
                f"{html.escape(str(a.get('type', '?')))} "
                f"({html.escape(str(a.get('text', '') or ''))})</li>\n"
            )
        if unscored_reason:
            css = "unscored"
            score_text = f"NOT SCORED ({html.escape(str(unscored_reason))})"
        else:
            css = "pass" if score > 0 else "fail"
            score_text = f"score={score:.2f}"
        error_block = ""
        if error:
            error_block = f"<p class='error'>Error: {html.escape(error)}</p>"
        details_html += f"""
        <details>
            <summary class="{css}">
                <strong>{html.escape(model)}</strong> on
                <em>{html.escape(task_name)}</em>:
                {score_text}, steps={steps}, time={elapsed:.1f}s
            </summary>
            <div class="detail-content">
                {error_block}
                <ul>{actions_list}</ul>
            </div>
        </details>"""
    report_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>{html.escape(summary['comparison_name'])}</title>
    <style>
        body {{ font-family: -apple-system, sans-serif; max-width: 960px; margin: 2em auto; padding: 0 1em; }}
        h1 {{ border-bottom: 2px solid #333; padding-bottom: 0.5em; }}
        table {{ border-collapse: collapse; width: 100%; margin: 1em 0; }}
        th, td {{ border: 1px solid #ddd; padding: 8px 12px; text-align: center; }}
        th {{ background: #f5f5f5; }}
        .pass {{ background: #e6ffe6; }}
        .fail {{ background: #ffe6e6; }}
        details {{ margin: 0.5em 0; padding: 0.5em; border: 1px solid #eee; border-radius: 4px; }}
        summary {{ cursor: pointer; padding: 0.3em; }}
        summary.pass {{ color: #2a7a2a; }}
        summary.fail {{ color: #a33; }}
        .detail-content {{ padding: 0.5em 1em; }}
        .error {{ color: #a33; font-style: italic; }}
        .meta {{ color: #666; font-size: 0.9em; }}
    </style>
</head>
<body>
    <h1>{html.escape(summary['comparison_name'])}</h1>
    <p class="meta">{html.escape(summary.get('description', ''))}</p>
    <p class="meta">Generated: {summary['generated_at']}</p>
    <h2>Score Matrix</h2>
    <table>
        <thead>
            <tr>
                <th>Model</th>
                {task_headers}
                <th>Avg Score</th>
                <th>Avg Steps</th>
                <th>Avg Time</th>
            </tr>
        </thead>
        <tbody>
            {rows_html}
        </tbody>
    </table>
    <h2>Per-Run Details</h2>
    {details_html}
</body>
</html>"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report_html)
    logger.info("HTML report written to %s", output_path)
    return output_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Systematic model comparison for WAA tasks",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", required=True, help="Path to comparison YAML config file")
    parser.add_argument("--server-url", default=None, help="Override server URL from config")
    parser.add_argument("--resume", action="store_true", help="Skip already-completed (model, task) pairs")
    # Infrastructure flags
    parser.add_argument("--manage-vm", action="store_true", help="Start/stop WAA VM automatically")
    parser.add_argument("--vm-name", default="waa-pool-00", help="VM name for --manage-vm")
    parser.add_argument("--resource-group", default="openadapt-agents", help="Azure resource group")
    parser.add_argument("--setup-tunnels", action="store_true", help="Set up SSH tunnels to the VM")
    parser.add_argument("--no-deallocate", action="store_true", help="Do not deallocate VM after comparison")
    # SGLang / local model flags
    parser.add_argument(
        "--gpu-host", default=None,
        help="SSH target for SGLang model serving (e.g. 'user@gpu-server'). "
             "Required for provider='sglang' models. If omitted, sglang models are skipped.",
    )
    parser.add_argument("--ssh-key", default=None, help="SSH private key for --gpu-host")

    args = parser.parse_args()
    config = load_config(args.config)
    logger.info(
        "Loaded comparison config: %s (%d models, %d tasks)",
        config.name, len(config.models), len(config.tasks),
    )
    server_url = args.server_url or config.server_url
    vm_ip = None

    try:
        if args.manage_vm:
            logger.info("Step 0: Starting VM %s...", args.vm_name)
            vm_ip = _manage_vm_lifecycle(args.vm_name, args.resource_group)
            if not vm_ip:
                logger.error("Failed to start VM")
                return 1

        if args.setup_tunnels:
            if not vm_ip:
                from scripts.run_correction_flywheel import get_vm_ip
                vm_ip = get_vm_ip(args.vm_name, args.resource_group)
                if not vm_ip:
                    logger.error("Cannot set up tunnels: VM IP not found for %s", args.vm_name)
                    return 1
            logger.info("Step 1: Setting up SSH tunnels to %s...", vm_ip)
            _setup_tunnels(vm_ip)
            if not _wait_for_waa(server_url):
                logger.error("WAA server not ready")
                return 1

        logger.info("Step 2: Running comparison...")
        run_start = time.time()
        results = run_comparison(
            config,
            server_url_override=server_url,
            resume=args.resume,
            gpu_host=args.gpu_host,
            ssh_key=args.ssh_key,
        )
        total_elapsed = time.time() - run_start

        if not results:
            logger.warning("No results collected")
            return 1

        logger.info("Step 3: Generating outputs...")
        output_dir = Path(config.output_dir)
        summary = build_summary(results, config)
        summary_path = output_dir / "summary.json"
        with open(summary_path, "w") as f:
            json.dump(summary, f, indent=2, default=str)
        logger.info("Summary JSON: %s", summary_path)
        report_path = output_dir / "comparison_report.html"
        generate_html_report(summary, results, report_path)
        print_summary_table(summary)
        print(f"\nTotal time: {total_elapsed / 60:.1f} min")
        print(f"Results:    {output_dir / 'results.jsonl'}")
        print(f"Summary:    {summary_path}")
        print(f"Report:     {report_path}")
        return 0

    finally:
        if args.manage_vm and not args.no_deallocate:
            from scripts.run_correction_flywheel import deallocate_vm
            logger.info("Cleanup: deallocating VM %s...", args.vm_name)
            deallocate_vm(args.vm_name, args.resource_group)


if __name__ == "__main__":
    sys.exit(main())

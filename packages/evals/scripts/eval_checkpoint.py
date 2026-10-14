#!/usr/bin/env python3
"""Evaluate a GRPO checkpoint (LoRA adapter) against WAA tasks.

Runs a local VLM (with optional LoRA checkpoint) against WAA tasks and
records scores, screenshots, and actions for before/after comparison.

Usage:
    # Evaluate checkpoint against notepad task
    python scripts/eval_checkpoint.py \
        --base-model Qwen/Qwen2.5-VL-7B-Instruct \
        --checkpoint path/to/step_10/ \
        --task-dir example_tasks \
        --server-url http://localhost:5001 \
        --max-steps 10 \
        --output eval_results/

    # Compare base model vs checkpoint
    python scripts/eval_checkpoint.py \
        --base-model Qwen/Qwen2.5-VL-7B-Instruct \
        --task-dir example_tasks \
        --server-url http://localhost:5001 \
        --output eval_results/baseline/

    python scripts/eval_checkpoint.py \
        --base-model Qwen/Qwen2.5-VL-7B-Instruct \
        --checkpoint path/to/step_10/ \
        --task-dir example_tasks \
        --server-url http://localhost:5001 \
        --output eval_results/step_10/

    # Then compare with trace analyzer
    python -m openadapt_evals.analysis eval_results/baseline/ \
        --compare eval_results/step_10/

Prerequisites:
    - WAA VM running with SSH tunnel (port 5001 -> VM port 5000)
    - GPU with sufficient VRAM (A10G 24GB for 7B + LoRA 4-bit)
    - pip install transformers peft bitsandbytes accelerate pillow
"""

from __future__ import annotations

import argparse
import io
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
logger = logging.getLogger("eval_checkpoint")

# Graceful shutdown
_shutdown_requested = False


def _signal_handler(signum, frame):
    global _shutdown_requested
    if _shutdown_requested:
        logger.warning("Second interrupt received, forcing exit")
        sys.exit(1)
    _shutdown_requested = True
    logger.warning("Shutdown requested, finishing current task...")


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# ---------------------------------------------------------------------------
# Model loading (matches GRPO trainer pattern from openadapt-ml)
# ---------------------------------------------------------------------------

# Import the SYSTEM_PROMPT and action parsing from openadapt-ml trainer
# to ensure prompt/parsing parity between training and evaluation.
try:
    from openadapt_ml.training.grpo.trainer import (
        _build_agent_messages,
        _parse_vlm_output_to_action,
    )

    _HAS_OPENADAPT_ML = True
except ImportError:
    _HAS_OPENADAPT_ML = False

# Fallback system prompt if openadapt-ml is not installed
_FALLBACK_SYSTEM_PROMPT = (
    "You are a GUI automation agent. Given a screenshot and a user goal, "
    "predict the single next action.\n\n"
    "COORDINATE SYSTEM:\n"
    "- x=0.0 is the LEFT edge, x=1.0 is the RIGHT edge\n"
    "- y=0.0 is the TOP edge, y=1.0 is the BOTTOM edge\n"
    "- To click the CENTER of an element, estimate its center position "
    "as a fraction of screen width/height\n"
    "- Example: An element in the middle of the screen would be "
    "approximately x=0.5, y=0.5\n\n"
    "ALLOWED ACTIONS (use exactly this format):\n"
    "- CLICK(x=0.XX, y=0.XX)  -> click at normalized coordinates\n"
    '- TYPE(text="...")     -> type text into the currently focused field\n'
    "- WAIT()                 -> wait for UI to update\n"
    "- DONE()                 -> task is complete\n\n"
    "RESPONSE FORMAT (required):\n"
    "Thought: [Brief reasoning: what element to interact with and why]\n"
    "Action: [Exactly one action, e.g., CLICK(x=0.35, y=0.42)]\n\n"
)

DEFAULT_SCREEN_SIZE = (1920, 1080)


def _fallback_build_agent_messages(
    instruction: str, *, include_image: bool = False
) -> list[dict]:
    """Fallback prompt builder when openadapt-ml is not installed."""
    text_content = (
        f"Goal: {instruction}\n\n"
        "Look at the screenshot and determine the NEXT action.\n\n"
        'Action: [CLICK(x=..., y=...) or TYPE(text="...") or WAIT() or DONE()]'
    )
    if include_image:
        user_content = [
            {"type": "image"},
            {"type": "text", "text": text_content},
        ]
    else:
        user_content = text_content
    return [
        {"role": "system", "content": _FALLBACK_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def _fallback_parse_vlm_output(
    text: str,
    screen_size: tuple[int, int] = DEFAULT_SCREEN_SIZE,
) -> Any:
    """Fallback action parser when openadapt-ml is not installed."""
    import re

    from openadapt_evals.adapters.base import BenchmarkAction

    text = text.strip()
    width, height = screen_size

    # CLICK(x=..., y=...)
    m = re.search(r"CLICK\(x=(-?[\d.]+),\s*y=(-?[\d.]+)\)", text, re.IGNORECASE)
    if m:
        x_frac = max(0.0, min(1.0, float(m.group(1))))
        y_frac = max(0.0, min(1.0, float(m.group(2))))
        return BenchmarkAction(
            type="click", x=int(x_frac * width), y=int(y_frac * height)
        )

    # TYPE(text="..." or '...')
    m = re.search(
        r"""TYPE\(text=["']([^"'\\]*(?:\\.[^"'\\]*)*)["']\)""", text, re.IGNORECASE
    )
    if m:
        typed_text = (
            m.group(1).replace("\\\\", "\\").replace('\\"', '"').replace("\\'", "'")
        )
        return BenchmarkAction(type="type", text=typed_text)

    if re.search(r"\bWAIT\s*\(\s*\)", text, re.IGNORECASE):
        return BenchmarkAction(type="wait")

    if re.search(r"\bDONE\s*\(\s*\)", text, re.IGNORECASE):
        return BenchmarkAction(type="done")

    logger.warning("Could not parse VLM output: %s. Defaulting to DONE.", text)
    return BenchmarkAction(type="done")


def build_agent_messages(instruction: str, *, include_image: bool = False):
    """Build chat messages using openadapt-ml or fallback."""
    if _HAS_OPENADAPT_ML:
        return _build_agent_messages(instruction, include_image=include_image)
    return _fallback_build_agent_messages(instruction, include_image=include_image)


def parse_vlm_output(text: str, screen_size=DEFAULT_SCREEN_SIZE):
    """Parse VLM output using openadapt-ml or fallback."""
    if _HAS_OPENADAPT_ML:
        return _parse_vlm_output_to_action(text, screen_size=screen_size)
    return _fallback_parse_vlm_output(text, screen_size=screen_size)


def load_model_and_processor(
    base_model: str,
    checkpoint: str | None = None,
    quantize_4bit: bool = False,
):
    """Load VLM with optional LoRA adapter.

    Uses the same loading pattern as the GRPO trainer:
    AutoModelForImageTextToText + AutoProcessor + PEFT.

    Args:
        base_model: HuggingFace model name.
        checkpoint: Path to PEFT/LoRA adapter directory (optional).
        quantize_4bit: Use 4-bit quantization via bitsandbytes.

    Returns:
        (model, processor) tuple.
    """
    import torch
    from transformers import AutoProcessor

    try:
        from transformers import AutoModelForImageTextToText as AutoVLM
    except ImportError:
        from transformers import AutoModelForVision2Seq as AutoVLM

    logger.info("Loading base model: %s", base_model)

    load_kwargs: dict[str, Any] = {
        "torch_dtype": torch.bfloat16,
        "device_map": "auto",
    }

    if quantize_4bit:
        from transformers import BitsAndBytesConfig

        load_kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_quant_type="nf4",
        )
        logger.info("4-bit quantization enabled")

    processor = AutoProcessor.from_pretrained(base_model)
    model = AutoVLM.from_pretrained(base_model, **load_kwargs)

    if checkpoint:
        from peft import PeftModel

        logger.info("Loading LoRA adapter from: %s", checkpoint)
        model = PeftModel.from_pretrained(
            model,
            checkpoint,
            is_trainable=False,
        )
        model.eval()
        logger.info("LoRA adapter loaded and set to eval mode")
    else:
        model.eval()
        logger.info("Base model loaded (no checkpoint)")

    return model, processor


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------


def generate_action(
    model,
    processor,
    screenshot_bytes: bytes,
    instruction: str,
    screen_size: tuple[int, int] = DEFAULT_SCREEN_SIZE,
    temperature: float = 0.0,
):
    """Generate next action from screenshot + instruction.

    Args:
        model: HuggingFace VLM (with or without LoRA).
        processor: HuggingFace processor.
        screenshot_bytes: PNG screenshot bytes.
        instruction: Task instruction text.
        screen_size: (width, height) for coordinate scaling.
        temperature: Sampling temperature (0.0 = greedy).

    Returns:
        (action: BenchmarkAction, raw_text: str) tuple.
    """
    import torch
    from PIL import Image

    image = Image.open(io.BytesIO(screenshot_bytes)).convert("RGB")
    messages = build_agent_messages(instruction, include_image=True)

    if hasattr(processor, "apply_chat_template"):
        text_input = processor.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
    else:
        text_input = messages[-1]["content"]

    inputs = processor(text=[text_input], images=[image], return_tensors="pt")
    inputs = {k: v.to(model.device) for k, v in inputs.items()}

    gen_kwargs = {"max_new_tokens": 100}
    if temperature > 0:
        gen_kwargs["temperature"] = temperature
        gen_kwargs["do_sample"] = True
    else:
        gen_kwargs["do_sample"] = False

    with torch.no_grad():
        outputs = model.generate(**inputs, **gen_kwargs)

    decoded = processor.decode(
        outputs[0][inputs["input_ids"].shape[1]:],
        skip_special_tokens=True,
    )

    action = parse_vlm_output(decoded, screen_size=screen_size)
    return action, decoded


# ---------------------------------------------------------------------------
# JSONL helpers (same pattern as run_full_eval.py)
# ---------------------------------------------------------------------------


def _results_path(output_dir: str) -> Path:
    """Return the output JSONL path within the output directory."""
    p = Path(output_dir) / "results.jsonl"
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
                if record.get("_meta"):
                    continue
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
# Server health check
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
) -> bool:
    """Wait for WAA server with exponential backoff."""
    for attempt in range(max_retries):
        if check_server_health(server_url):
            if attempt > 0:
                logger.info("Server reachable (attempt %d/%d)", attempt + 1, max_retries)
            return True
        delay = min(base_delay * (2 ** attempt), 60.0)
        logger.warning(
            "Server unreachable (attempt %d/%d), retrying in %.0fs...",
            attempt + 1, max_retries, delay,
        )
        time.sleep(delay)
    return False


# ---------------------------------------------------------------------------
# Single task execution
# ---------------------------------------------------------------------------


def run_single_task(
    task_config,
    model,
    processor,
    server_url: str,
    max_steps: int,
    save_screenshots: bool,
    screenshots_dir: Path | None,
    screen_size: tuple[int, int] = DEFAULT_SCREEN_SIZE,
    temperature: float = 0.0,
) -> dict:
    """Run a single task with local VLM and return a result dict.

    Never raises -- all errors are caught and returned in the result.
    """
    from openadapt_evals.adapters.rl_env import ResetConfig, RLEnvironment
    from openadapt_evals.adapters.waa.live import WAALiveAdapter, WAALiveConfig

    start_time = time.time()
    task_id = task_config.id
    result: dict[str, Any] = {
        "task_id": task_id,
        "task_name": task_config.name,
        "started_at": datetime.now().isoformat(),
        "score": 0.0,
        "success": False,
        "steps": 0,
        "actions": [],
        "error": None,
        "error_type": None,
    }

    try:
        adapter = WAALiveAdapter(WAALiveConfig(server_url=server_url))
        env = RLEnvironment(adapter, task_config=task_config)

        obs = env.reset(config=ResetConfig(task_id=task_id))

        # Save reset screenshot
        task_screenshot_dir = None
        if save_screenshots and screenshots_dir:
            safe_name = task_id.replace("/", "_")[:40]
            task_screenshot_dir = screenshots_dir / safe_name
            task_screenshot_dir.mkdir(parents=True, exist_ok=True)
            if obs.screenshot:
                (task_screenshot_dir / "step_00_reset.png").write_bytes(obs.screenshot)

        instruction = task_config.name

        for step in range(max_steps):
            if _shutdown_requested:
                result["error"] = "shutdown_requested"
                result["error_type"] = "interrupted"
                break

            if not obs.screenshot:
                logger.warning("Task %s: no screenshot at step %d", task_id[:12], step + 1)
                break

            # Generate action from model
            action, raw_text = generate_action(
                model, processor, obs.screenshot, instruction,
                screen_size=screen_size, temperature=temperature,
            )

            logger.info(
                "Task %s step %d: %s (raw: %s)",
                task_id[:12], step + 1, action.type, raw_text[:80],
            )
            result["actions"].append({
                "step": step + 1,
                "type": action.type,
                "x": action.x,
                "y": action.y,
                "text": action.text,
                "raw_vlm_output": raw_text,
            })

            if action.type == "done":
                logger.info("Task %s: model signaled DONE at step %d", task_id[:12], step + 1)
                break

            # Execute action against WAA
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

            if task_screenshot_dir and obs.screenshot:
                (task_screenshot_dir / f"step_{step + 1:02d}.png").write_bytes(
                    obs.screenshot
                )

            if step_result.done:
                logger.info("Task %s: env signaled done at step %d", task_id[:12], step + 1)
                break

        # Evaluate
        result["steps"] = env.step_count

        if task_config.milestones:
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
        logger.error("Task %s failed: %s", task_id[:12], e)

    elapsed = time.time() - start_time
    result["elapsed_seconds"] = round(elapsed, 2)
    result["finished_at"] = datetime.now().isoformat()
    return result


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def print_summary(results: list[dict], total_elapsed: float, meta: dict) -> None:
    """Print a summary table of all results."""
    if not results:
        print("\nNo results to summarize.")
        return

    total = len(results)
    successes = sum(1 for r in results if r.get("success"))
    scores = [r.get("score", 0.0) for r in results]
    avg_score = sum(scores) / total if total else 0.0
    total_steps = sum(r.get("steps", 0) for r in results)
    avg_steps = total_steps / total if total else 0.0
    task_times = [r.get("elapsed_seconds", 0.0) for r in results]
    avg_time = sum(task_times) / total if total else 0.0

    infra_errors = sum(1 for r in results if r.get("error_type") == "infrastructure")
    non_infra = [r for r in results if r.get("error_type") != "infrastructure"]
    non_infra_success = sum(1 for r in non_infra if r.get("success"))
    adj_rate = non_infra_success / len(non_infra) if non_infra else 0.0

    print("\n" + "=" * 70)
    print("CHECKPOINT EVALUATION SUMMARY")
    print("=" * 70)
    print(f"  Model:              {meta.get('base_model', '?')}")
    if meta.get("checkpoint"):
        print(f"  Checkpoint:         {meta.get('checkpoint')}")
    else:
        print("  Checkpoint:         (baseline -- no adapter)")
    print(f"  Total tasks:        {total}")
    print(f"  Passed:             {successes} ({successes / total:.1%})")
    print(f"  Failed:             {total - successes}")
    print(f"  Avg score:          {avg_score:.3f}")
    print(f"  Avg steps:          {avg_steps:.1f}")
    print(f"  Avg task time:      {avg_time:.1f}s")
    print(f"  Total time:         {total_elapsed / 60:.1f} min")
    if infra_errors:
        print(f"  Infra errors:       {infra_errors}")
        print(f"  Adj success rate:   {adj_rate:.1%} (excluding infra)")

    # Per-task results
    print()
    print(f"  {'Task':>30s}  {'Score':>6s}  {'Steps':>5s}  {'Time':>7s}  Status")
    print("  " + "-" * 65)
    for r in results:
        name = r.get("task_name", r["task_id"])
        if len(name) > 28:
            name = name[:26] + ".."
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
        print(f"  {name:>30s}  {score:6.2f}  {steps:5d}  {t:6.1f}s  {status}")
    print("=" * 70)


def write_summary_json(output_dir: Path, results: list[dict], meta: dict) -> Path:
    """Write a summary JSON file for programmatic comparison."""
    total = len(results)
    successes = sum(1 for r in results if r.get("success"))
    scores = [r.get("score", 0.0) for r in results]

    summary = {
        **meta,
        "total_tasks": total,
        "passed": successes,
        "pass_rate": successes / total if total else 0.0,
        "avg_score": sum(scores) / total if total else 0.0,
        "avg_steps": sum(r.get("steps", 0) for r in results) / total if total else 0.0,
        "per_task": {
            r["task_id"]: {
                "score": r.get("score", 0.0),
                "success": r.get("success", False),
                "steps": r.get("steps", 0),
                "elapsed_seconds": r.get("elapsed_seconds", 0.0),
            }
            for r in results
        },
    }

    path = output_dir / "summary.json"
    with open(path, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    return path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate a GRPO checkpoint (LoRA adapter) against WAA tasks",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Model
    parser.add_argument(
        "--base-model",
        default="Qwen/Qwen2.5-VL-7B-Instruct",
        help="HuggingFace model name (default: Qwen/Qwen2.5-VL-7B-Instruct)",
    )
    parser.add_argument(
        "--checkpoint",
        default=None,
        help="Path to PEFT/LoRA adapter directory (optional, omit for baseline)",
    )
    parser.add_argument(
        "--quantize-4bit",
        action="store_true",
        help="Use 4-bit quantization via bitsandbytes",
    )

    # Tasks
    parser.add_argument(
        "--task-dir",
        default=None,
        help="Directory of TaskConfig YAMLs/JSONs (default: example_tasks/)",
    )
    parser.add_argument(
        "--task-ids",
        default=None,
        help="Comma-separated task IDs to evaluate (default: all from task-dir)",
    )

    # Server
    parser.add_argument(
        "--server-url",
        default="http://localhost:5001",
        help="WAA server URL (default: http://localhost:5001)",
    )

    # Execution
    parser.add_argument("--max-steps", type=int, default=10)
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help="Sampling temperature (0.0 = greedy, default: 0.0)",
    )
    parser.add_argument(
        "--screen-size",
        default="1920x1080",
        help="Screen size WxH for coordinate scaling (default: 1920x1080)",
    )

    # Output
    parser.add_argument(
        "--output", "-o",
        required=True,
        help="Output directory for results (JSONL, screenshots, summary)",
    )
    parser.add_argument(
        "--save-screenshots",
        action="store_true",
        default=True,
        help="Save screenshots per step (default: True)",
    )
    parser.add_argument(
        "--no-screenshots",
        action="store_true",
        help="Disable screenshot saving",
    )

    # Resume
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip already-completed tasks in the output file",
    )

    args = parser.parse_args()

    # Parse screen size
    try:
        w, h = args.screen_size.lower().split("x")
        screen_size = (int(w), int(h))
    except ValueError:
        parser.error(f"Invalid --screen-size format: {args.screen_size}. Use WxH, e.g. 1920x1080")
        return 1  # unreachable but keeps type checker happy

    # Handle screenshot flag
    save_screenshots = args.save_screenshots and not args.no_screenshots

    # -------------------------------------------------------------------
    # Load task configs
    # -------------------------------------------------------------------
    from openadapt_evals.task_config import TaskConfig

    task_dir = args.task_dir
    if not task_dir:
        # Default to example_tasks/ relative to the repo root
        script_dir = Path(__file__).resolve().parent
        task_dir = str(script_dir.parent / "example_tasks")

    task_dir_path = Path(task_dir)
    if not task_dir_path.is_dir():
        logger.error("Task directory not found: %s", task_dir)
        return 1

    all_task_configs = TaskConfig.from_dir(task_dir)
    if not all_task_configs:
        logger.error("No task configs found in %s", task_dir)
        return 1

    logger.info("Loaded %d task configs from %s", len(all_task_configs), task_dir)

    # Filter by --task-ids if provided
    if args.task_ids:
        selected_ids = {t.strip() for t in args.task_ids.split(",")}
        all_task_configs = [tc for tc in all_task_configs if tc.id in selected_ids]
        if not all_task_configs:
            logger.error("No matching task configs for --task-ids: %s", args.task_ids)
            return 1
        logger.info("Filtered to %d tasks matching --task-ids", len(all_task_configs))

    # -------------------------------------------------------------------
    # Setup output
    # -------------------------------------------------------------------
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    results_path = _results_path(args.output)

    screenshots_dir = None
    if save_screenshots:
        screenshots_dir = output_dir / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)

    # Resume: skip completed tasks
    completed_ids: set[str] = set()
    if args.resume:
        completed_ids = _load_completed_task_ids(results_path)
        if completed_ids:
            logger.info(
                "Resuming: %d tasks already completed",
                len(completed_ids),
            )

    remaining = [tc for tc in all_task_configs if tc.id not in completed_ids]
    if not remaining:
        logger.info("All tasks already completed!")
        return 0

    # -------------------------------------------------------------------
    # Check server health
    # -------------------------------------------------------------------
    logger.info("Checking WAA server at %s...", args.server_url)
    if not wait_for_server(args.server_url):
        logger.error(
            "Cannot reach server at %s. "
            "Ensure SSH tunnel is active and WAA is running.",
            args.server_url,
        )
        return 1

    # -------------------------------------------------------------------
    # Load model
    # -------------------------------------------------------------------
    logger.info("Loading model...")
    model, processor = load_model_and_processor(
        base_model=args.base_model,
        checkpoint=args.checkpoint,
        quantize_4bit=args.quantize_4bit,
    )

    # -------------------------------------------------------------------
    # Write run metadata
    # -------------------------------------------------------------------
    meta = {
        "base_model": args.base_model,
        "checkpoint": args.checkpoint,
        "quantize_4bit": args.quantize_4bit,
        "server_url": args.server_url,
        "max_steps": args.max_steps,
        "temperature": args.temperature,
        "screen_size": list(screen_size),
        "task_dir": task_dir,
        "run_started": datetime.now().isoformat(),
        "total_tasks": len(all_task_configs),
        "remaining_tasks": len(remaining),
        "resumed": args.resume,
    }
    _append_result(results_path, {"_meta": True, **meta})

    # -------------------------------------------------------------------
    # Main evaluation loop
    # -------------------------------------------------------------------
    total_tasks = len(remaining)
    all_results: list[dict] = []
    run_start = time.time()

    logger.info(
        "Starting evaluation: %d tasks, max %d steps each",
        total_tasks, args.max_steps,
    )

    for i, task_config in enumerate(remaining):
        if _shutdown_requested:
            logger.warning("Shutdown requested, stopping after %d/%d tasks", i, total_tasks)
            break

        # Progress
        elapsed = time.time() - run_start
        if i > 0 and elapsed > 0:
            rate = elapsed / i
            eta_str = f"{rate * (total_tasks - i) / 60:.1f}m remaining"
        else:
            eta_str = "estimating..."

        logger.info(
            "=== Task %d/%d: %s (%s) ===",
            i + 1, total_tasks, task_config.name[:40], eta_str,
        )

        # Health check
        if not check_server_health(args.server_url):
            logger.warning("Server unreachable, attempting reconnect...")
            if not wait_for_server(args.server_url):
                result = {
                    "task_id": task_config.id,
                    "task_name": task_config.name,
                    "score": 0.0,
                    "success": False,
                    "steps": 0,
                    "actions": [],
                    "error": "Server unreachable after retries",
                    "error_type": "infrastructure",
                    "elapsed_seconds": 0.0,
                    "finished_at": datetime.now().isoformat(),
                }
                _append_result(results_path, result)
                all_results.append(result)
                continue

        # Run the task
        result = run_single_task(
            task_config=task_config,
            model=model,
            processor=processor,
            server_url=args.server_url,
            max_steps=args.max_steps,
            save_screenshots=save_screenshots,
            screenshots_dir=screenshots_dir,
            screen_size=screen_size,
            temperature=args.temperature,
        )

        # Save immediately
        _append_result(results_path, result)
        all_results.append(result)

        # Progress report
        status = "PASS" if result.get("success") else "FAIL"
        logger.info(
            "Task %s: %s (score=%.2f, steps=%d, time=%.1fs)",
            task_config.id[:12], status,
            result.get("score", 0.0),
            result.get("steps", 0),
            result.get("elapsed_seconds", 0.0),
        )

    total_elapsed = time.time() - run_start

    # Include previously completed results if resuming
    if args.resume and completed_ids:
        prev_results = []
        with open(results_path, "r") as f:
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

    # Print and save summary
    print_summary(all_results, total_elapsed, meta)
    summary_path = write_summary_json(output_dir, all_results, meta)

    print(f"\nResults saved to: {results_path}")
    print(f"Summary saved to: {summary_path}")
    if screenshots_dir:
        print(f"Screenshots saved to: {screenshots_dir}")

    if _shutdown_requested:
        print("\nRun was interrupted. Resume with:")
        print("  python scripts/eval_checkpoint.py --resume \\")
        print(f"    --base-model {args.base_model} \\")
        if args.checkpoint:
            print(f"    --checkpoint {args.checkpoint} \\")
        print(f"    --task-dir {task_dir} \\")
        print(f"    --server-url {args.server_url} \\")
        print(f"    --output {args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

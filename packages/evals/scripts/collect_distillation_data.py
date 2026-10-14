#!/usr/bin/env python3
"""Collect distillation data from a frontier model (teacher) for SFT training.

Runs a frontier model (GPT-5.4, Claude, etc.) as a unified desktop agent on
WAA tasks, saving every successful (screenshot, action) trajectory as
SFT-ready training data via PlannerTrajectoryLogger.

The output is a directory of JSONL + screenshot PNGs that can be directly
consumed by ``finetune_distilled.py`` for LoRA fine-tuning of a smaller
student model (e.g., Qwen3.5-9B).

This is the first step of the distillation pipeline described in
``private/research_superhuman_desktop_agent_2026_03_20.md`` Section 8.3
(Phase 1: Distillation SFT).

Uses WAADirect for reliable task setup instead of the adapter layer.
Task configs are loaded from local YAML/JSON files via --task-dir,
bypassing WAA's built-in task ID lookup.

Usage:
    # Collect trajectories from GPT-5.4 (default teacher)
    python scripts/collect_distillation_data.py \\
        --task-dir tasks/ \\
        --server-url http://localhost:5001

    # Collect from Claude with cost-limited testing
    python scripts/collect_distillation_data.py \\
        --task-dir tasks/ \\
        --model claude-sonnet-4-6-20260210 \\
        --provider anthropic \\
        --max-tasks 5 \\
        --server-url http://localhost:5001

    # Specific tasks only
    python scripts/collect_distillation_data.py \\
        --task-dir tasks/ \\
        --tasks change-font-arial,open-notepad \\
        --server-url http://localhost:5001

    # Resume a previous collection run
    python scripts/collect_distillation_data.py \\
        --task-dir tasks/ \\
        --server-url http://localhost:5001 \\
        --output-dir distillation_data/gpt54_run1 \\
        --resume

Prerequisites:
    - WAA VM running with SSH tunnel (port 5001 -> VM port 5000)
    - API key set for the teacher model (OPENAI_API_KEY, ANTHROPIC_API_KEY)
    - Task YAML/JSON files in --task-dir
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("collect_distillation_data")

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
# Token / cost estimation
# ---------------------------------------------------------------------------

# Approximate pricing per 1M tokens (input/output) as of March 2026.
_MODEL_PRICING: dict[str, tuple[float, float]] = {
    "gpt-5.4": (2.50, 15.00),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-5.2": (1.75, 14.00),
    "gpt-5": (1.25, 10.00),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "claude-opus-4-6-20260210": (5.00, 25.00),
    "claude-sonnet-4-6-20260210": (3.00, 15.00),
    "claude-sonnet-4-20250514": (3.00, 15.00),
    "claude-haiku-4-5-20241022": (1.00, 5.00),
}

# Approximate tokens per screenshot (high detail) + text overhead.
_TOKENS_PER_SCREENSHOT = 2500
_TOKENS_PER_STEP_TEXT_INPUT = 500
_TOKENS_PER_STEP_OUTPUT = 300


class CostTracker:
    """Track estimated API costs during distillation data collection."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_steps = 0
        self.total_episodes = 0
        self.successful_episodes = 0
        self.failed_episodes = 0

        pricing = _MODEL_PRICING.get(model)
        if pricing:
            self._input_price_per_m, self._output_price_per_m = pricing
        else:
            # Unknown model -- use a conservative default
            self._input_price_per_m = 3.00
            self._output_price_per_m = 15.00
            logger.warning(
                "No pricing data for model %s, using default $3/$15 per M tokens",
                model,
            )

    def record_step(
        self,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        """Record tokens used for a single step."""
        self.total_steps += 1
        if input_tokens is not None:
            self.total_input_tokens += input_tokens
        else:
            # Estimate: screenshot + text context with multi-turn accumulation
            step_in_episode = self.total_steps % 15  # rough estimate
            context_multiplier = 1 + step_in_episode * 0.3
            self.total_input_tokens += int(
                (_TOKENS_PER_SCREENSHOT + _TOKENS_PER_STEP_TEXT_INPUT)
                * context_multiplier
            )
        if output_tokens is not None:
            self.total_output_tokens += output_tokens
        else:
            self.total_output_tokens += _TOKENS_PER_STEP_OUTPUT

    def record_episode(self, success: bool) -> None:
        """Record episode outcome."""
        self.total_episodes += 1
        if success:
            self.successful_episodes += 1
        else:
            self.failed_episodes += 1

    @property
    def estimated_cost(self) -> float:
        """Estimated total cost in USD."""
        input_cost = (self.total_input_tokens / 1_000_000) * self._input_price_per_m
        output_cost = (self.total_output_tokens / 1_000_000) * self._output_price_per_m
        return input_cost + output_cost

    def summary(self) -> str:
        """Return a formatted summary string."""
        input_cost = (self.total_input_tokens / 1_000_000) * self._input_price_per_m
        output_cost = (self.total_output_tokens / 1_000_000) * self._output_price_per_m
        return (
            f"Cost Summary ({self.model}):\n"
            f"  Total episodes:     {self.total_episodes}\n"
            f"  Successful:         {self.successful_episodes}\n"
            f"  Failed (discarded): {self.failed_episodes}\n"
            f"  Total steps:        {self.total_steps}\n"
            f"  Input tokens:       {self.total_input_tokens:,}\n"
            f"  Output tokens:      {self.total_output_tokens:,}\n"
            f"  Input cost:         ${input_cost:.2f}\n"
            f"  Output cost:        ${output_cost:.2f}\n"
            f"  Total cost:         ${input_cost + output_cost:.2f}"
        )


# ---------------------------------------------------------------------------
# Teacher agent: Unified frontier model acting as desktop agent
# ---------------------------------------------------------------------------


class TeacherAgent:
    """Unified frontier model agent for distillation data collection.

    Uses the OpenAI-compatible API for GPT models and the Anthropic API
    for Claude models. The teacher acts as a unified planner+grounder,
    outputting structured actions with pixel coordinates directly.

    Args:
        model: Model API ID (e.g., ``"gpt-5.4"``, ``"claude-sonnet-4-6-20260210"``).
        provider: API provider (``"openai"`` or ``"anthropic"``).
        max_tokens: Maximum output tokens per step.
    """

    SYSTEM_PROMPT = (
        "You are an expert desktop automation agent. You are performing a task "
        "on a Windows desktop. Given a screenshot and task instruction, output "
        "the next action as a JSON object.\n\n"
        "Output format:\n"
        '{"decision": "COMMAND" | "DONE",\n'
        ' "action_type": "click" | "double_click" | "type" | "key" | "scroll",\n'
        ' "x": <fractional 0.0-1.0 horizontal coordinate>,\n'
        ' "y": <fractional 0.0-1.0 vertical coordinate>,\n'
        ' "text": "<text to type or key to press>",\n'
        ' "target_description": "<what element you are interacting with>",\n'
        ' "reasoning": "<brief explanation of why>"}\n\n'
        "Rules:\n"
        "- x and y are fractional coordinates (0.0 = left/top, 1.0 = right/bottom)\n"
        "- For type: text contains the text to type. Append \\n for Enter.\n"
        "- For key: text contains the key name (e.g., 'Enter', 'Ctrl+A').\n"
        "- For scroll: text is 'up' or 'down'. x,y indicate scroll position.\n"
        "- Output ONLY the JSON object, no other text.\n"
        "- Signal DONE when the task is complete."
    )

    def __init__(
        self,
        model: str = "gpt-5.4",
        provider: str = "openai",
        max_tokens: int = 2048,
    ) -> None:
        self.model = model
        self.provider = provider
        self.max_tokens = max_tokens
        self._conversation_history: list[dict[str, Any]] = []
        self._client: Any = None

    def _get_client(self):
        """Lazy-initialize API client."""
        if self._client is not None:
            return self._client

        if self.provider == "openai":
            from openai import OpenAI

            self._client = OpenAI()
        elif self.provider == "anthropic":
            import anthropic

            self._client = anthropic.Anthropic()
        else:
            raise ValueError(f"Unsupported provider: {self.provider}")

        return self._client

    def reset(self) -> None:
        """Reset conversation history for a new episode."""
        self._conversation_history = []

    def act(
        self,
        screenshot_bytes: bytes,
        task_instruction: str,
        action_history: list[str],
    ) -> tuple[dict[str, Any], int | None, int | None]:
        """Call the teacher model to get the next action.

        Args:
            screenshot_bytes: PNG screenshot bytes.
            task_instruction: Natural language task instruction.
            action_history: List of previous action strings.

        Returns:
            Tuple of (parsed_action_dict, input_tokens, output_tokens).
        """
        client = self._get_client()

        history_text = "\n".join(
            f"  {i + 1}. {a}" for i, a in enumerate(action_history[-10:])
        )
        if not history_text:
            history_text = "  (none yet)"

        user_text = (
            f"Task: {task_instruction}\n\n"
            f"Previous actions:\n{history_text}\n\n"
            "Look at the screenshot and output the next action as JSON."
        )

        screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

        if self.provider == "openai":
            return self._act_openai(client, user_text, screenshot_b64)
        elif self.provider == "anthropic":
            return self._act_anthropic(client, user_text, screenshot_b64)
        else:
            raise ValueError(f"Unsupported provider: {self.provider}")

    def _act_openai(
        self, client: Any, user_text: str, screenshot_b64: str
    ) -> tuple[dict[str, Any], int | None, int | None]:
        """Call OpenAI-compatible API."""
        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
        ]
        # Include conversation history for multi-turn context
        messages.extend(self._conversation_history)
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{screenshot_b64}",
                            "detail": "high",
                        },
                    },
                    {"type": "text", "text": user_text},
                ],
            }
        )

        # GPT-5.x and o-series models require max_completion_tokens;
        # older models use max_tokens.
        token_param = (
            {"max_completion_tokens": self.max_tokens}
            if self.model.startswith(("gpt-5", "o1", "o3", "o4"))
            else {"max_tokens": self.max_tokens}
        )

        response = client.chat.completions.create(
            model=self.model,
            messages=messages,
            **token_param,
            temperature=0.1,
        )

        content = response.choices[0].message.content or ""
        input_tokens = getattr(response.usage, "prompt_tokens", None)
        output_tokens = getattr(response.usage, "completion_tokens", None)

        # Save to conversation history (text only, not images, to control size)
        self._conversation_history.append(
            {"role": "user", "content": user_text}
        )
        self._conversation_history.append(
            {"role": "assistant", "content": content}
        )

        # Keep history manageable
        if len(self._conversation_history) > 20:
            self._conversation_history = self._conversation_history[-16:]

        return self._parse_action(content), input_tokens, output_tokens

    def _act_anthropic(
        self, client: Any, user_text: str, screenshot_b64: str
    ) -> tuple[dict[str, Any], int | None, int | None]:
        """Call Anthropic API."""
        messages = list(self._conversation_history)
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": screenshot_b64,
                        },
                    },
                    {"type": "text", "text": user_text},
                ],
            }
        )

        # Anthropic API always uses max_tokens (not max_completion_tokens).
        response = client.messages.create(
            model=self.model,
            system=self.SYSTEM_PROMPT,
            messages=messages,
            max_tokens=self.max_tokens,
            temperature=0.1,
        )

        content = ""
        for block in response.content:
            if hasattr(block, "text"):
                content += block.text

        input_tokens = getattr(response.usage, "input_tokens", None)
        output_tokens = getattr(response.usage, "output_tokens", None)

        # Save to conversation history (text only)
        self._conversation_history.append(
            {"role": "user", "content": user_text}
        )
        self._conversation_history.append(
            {"role": "assistant", "content": content}
        )

        if len(self._conversation_history) > 20:
            self._conversation_history = self._conversation_history[-16:]

        return self._parse_action(content), input_tokens, output_tokens

    @staticmethod
    def _parse_action(text: str) -> dict[str, Any]:
        """Parse the model's JSON output into an action dict."""
        import re

        text = text.strip()
        # Strip markdown code fences
        text = re.sub(r"```json\s*", "", text)
        text = re.sub(r"```\s*$", "", text)

        # Find JSON object
        match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
        if not match:
            logger.warning("No JSON found in teacher output: %s", text[:200])
            return {"decision": "DONE", "reasoning": "Failed to parse output"}

        try:
            data = json.loads(match.group())
        except json.JSONDecodeError:
            logger.warning("Invalid JSON from teacher: %s", match.group()[:200])
            return {"decision": "DONE", "reasoning": "Failed to parse JSON"}

        return data


# ---------------------------------------------------------------------------
# Episode runner (WAADirect-based)
# ---------------------------------------------------------------------------


def run_distillation_episode(
    task_config: Any,
    teacher: TeacherAgent,
    waa: Any,
    trajectory_logger: Any,
    cost_tracker: CostTracker,
    max_steps: int = 15,
    eval_model: str = "gpt-4.1-mini",
    screen_size: tuple[int, int] = (1920, 1080),
) -> tuple[bool, float]:
    """Run a single distillation episode using WAADirect.

    The teacher agent interacts with the WAA environment while
    PlannerTrajectoryLogger records every (screenshot, action) pair.
    At the end, milestones are evaluated via VLM screenshot checks.

    Args:
        task_config: TaskConfig instance with setup, instruction, milestones.
        teacher: TeacherAgent instance.
        waa: WAADirect instance.
        trajectory_logger: PlannerTrajectoryLogger instance.
        cost_tracker: CostTracker instance.
        max_steps: Maximum steps per episode.
        eval_model: VLM model for milestone evaluation.
        screen_size: Screen resolution for coordinate conversion.

    Returns:
        Tuple of (success, score).
    """
    from openadapt_evals.training.standalone.prompt import SimpleAction
    from openadapt_evals.training.standalone.reward import evaluate_milestones_screenshot

    task_id = task_config.id
    task_instruction = task_config.name
    episode_id = f"{task_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    teacher.reset()

    # Setup task environment via WAADirect (kills old processes, runs setup commands)
    try:
        waa_config = task_config.to_waa_config()
        waa.setup_task(waa_config)
    except Exception as e:
        logger.error("Failed to setup task %s: %s", task_id, e)
        cost_tracker.record_episode(success=False)
        return False, 0.0

    # Take initial screenshot
    try:
        screenshot = waa.screenshot()
    except Exception as e:
        logger.error("Failed to take initial screenshot for task %s: %s", task_id, e)
        cost_tracker.record_episode(success=False)
        return False, 0.0

    action_history: list[str] = []
    screen_w, screen_h = screen_size

    for step in range(max_steps):
        if _shutdown_requested:
            logger.warning("Shutdown requested during episode %s", episode_id)
            break

        # Get teacher's action
        try:
            action_dict, input_tokens, output_tokens = teacher.act(
                screenshot, task_instruction, action_history
            )
        except Exception as e:
            logger.error("Teacher API call failed at step %d: %s", step, e)
            break

        cost_tracker.record_step(input_tokens, output_tokens)

        # Log the step via trajectory logger
        trajectory_logger.log_step(
            episode_id=episode_id,
            step_index=step,
            screenshot_bytes=screenshot,
            a11y_tree=None,
            task_instruction=task_instruction,
            action_history=list(action_history),
            planner_output=action_dict,
        )

        decision = action_dict.get("decision", "COMMAND").upper()
        if decision == "DONE":
            logger.info(
                "Teacher signaled DONE at step %d for task %s",
                step,
                task_id,
            )
            break

        # Convert teacher output to SimpleAction and execute via WAADirect
        action_type = action_dict.get("action_type", "click")
        x_frac = action_dict.get("x")
        y_frac = action_dict.get("y")
        text = action_dict.get("text", "")
        target = action_dict.get("target_description", "")

        # Build action description for history
        if action_type in ("click", "double_click"):
            action_str = f"{action_type.upper()}({target})"
            if x_frac is not None and y_frac is not None:
                action_str += f" at ({x_frac:.3f}, {y_frac:.3f})"
        elif action_type == "type":
            action_str = f"TYPE({text!r})"
        elif action_type == "key":
            action_str = f"KEY({text})"
        elif action_type == "scroll":
            action_str = f"SCROLL({text})"
        else:
            action_str = f"{action_type.upper()}()"

        action_history.append(action_str)

        # Convert fractional coordinates to absolute pixels
        abs_x: int | None = None
        abs_y: int | None = None
        if x_frac is not None and y_frac is not None:
            fx, fy = float(x_frac), float(y_frac)
            if 0 <= fx <= 1 and 0 <= fy <= 1:
                abs_x = int(fx * screen_w)
                abs_y = int(fy * screen_h)
            else:
                # Already absolute pixel coordinates
                abs_x = int(fx)
                abs_y = int(fy)

        # Build SimpleAction for WAADirect
        action = SimpleAction(
            type=action_type,
            x=abs_x,
            y=abs_y,
            text=text if action_type == "type" else None,
            key=text if action_type == "key" else None,
        )

        # Execute action via WAADirect
        try:
            waa.execute_action(action)
            time.sleep(0.5)
        except Exception as e:
            logger.error(
                "Action execution failed at step %d for task %s: %s",
                step,
                task_id,
                e,
            )
            break

        # Take next screenshot
        try:
            screenshot = waa.screenshot()
        except Exception as e:
            logger.error(
                "Screenshot failed at step %d for task %s: %s",
                step,
                task_id,
                e,
            )
            break

    # Evaluate: take a fresh screenshot and run milestone checks
    score = 0.0
    if getattr(task_config, "milestones", None):
        try:
            eval_screenshot = waa.screenshot()
            score = evaluate_milestones_screenshot(
                task_config, eval_screenshot, model=eval_model,
            )
        except Exception as e:
            logger.error("Milestone evaluation failed for task %s: %s", task_id, e)
            score = 0.0
    else:
        # No milestones defined -- teacher DONE signal counts as partial success
        if action_history and decision == "DONE":
            score = 0.5
            logger.info(
                "No milestones for task %s, using teacher DONE signal (score=0.5)",
                task_id,
            )

    success = score > 0
    cost_tracker.record_episode(success)

    # End episode in trajectory logger (with keep_failed=True, all are kept)
    trajectory_logger.end_episode(episode_id, reward=score)

    return success, score


# ---------------------------------------------------------------------------
# Resume support
# ---------------------------------------------------------------------------


def get_completed_tasks(output_dir: Path) -> set[str]:
    """Get task IDs that already have successful trajectories.

    Scans the JSONL file for episodes with episode_reward > 0 and
    extracts the task ID prefix from the episode_id.
    """
    completed = set()
    jsonl_path = output_dir / "trajectories.jsonl"
    if not jsonl_path.exists():
        return completed

    seen_episodes: dict[str, float] = {}
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                ep_id = record.get("episode_id", "")
                reward = record.get("episode_reward")
                if reward is not None and reward > 0:
                    seen_episodes[ep_id] = reward
            except json.JSONDecodeError:
                continue

    for ep_id in seen_episodes:
        # Episode ID format: {task_id}_{timestamp}
        # Task IDs contain hyphens, so split from the right on underscore
        parts = ep_id.rsplit("_", 2)
        if len(parts) >= 3:
            task_id = parts[0]
            completed.add(task_id)

    return completed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect distillation data from a frontier teacher model",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument(
        "--model",
        default="gpt-5.4",
        help="Teacher model API ID (default: gpt-5.4)",
    )
    parser.add_argument(
        "--provider",
        default="openai",
        choices=["openai", "anthropic"],
        help="API provider for the teacher model (default: openai)",
    )
    parser.add_argument(
        "--task-dir",
        required=True,
        help="Directory containing task YAML/JSON config files",
    )
    parser.add_argument(
        "--tasks",
        default=None,
        help=(
            "Comma-separated task IDs to filter from --task-dir "
            "(default: all tasks in task-dir)"
        ),
    )
    parser.add_argument(
        "--max-tasks",
        type=int,
        default=None,
        help="Limit number of tasks to process (for cost-limited testing)",
    )
    parser.add_argument(
        "--server-url",
        default="http://localhost:5001",
        help="WAA server URL (default: http://localhost:5001)",
    )
    parser.add_argument(
        "--output-dir",
        default="distillation_data",
        help="Directory to save trajectory data (default: distillation_data/)",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=15,
        help="Maximum steps per episode (default: 15)",
    )
    parser.add_argument(
        "--eval-model",
        default="gpt-4.1-mini",
        help="VLM model for milestone evaluation (default: gpt-4.1-mini)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip tasks that already have saved successful trajectories",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List tasks without running them",
    )

    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load task configs from --task-dir
    from openadapt_evals.task_config import TaskConfig

    task_dir = Path(args.task_dir)
    if not task_dir.exists():
        logger.error("Task directory not found: %s", task_dir)
        return 1

    all_task_configs = TaskConfig.from_dir(str(task_dir))
    if not all_task_configs:
        logger.error("No task configs found in %s", task_dir)
        return 1
    logger.info("Loaded %d task configs from %s", len(all_task_configs), task_dir)

    # Build lookup by task ID
    task_config_map: dict[str, Any] = {tc.id: tc for tc in all_task_configs}

    # Filter to specific tasks if requested
    if args.tasks:
        requested_ids = [t.strip() for t in args.tasks.split(",") if t.strip()]
        missing = [tid for tid in requested_ids if tid not in task_config_map]
        if missing:
            logger.warning("Tasks not found in --task-dir: %s", missing)
        task_ids = [tid for tid in requested_ids if tid in task_config_map]
    else:
        task_ids = list(task_config_map.keys())

    # Apply max-tasks limit
    if args.max_tasks is not None and args.max_tasks < len(task_ids):
        task_ids = task_ids[: args.max_tasks]
        logger.info("Limited to %d tasks (--max-tasks)", args.max_tasks)

    # Resume: skip completed tasks
    if args.resume:
        completed = get_completed_tasks(output_dir)
        if completed:
            before = len(task_ids)
            task_ids = [t for t in task_ids if t not in completed]
            logger.info(
                "Resume: %d tasks already completed, %d remaining",
                before - len(task_ids),
                len(task_ids),
            )

    # Dry run
    if args.dry_run:
        print(f"\nDry run: would collect distillation data for {len(task_ids)} tasks")
        print(f"Teacher model: {args.model} ({args.provider})")
        print(f"Server: {args.server_url}")
        print(f"Task dir: {task_dir}")
        print(f"Output: {output_dir}")
        print(f"Max steps: {args.max_steps}")
        print(f"Eval model: {args.eval_model}")
        print("\nTasks:")
        for i, tid in enumerate(task_ids, 1):
            tc = task_config_map[tid]
            print(f"  {i:3d}. {tid} - {tc.name[:60]}")
        return 0

    if not task_ids:
        logger.info("No tasks to process.")
        return 0

    # Initialize WAADirect and verify server health
    from openadapt_evals.training.standalone.waa_direct import WAADirect

    waa = WAADirect(server_url=args.server_url)
    if not waa.health_check():
        logger.error(
            "WAA server not reachable at %s. Ensure SSH tunnel is active.",
            args.server_url,
        )
        return 1

    # Initialize components
    from openadapt_evals.training.trajectory_logger import PlannerTrajectoryLogger

    trajectory_logger = PlannerTrajectoryLogger(output_dir=str(output_dir), keep_failed=True)
    teacher = TeacherAgent(model=args.model, provider=args.provider)
    cost_tracker = CostTracker(model=args.model)

    # Save run metadata
    meta = {
        "_meta": True,
        "run_started": datetime.now().isoformat(),
        "teacher_model": args.model,
        "provider": args.provider,
        "server_url": args.server_url,
        "task_dir": str(task_dir),
        "max_steps": args.max_steps,
        "eval_model": args.eval_model,
        "total_tasks": len(task_ids),
        "resumed": args.resume,
    }
    meta_path = output_dir / "run_metadata.json"
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    # Main collection loop
    total_tasks = len(task_ids)
    run_start = time.time()
    results: list[dict[str, Any]] = []

    logger.info(
        "Starting distillation data collection: %d tasks, teacher=%s",
        total_tasks,
        args.model,
    )

    for i, task_id in enumerate(task_ids):
        if _shutdown_requested:
            logger.warning(
                "Shutdown requested after %d/%d tasks", i, total_tasks
            )
            break

        # Progress
        elapsed = time.time() - run_start
        if i > 0 and elapsed > 0:
            rate = elapsed / i
            eta_seconds = rate * (total_tasks - i)
            eta_str = f"{eta_seconds / 60:.1f}m remaining"
        else:
            eta_str = "estimating..."

        tc = task_config_map[task_id]
        logger.info(
            "=== Task %d/%d [%s] %s (%s, est cost: $%.2f) ===",
            i + 1,
            total_tasks,
            task_id,
            tc.name[:40],
            eta_str,
            cost_tracker.estimated_cost,
        )

        task_start = time.time()
        success, score = run_distillation_episode(
            task_config=tc,
            teacher=teacher,
            waa=waa,
            trajectory_logger=trajectory_logger,
            cost_tracker=cost_tracker,
            max_steps=args.max_steps,
            eval_model=args.eval_model,
        )
        task_elapsed = time.time() - task_start

        status = "PASS" if success else "FAIL"
        logger.info(
            "Task %s: %s (score=%.2f, time=%.1fs)",
            task_id,
            status,
            score,
            task_elapsed,
        )

        results.append(
            {
                "task_id": task_id,
                "task_name": tc.name,
                "success": success,
                "score": score,
                "elapsed_seconds": round(task_elapsed, 2),
            }
        )

    # Summary
    total_elapsed = time.time() - run_start
    total = len(results)
    successes = sum(1 for r in results if r["success"])

    print("\n" + "=" * 70)
    print("DISTILLATION DATA COLLECTION SUMMARY")
    print("=" * 70)
    print(f"  Teacher model:      {args.model}")
    print(f"  Task dir:           {task_dir}")
    print(f"  Total tasks:        {total}")
    print(f"  Successful:         {successes} ({successes / total:.1%})" if total else "")
    print(f"  Failed (kept):      {total - successes}")
    print(f"  Total time:         {total_elapsed / 60:.1f} min")
    print(f"  Output dir:         {output_dir}")
    print()
    print(cost_tracker.summary())
    print("=" * 70)

    # Save summary
    summary_path = output_dir / "collection_summary.json"
    with open(summary_path, "w") as f:
        json.dump(
            {
                "teacher_model": args.model,
                "task_dir": str(task_dir),
                "total_tasks": total,
                "successful": successes,
                "failed": total - successes,
                "total_elapsed_minutes": round(total_elapsed / 60, 2),
                "estimated_cost_usd": round(cost_tracker.estimated_cost, 2),
                "total_input_tokens": cost_tracker.total_input_tokens,
                "total_output_tokens": cost_tracker.total_output_tokens,
                "total_steps": cost_tracker.total_steps,
                "results": results,
                "finished_at": datetime.now().isoformat(),
            },
            f,
            indent=2,
        )

    if _shutdown_requested and i < total_tasks:
        print("\nRun was interrupted. Resume with:")
        print(
            f"  python scripts/collect_distillation_data.py "
            f"--resume --output-dir {output_dir} "
            f"--task-dir {task_dir} "
            f"--server-url {args.server_url} "
            f"--model {args.model}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())

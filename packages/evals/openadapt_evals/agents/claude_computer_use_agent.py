"""Claude Computer Use agent for benchmark evaluation.

This module provides an agent that uses Anthropic's native computer_use tool
for GUI automation. Unlike ApiAgent which asks Claude to output action strings
and parses them via regex, this agent uses the structured tool_use/tool_result
protocol where Claude directly outputs typed action dicts.

Key advantages over ApiAgent:
- Claude was specifically trained for coordinate prediction via computer_use
- Structured action output (no regex parsing, no ambiguity)
- Multi-turn conversation maintained across steps
- Built-in screenshot handling via tool_result

Plan progress tracking:
- When a multi-level demo (with GOAL/PLAN/REFERENCE TRAJECTORY sections) is
  provided, the agent tracks which plan steps are complete.
- At each step, a dynamic plan progress summary replaces the static demo text.
- If Claude declares "done" prematurely (while plan steps remain), the agent
  overrides the done signal and injects a continuation prompt to keep going.

Usage:
    from openadapt_evals.agents import ClaudeComputerUseAgent

    agent = ClaudeComputerUseAgent()
    action = agent.act(observation, task)

    # With demo conditioning
    agent = ClaudeComputerUseAgent(demo="Step 1: Click Start menu...")

    # With multi-level demo (enables plan progress tracking)
    agent = ClaudeComputerUseAgent(demo=open("demo_multilevel.txt").read())
"""

from __future__ import annotations

import base64
import logging
import os
import re
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

from openadapt_evals.adapters.base import (
    BenchmarkAction,
    BenchmarkObservation,
    BenchmarkTask,
)
from openadapt_evals.agents.base import BenchmarkAgent

logger = logging.getLogger("openadapt_evals.agents.claude_cu")

# Load .env file if it exists
try:
    from dotenv import load_dotenv

    current_dir = Path.cwd()
    for path in [current_dir] + list(current_dir.parents):
        env_file = path / ".env"
        if env_file.exists():
            load_dotenv(env_file)
            break
except ImportError:
    pass

# Tool version and beta string for Opus 4.6 / Sonnet 4.6
COMPUTER_TOOL_TYPE = "computer_20251124"
COMPUTER_USE_BETA = "computer-use-2025-11-24"


def _parse_multilevel_demo(demo_text: str) -> dict | None:
    """Parse a multi-level demo into structured components.

    Extracts the GOAL, PLAN steps, and REFERENCE TRAJECTORY from a demo
    that follows the multi-level format. Returns None if the text does not
    match the expected format (i.e., does not contain GOAL:, PLAN:, and
    REFERENCE TRAJECTORY sections).

    Args:
        demo_text: The full demo text to parse.

    Returns:
        Dict with keys:
            - "goal": str - the goal text
            - "plan_steps": list[str] - ordered list of plan step descriptions
            - "trajectory": list[dict] - list of dicts with keys
              "step_num", "think", "action", "expect"
        Returns None if the demo is not in multi-level format.
    """
    if not demo_text:
        return None

    # Check for required sections
    has_goal = "GOAL:" in demo_text
    has_plan = "PLAN:" in demo_text
    has_traj = "REFERENCE TRAJECTORY" in demo_text

    if not (has_goal and has_plan and has_traj):
        return None

    # Extract GOAL
    goal_match = re.search(r"GOAL:\s*(.+?)(?:\n\n|\nPLAN:)", demo_text, re.DOTALL)
    goal = goal_match.group(1).strip() if goal_match else ""

    # Extract PLAN steps
    plan_match = re.search(
        r"PLAN:\s*\n(.*?)(?:\nREFERENCE TRAJECTORY|\Z)", demo_text, re.DOTALL
    )
    plan_steps: list[str] = []
    if plan_match:
        plan_text = plan_match.group(1)
        # Match numbered lines like "1. Do something"
        for step_match in re.finditer(r"^\d+\.\s*(.+)$", plan_text, re.MULTILINE):
            plan_steps.append(step_match.group(1).strip())

    # Extract REFERENCE TRAJECTORY steps
    traj_match = re.search(
        r"REFERENCE TRAJECTORY[^\n]*\n(.*)", demo_text, re.DOTALL
    )
    trajectory: list[dict] = []
    if traj_match:
        traj_text = traj_match.group(1)
        # Split on "Step N:" headers
        step_blocks = re.split(r"(?=^Step\s+\d+:)", traj_text, flags=re.MULTILINE)
        for block in step_blocks:
            block = block.strip()
            if not block:
                continue
            # Parse step number
            step_num_match = re.match(r"Step\s+(\d+):", block)
            if not step_num_match:
                continue
            step_num = int(step_num_match.group(1))

            # Parse Think/Action/Expect fields
            think_match = re.search(
                r"Think:\s*(.+?)(?=\n\s*Action:|\Z)", block, re.DOTALL
            )
            action_match = re.search(
                r"Action:\s*(.+?)(?=\n\s*Expect:|\Z)", block, re.DOTALL
            )
            expect_match = re.search(
                r"Expect:\s*(.+?)(?=\n\s*$|\nStep\s+\d+:|\Z)", block, re.DOTALL
            )

            trajectory.append({
                "step_num": step_num,
                "think": think_match.group(1).strip() if think_match else "",
                "action": action_match.group(1).strip() if action_match else "",
                "expect": expect_match.group(1).strip() if expect_match else "",
            })

    return {
        "goal": goal,
        "plan_steps": plan_steps,
        "trajectory": trajectory,
    }


def _build_plan_progress_text(
    goal: str,
    plan_steps: list[dict],
    trajectory: list[dict],
    step_count: int,
) -> str:
    """Build a dynamic plan progress summary for injection into messages.

    Args:
        goal: The task goal text.
        plan_steps: List of plan step dicts with "text", "status", "step_num".
        trajectory: List of trajectory step dicts with "step_num", "think",
            "action", "expect".
        step_count: Current agent step count (1-indexed).

    Returns:
        Formatted plan progress text.
    """
    total = len(plan_steps)

    done_steps = [s for s in plan_steps if s["status"] == "done"]
    in_progress = [s for s in plan_steps if s["status"] == "in_progress"]
    pending = [s for s in plan_steps if s["status"] == "pending"]

    # Determine current step (first in_progress, or first pending)
    current = in_progress[0] if in_progress else (pending[0] if pending else None)
    current_num = current["step_num"] if current else total

    lines = [
        f"GOAL: {goal}",
        "",
        f"PLAN PROGRESS (step {current_num}/{total}):",
    ]

    # Completed steps
    if done_steps:
        lines.append("  Completed:")
        for s in done_steps:
            lines.append(f"    [{s['step_num']}] {s['text']}")
    else:
        lines.append("  Completed: (none yet)")

    # Current step
    if current:
        lines.append(f"  Current: step {current['step_num']} - {current['text']}")
    else:
        lines.append("  Current: (all steps complete)")

    # Remaining steps
    remaining = [s for s in pending if current and s["step_num"] > current["step_num"]]
    if remaining:
        lines.append("  Remaining:")
        for s in remaining:
            lines.append(f"    [{s['step_num']}] {s['text']}")
    elif pending and not current:
        pass  # All done
    else:
        lines.append("  Remaining: (none)")

    # Current step detail from trajectory
    if current:
        traj_step = None
        for t in trajectory:
            if t["step_num"] == current["step_num"]:
                traj_step = t
                break
        if traj_step:
            lines.extend([
                "",
                "CURRENT STEP DETAIL:",
                f"  Think: {traj_step['think']}",
                f"  Action: {traj_step['action']}",
                f"  Expect: {traj_step['expect']}",
            ])

    lines.extend([
        "",
        "You MUST complete ALL remaining steps before declaring the task done.",
        "Do NOT declare done until the last step is verified complete.",
    ])

    return "\n".join(lines)


class ClaudeComputerUseAgent(BenchmarkAgent):
    """Agent using Claude's native computer_use tool.

    Uses client.beta.messages.create() with the computer_use tool for
    structured action output. Claude predicts pixel coordinates directly
    (it was trained for this), and actions come back as typed dicts
    rather than free text that needs regex parsing.

    When a multi-level demo is provided (with GOAL/PLAN/REFERENCE TRAJECTORY),
    the agent parses the demo structure, tracks plan progress, injects dynamic
    progress summaries at each step, and overrides premature "done" signals.

    Args:
        api_key: Anthropic API key. Falls back to ANTHROPIC_API_KEY env var.
        model: Model to use. Defaults to claude-sonnet-4-6.
        display_width: Width of the display in pixels.
        display_height: Height of the display in pixels.
        demo: Optional demonstration text to include at every step.
        max_tokens: Maximum tokens per API response.
    """

    DEFAULT_MODEL = "claude-sonnet-4-6"

    # Minimum normalized coordinate to avoid PyAutoGUI fail-safe (top-left corner)
    _COORD_EPS = 0.005  # ~6px at 1280, ~4px at 720

    # Maximum consecutive done overrides before accepting "done"
    MAX_DONE_OVERRIDES = 3

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        display_width: int = 1280,
        display_height: int = 720,
        demo: str | None = None,
        max_tokens: int = 4096,
    ):
        self.model = model or self.DEFAULT_MODEL
        self.display_width = display_width
        self.display_height = display_height
        self.demo = demo
        self.max_tokens = max_tokens

        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is required. "
                "Set it in environment or pass api_key parameter."
            )

        try:
            from anthropic import Anthropic

            self._client = Anthropic(api_key=self.api_key)
        except ImportError:
            raise RuntimeError(
                "anthropic package required. Install with: pip install anthropic"
            )

        # Conversation state (maintained across steps within an episode)
        self._messages: list[dict[str, Any]] = []
        self._step_count = 0
        self._last_tool_use_id: str | None = None

        # Plan progress tracking (enabled when multi-level demo is provided)
        self._parsed_demo: dict | None = _parse_multilevel_demo(demo) if demo else None
        self._plan_steps: list[dict] = []
        self._trajectory: list[dict] = []
        self._goal: str = ""
        self._consecutive_done_overrides: int = 0

        # When True, _advance_plan_steps() is a no-op.  The DemoController
        # sets this flag so that step progression is driven exclusively by
        # VLM verification, preventing drift between the agent's keyword
        # heuristic and the controller's verifier.
        self._external_step_control: bool = False

        if self._parsed_demo:
            self._goal = self._parsed_demo["goal"]
            self._trajectory = self._parsed_demo["trajectory"]
            # Build plan step tracking list
            for i, step_text in enumerate(self._parsed_demo["plan_steps"]):
                self._plan_steps.append({
                    "text": step_text,
                    "status": "pending",
                    "step_num": i + 1,
                })
            # Mark the first step as in_progress
            if self._plan_steps:
                self._plan_steps[0]["status"] = "in_progress"
            logger.info(
                f"Multi-level demo parsed: goal='{self._goal[:60]}...', "
                f"{len(self._plan_steps)} plan steps, "
                f"{len(self._trajectory)} trajectory steps"
            )
        else:
            logger.info(
                f"ClaudeComputerUseAgent initialized: model={self.model}, "
                f"display={self.display_width}x{self.display_height}"
            )
            if self.demo:
                logger.info(
                    f"Demo provided ({len(self.demo)} chars, non-multilevel) "
                    f"- persists across all steps"
                )

    def _clamp_coord(self, x_norm: float, y_norm: float) -> tuple[float, float]:
        """Clamp normalized coordinates away from (0,0) to avoid fail-safe."""
        if x_norm < self._COORD_EPS and y_norm < self._COORD_EPS:
            logger.warning(
                f"Clamping near-zero coordinates ({x_norm:.4f}, {y_norm:.4f}) "
                f"to ({self._COORD_EPS}, {self._COORD_EPS}) to avoid fail-safe"
            )
            return (self._COORD_EPS, self._COORD_EPS)
        return (x_norm, y_norm)

    def reset(self) -> None:
        """Reset agent state between episodes."""
        # Note: _external_step_control is not reset here because the controller
        # that set it persists across resets
        self._messages = []
        self._step_count = 0
        self._last_tool_use_id = None
        self._consecutive_done_overrides = 0

        # Re-initialize plan progress if multi-level demo was parsed
        if self._parsed_demo:
            self._plan_steps = []
            for i, step_text in enumerate(self._parsed_demo["plan_steps"]):
                self._plan_steps.append({
                    "text": step_text,
                    "status": "pending",
                    "step_num": i + 1,
                })
            if self._plan_steps:
                self._plan_steps[0]["status"] = "in_progress"

    # Maximum internal retries for screenshot/wait actions before returning
    MAX_INTERNAL_RETRIES = 5

    def act(
        self,
        observation: BenchmarkObservation,
        task: BenchmarkTask,
        history: list[tuple[BenchmarkObservation, BenchmarkAction]] | None = None,
    ) -> BenchmarkAction:
        """Given observation and task, return next action.

        On the first step, sends the task instruction (with optional demo)
        as the initial user message. On subsequent steps, sends the screenshot
        as a tool_result for the previous tool_use.

        When Claude responds with a screenshot or wait action, this method
        loops internally (sending the screenshot back as a tool_result) rather
        than returning to the runner, since those actions don't affect the
        environment.

        Args:
            observation: Current observation from the environment.
            task: Task being performed.
            history: Optional list of previous (observation, action) pairs.

        Returns:
            Action to execute.
        """
        self._step_count += 1
        screenshot_b64 = self._encode_screenshot(observation)

        if screenshot_b64 is None:
            logger.warning("No screenshot available from environment")
            return BenchmarkAction(
                type="error",
                raw_action={"reason": "no_screenshot", "error_type": "infrastructure"},
            )

        if self._step_count == 1:
            # First step: send task instruction + initial screenshot
            self._messages = self._build_initial_messages(
                task.instruction, screenshot_b64
            )
        else:
            # Subsequent steps: send screenshot as tool_result
            if self._last_tool_use_id:
                tool_result = self._build_tool_result(
                    screenshot_b64, self._last_tool_use_id
                )
                content: list[dict[str, Any]] = [tool_result]
                # Re-inject demo at every step so it doesn't drift out of context.
                # When _external_step_control is True the DemoController provides
                # its own step-aware prompt via the augmented task instruction, so
                # skip injecting the agent's (stale) plan progress to avoid
                # conflicting step-tracking signals.
                if self._plan_steps and not self._external_step_control:
                    # Multi-level demo: inject dynamic plan progress
                    progress_text = _build_plan_progress_text(
                        self._goal,
                        self._plan_steps,
                        self._trajectory,
                        self._step_count,
                    )
                    content.append({
                        "type": "text",
                        "text": (
                            f"PLAN PROGRESS (agent step {self._step_count}):"
                            f"\n---\n{progress_text}\n---"
                        ),
                    })
                elif self.demo and not self._external_step_control:
                    # Non-multilevel demo: inject static text
                    content.append({
                        "type": "text",
                        "text": (
                            f"DEMONSTRATION (follow this pattern, you are at step "
                            f"{self._step_count}):\n---\n{self.demo}\n---"
                        ),
                    })
                self._messages.append({"role": "user", "content": content})

        # Loop: call API, and if Claude requests a screenshot/wait, send the
        # screenshot back and call again (up to MAX_INTERNAL_RETRIES times)
        for attempt in range(self.MAX_INTERNAL_RETRIES + 1):
            response = self._call_api()
            if response is None:
                return BenchmarkAction(
                    type="error",
                    raw_action={"reason": "api_call_failed", "error_type": "infrastructure"},
                )

            # Add assistant response to conversation
            self._messages.append({"role": "assistant", "content": response.content})

            # Check if Claude requested screenshot or wait (internal actions)
            internal_action = self._get_internal_action(response)
            if internal_action is not None:
                logger.info(
                    f"Claude requested '{internal_action}' (attempt {attempt + 1}/"
                    f"{self.MAX_INTERNAL_RETRIES}), sending screenshot back"
                )
                if internal_action == "wait":
                    import time
                    time.sleep(2)
                # Send screenshot as tool_result and loop
                tool_result = self._build_tool_result(
                    screenshot_b64, self._last_tool_use_id
                )
                self._messages.append({"role": "user", "content": [tool_result]})
                continue

            # Real action — return to runner
            return self._process_response(response, observation)

        # Exhausted retries on screenshot/wait — return error (not done)
        logger.warning(
            f"Exhausted {self.MAX_INTERNAL_RETRIES} internal retries on "
            "screenshot/wait actions"
        )
        return BenchmarkAction(
            type="error",
            raw_action={"reason": "max_internal_retries_exceeded", "error_type": "infrastructure"},
        )

    def _build_initial_messages(
        self, instruction: str, screenshot_b64: str | None
    ) -> list[dict[str, Any]]:
        """Build the initial user message with task instruction and optional demo.

        For multi-level demos, injects plan progress instead of raw demo text.

        Args:
            instruction: Task instruction text.
            screenshot_b64: Base64-encoded screenshot PNG.

        Returns:
            List with single user message.
        """
        content_parts: list[dict[str, Any]] = []

        # Build text prompt.
        # When _external_step_control is True the DemoController supplies its
        # own step-aware instruction, so we skip injecting the agent's
        # (potentially stale) plan progress to avoid conflicting signals.
        if self._plan_steps and not self._external_step_control:
            # Multi-level demo: use structured plan progress
            progress_text = _build_plan_progress_text(
                self._goal,
                self._plan_steps,
                self._trajectory,
                self._step_count,
            )
            text = (
                f"Here is a structured plan for the task:\n\n"
                f"{progress_text}\n\n"
                f"Now complete this task: {instruction}"
            )
        elif self.demo and not self._external_step_control:
            text = (
                f"Here is a demonstration of a similar completed task:\n\n"
                f"{self.demo}\n\n"
                f"Now complete this task: {instruction}"
            )
        else:
            text = f"Task: {instruction}"
        content_parts.append({"type": "text", "text": text})

        # Include initial screenshot as an image
        if screenshot_b64:
            content_parts.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": screenshot_b64,
                    },
                }
            )

        return [{"role": "user", "content": content_parts}]

    def _build_tool_result(
        self, screenshot_b64: str | None, tool_use_id: str
    ) -> dict[str, Any]:
        """Build a tool_result block with a screenshot.

        Args:
            screenshot_b64: Base64-encoded screenshot PNG.
            tool_use_id: ID of the tool_use block this result is for.

        Returns:
            tool_result dict for the messages array.
        """
        content: list[dict[str, Any]] = []
        if screenshot_b64:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": screenshot_b64,
                    },
                }
            )
        else:
            content.append({"type": "text", "text": "Screenshot unavailable."})

        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": content,
        }

    def _call_api(self):
        """Call the Anthropic beta API with computer_use tool.

        Returns:
            API response object, or None on error.
        """
        tools = [
            {
                "type": COMPUTER_TOOL_TYPE,
                "name": "computer",
                "display_width_px": self.display_width,
                "display_height_px": self.display_height,
            }
        ]

        try:
            response = self._client.beta.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                messages=self._messages,
                tools=tools,
                betas=[COMPUTER_USE_BETA],
            )
            return response
        except Exception as e:
            logger.error(f"API call failed: {e}")
            return None

    def _process_response(
        self, response: Any, observation: BenchmarkObservation
    ) -> BenchmarkAction:
        """Extract BenchmarkAction from API response.

        Looks for tool_use blocks in the response content. If none found,
        treats as task completion (done). If plan steps remain and Claude
        declares "done" prematurely, overrides the done signal by injecting
        a continuation prompt and re-calling the API.

        Args:
            response: API response object.
            observation: Current observation (for coordinate context).

        Returns:
            Parsed BenchmarkAction.
        """
        for block in response.content:
            if block.type == "tool_use" and block.name == "computer":
                self._last_tool_use_id = block.id
                action = self._map_action(block.input, observation)
                # Advance plan steps heuristically after returning a real action
                self._advance_plan_steps(action)
                # Reset consecutive done overrides since we got a real action
                self._consecutive_done_overrides = 0
                return action

        # No tool_use block — Claude considers task complete
        text_parts = [b.text for b in response.content if hasattr(b, "text")]

        # Check for premature done when plan steps remain.
        # When _external_step_control is True the DemoController handles
        # done-override logic, so the agent should not also override based
        # on its own (stale) plan steps.
        if (
            self._plan_steps
            and not self._external_step_control
            and self._has_remaining_plan_steps()
        ):
            if self._consecutive_done_overrides < self.MAX_DONE_OVERRIDES:
                self._consecutive_done_overrides += 1
                remaining = self._get_remaining_step_descriptions()
                logger.warning(
                    f"Overriding premature 'done' (override "
                    f"{self._consecutive_done_overrides}/{self.MAX_DONE_OVERRIDES}). "
                    f"Remaining steps: {remaining}"
                )
                # Inject continuation prompt
                progress_text = _build_plan_progress_text(
                    self._goal,
                    self._plan_steps,
                    self._trajectory,
                    self._step_count,
                )
                continuation = (
                    f"You declared done, but the following plan steps are NOT "
                    f"complete yet:\n{remaining}\n\n"
                    f"{progress_text}\n\n"
                    f"Please continue with the next incomplete step. "
                    f"Do NOT declare done until ALL steps are complete."
                )
                self._messages.append({
                    "role": "user",
                    "content": [{"type": "text", "text": continuation}],
                })

                # Re-call the API
                retry_response = self._call_api()
                if retry_response is None:
                    return BenchmarkAction(
                        type="error",
                        raw_action={
                            "reason": "api_call_failed",
                            "error_type": "infrastructure",
                        },
                    )
                self._messages.append({
                    "role": "assistant",
                    "content": retry_response.content,
                })
                # Recursively process the retry response
                return self._process_response(retry_response, observation)
            else:
                logger.warning(
                    f"Accepting 'done' after {self.MAX_DONE_OVERRIDES} "
                    f"consecutive overrides. Remaining steps: "
                    f"{self._get_remaining_step_descriptions()}"
                )

        return BenchmarkAction(
            type="done",
            raw_action={"reason": "no_tool_use", "text": " ".join(text_parts)},
        )

    def _has_remaining_plan_steps(self) -> bool:
        """Check if any plan steps are not yet done.

        Returns:
            True if there are pending or in_progress steps.
        """
        return any(s["status"] != "done" for s in self._plan_steps)

    def _get_remaining_step_descriptions(self) -> str:
        """Get a formatted string of remaining plan step descriptions.

        Returns:
            Newline-separated list of remaining step numbers and descriptions.
        """
        remaining = [
            f"  [{s['step_num']}] {s['text']}"
            for s in self._plan_steps
            if s["status"] != "done"
        ]
        return "\n".join(remaining)

    def _advance_plan_steps(self, action: BenchmarkAction) -> None:
        """Advance plan step tracking based on the action being taken.

        Only advances at most ONE step at a time to prevent tracking drift.
        The current in_progress step is marked as done and the next pending
        step becomes in_progress. This conservative approach avoids the
        problem of keyword heuristics aggressively skipping multiple steps
        based on superficial text matches (e.g., typing "Year" matching
        both the header step and the data entry step).

        When ``_external_step_control`` is True (set by :class:`DemoController`),
        this method is a no-op because step progression is managed by VLM
        verification in the controller.

        Args:
            action: The BenchmarkAction being returned to the runner.
        """
        if self._external_step_control:
            return

        if not self._plan_steps:
            return

        # Build a representation of the action for matching
        action_keywords = self._extract_action_keywords(action)
        if not action_keywords:
            return

        # Find the current in_progress step
        current_idx = None
        for i, step in enumerate(self._plan_steps):
            if step["status"] == "in_progress":
                current_idx = i
                break

        if current_idx is None:
            # No in_progress step -- try to start the first pending one
            for i, step in enumerate(self._plan_steps):
                if step["status"] == "pending":
                    step["status"] = "in_progress"
                    logger.info(
                        f"Plan step {step['step_num']} now in_progress: "
                        f"{step['text'][:60]}"
                    )
                    break
            return

        # Check if the action matches the NEXT step better than the current
        # one. Only consider the immediately next step to prevent multi-step
        # jumps that cause tracking drift.
        current_score = self._match_score(action_keywords, current_idx)
        next_idx = current_idx + 1

        # Find next non-done step
        while next_idx < len(self._plan_steps):
            if self._plan_steps[next_idx]["status"] != "done":
                break
            next_idx += 1

        if next_idx < len(self._plan_steps):
            next_score = self._match_score(action_keywords, next_idx)
            if next_score > current_score and next_score > 0:
                # Advance exactly one step: current -> done, next -> in_progress
                self._plan_steps[current_idx]["status"] = "done"
                logger.info(
                    f"Plan step {self._plan_steps[current_idx]['step_num']} "
                    f"marked done: {self._plan_steps[current_idx]['text'][:60]}"
                )
                self._plan_steps[next_idx]["status"] = "in_progress"
                logger.info(
                    f"Plan step {self._plan_steps[next_idx]['step_num']} "
                    f"now in_progress: "
                    f"{self._plan_steps[next_idx]['text'][:60]}"
                )

    def _extract_action_keywords(self, action: BenchmarkAction) -> set[str]:
        """Extract keywords from an action for matching against plan steps.

        Args:
            action: BenchmarkAction to extract keywords from.

        Returns:
            Set of lowercase keyword strings.
        """
        keywords: set[str] = set()
        keywords.add(action.type)

        if action.text:
            # Add typed text and individual words
            keywords.add(action.text.lower())
            for word in action.text.lower().split():
                if len(word) > 2:
                    keywords.add(word)

        if action.key:
            keywords.add(action.key.lower())

        if action.raw_action:
            claude_action = action.raw_action.get("claude_action", {})
            action_name = claude_action.get("action", "")
            if action_name:
                keywords.add(action_name.lower())
            click_variant = action.raw_action.get("click_variant", "")
            if click_variant:
                keywords.add(click_variant.lower())

        return keywords

    def _match_score(self, action_keywords: set[str], step_idx: int) -> int:
        """Score how well action keywords match a plan step.

        Checks both the plan step text and the corresponding trajectory
        step's action description for keyword overlaps.

        Args:
            action_keywords: Set of keywords from the current action.
            step_idx: Index into self._plan_steps.

        Returns:
            Integer score (higher = better match).
        """
        score = 0
        step = self._plan_steps[step_idx]
        step_text_lower = step["text"].lower()

        # Check plan step text for keyword matches
        for kw in action_keywords:
            if kw in step_text_lower:
                score += 1

        # Check corresponding trajectory step action text
        step_num = step["step_num"]
        for traj in self._trajectory:
            if traj["step_num"] == step_num:
                traj_action_lower = traj["action"].lower()
                for kw in action_keywords:
                    if kw in traj_action_lower:
                        score += 1
                break

        return score

    def _map_action(
        self, tool_input: dict[str, Any], observation: BenchmarkObservation
    ) -> BenchmarkAction:
        """Map Claude computer_use tool input to BenchmarkAction.

        Args:
            tool_input: The input dict from the tool_use block.
            observation: Current observation for coordinate normalization.

        Returns:
            BenchmarkAction with normalized [0,1] coordinates.
        """
        action_type = tool_input.get("action", "")
        raw = {"claude_action": tool_input}

        # Click actions
        if action_type in (
            "left_click",
            "right_click",
            "middle_click",
            "double_click",
            "triple_click",
        ):
            coord = tool_input.get("coordinate", [0, 0])
            x_norm, y_norm = self._clamp_coord(
                coord[0] / self.display_width, coord[1] / self.display_height
            )
            ba_type = "click"
            raw["click_variant"] = action_type
            return BenchmarkAction(
                type=ba_type, x=x_norm, y=y_norm, raw_action=raw
            )

        # Type action
        if action_type == "type":
            return BenchmarkAction(
                type="type", text=tool_input.get("text", ""), raw_action=raw
            )

        # Key action
        if action_type == "key":
            key_str = tool_input.get("text", "")
            if "+" in key_str:
                parts = key_str.split("+")
                return BenchmarkAction(
                    type="key",
                    key=parts[-1],
                    modifiers=parts[:-1],
                    raw_action=raw,
                )
            return BenchmarkAction(type="key", key=key_str, raw_action=raw)

        # Scroll action
        if action_type == "scroll":
            direction = tool_input.get("scroll_direction", "down")
            amount = tool_input.get("scroll_amount", 3)
            return BenchmarkAction(
                type="scroll",
                scroll_direction=direction,
                scroll_amount=float(amount),
                raw_action=raw,
            )

        # Drag action
        if action_type == "left_click_drag":
            # Claude's computer_use API uses snake_case field names:
            #   start_coordinate: [x, y]  (drag start)
            #   coordinate: [x, y]        (drag end)
            start = tool_input.get("start_coordinate", [0, 0])
            end = tool_input.get("coordinate", [0, 0])
            sx, sy = self._clamp_coord(
                start[0] / self.display_width, start[1] / self.display_height
            )
            ex, ey = self._clamp_coord(
                end[0] / self.display_width, end[1] / self.display_height
            )
            return BenchmarkAction(
                type="drag",
                x=sx, y=sy, end_x=ex, end_y=ey,
                raw_action=raw,
            )

        # Mouse move — treat as a click with no effect for BenchmarkAction
        if action_type == "mouse_move":
            coord = tool_input.get("coordinate", [0, 0])
            x_norm, y_norm = self._clamp_coord(
                coord[0] / self.display_width, coord[1] / self.display_height
            )
            raw["is_mouse_move"] = True
            return BenchmarkAction(
                type="click",
                x=x_norm, y=y_norm,
                raw_action=raw,
            )

        # Screenshot and wait are handled internally by act() loop — they
        # should not reach here. If they do, treat as no-op.
        if action_type in ("screenshot", "wait"):
            logger.warning(
                f"'{action_type}' reached _map_action (should be handled by "
                "act() loop). Treating as no-op."
            )
            return BenchmarkAction(
                type="error",
                raw_action={"error": f"Unexpected internal action: {action_type}", **raw},
            )

        # Unknown action
        logger.warning(f"Unknown computer_use action: {action_type}")
        return BenchmarkAction(
            type="error",
            raw_action={"error": f"Unknown action: {action_type}", **raw},
        )

    def _get_internal_action(self, response: Any) -> str | None:
        """Check if response contains a screenshot or wait action.

        These actions don't affect the environment — they're requests from
        Claude to see the current screen or pause briefly. They should be
        handled internally by the act() loop rather than returned to the runner.

        Args:
            response: API response object.

        Returns:
            "screenshot" or "wait" if internal action found, None otherwise.
        """
        for block in response.content:
            if block.type == "tool_use" and block.name == "computer":
                action_type = block.input.get("action", "")
                if action_type in ("screenshot", "wait"):
                    self._last_tool_use_id = block.id
                    return action_type
        return None

    def _encode_screenshot(
        self, observation: BenchmarkObservation
    ) -> str | None:
        """Encode screenshot from observation to base64 PNG.

        Args:
            observation: Current observation.

        Returns:
            Base64-encoded PNG string, or None if no screenshot available.
        """
        screenshot_bytes = observation.screenshot
        if screenshot_bytes is None and observation.screenshot_path:
            try:
                screenshot_bytes = Path(observation.screenshot_path).read_bytes()
            except (FileNotFoundError, OSError):
                pass

        if screenshot_bytes is None:
            return None

        # Ensure it's PNG format and reasonable size
        try:
            img = Image.open(BytesIO(screenshot_bytes))
            # Update display dimensions from actual screenshot if needed
            if observation.viewport:
                self.display_width, self.display_height = observation.viewport
            elif img.size != (self.display_width, self.display_height):
                self.display_width, self.display_height = img.size

            buf = BytesIO()
            img.save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode("utf-8")
        except Exception as e:
            logger.warning(f"Failed to process screenshot: {e}")
            # Fall back to raw bytes
            return base64.b64encode(screenshot_bytes).decode("utf-8")

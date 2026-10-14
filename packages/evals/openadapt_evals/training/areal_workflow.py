"""AReaL AgentWorkflow wrapping WAADesktopEnv for RL training.

Implements AReaL's agent workflow pattern: a class with an async ``run()``
method that receives task data, communicates with an LLM through AReaL's
proxy (OpenAI-compatible), interacts with a desktop environment, and returns
a scalar reward.

AReaL transparently intercepts the OpenAI calls to track tokens, logprobs,
and compute gradients -- the workflow only needs to use the standard
AsyncOpenAI client pointed at the proxy ``base_url``.

AReaL is an OPTIONAL dependency. This module gracefully handles the case
where AReaL is not installed.

Usage with AReaL:
    # In your AReaL config YAML:
    workflow: openadapt_evals.training.areal_workflow.WAADesktopWorkflow

    # AReaL calls WAADesktopWorkflow.run() with:
    #   data = {"task_id": "...", "instruction": "...", "max_steps": 15}
    #   extra_kwargs = {"base_url": "http://...", "api_key": "..."}

Usage standalone (testing without AReaL):
    from openadapt_evals.training.areal_workflow import WAADesktopWorkflow

    wf = WAADesktopWorkflow()
    reward = await wf.run(
        data={"task_id": "abc-123", "instruction": "Change font to Arial"},
        base_url="http://localhost:8000/v1",
        api_key="fake",
    )
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any

from openadapt_evals.adapters.rl_env import ResetConfig, RLEnvironment
from openadapt_evals.training.trl_rollout import parse_action_json

# openai is a required dependency of openadapt-evals (listed in pyproject.toml),
# but we import it here with a guard for clarity and to allow module import
# even if openai is somehow missing.
try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None  # type: ignore[misc, assignment]

logger = logging.getLogger(__name__)

# System prompt matching the openadapt-ml agent format (JSON action DSL)
SYSTEM_PROMPT = (
    "You are a desktop automation agent. Given a screenshot and task instruction, "
    "output the next action as JSON:\n"
    '{"type": "click"|"type"|"key"|"scroll"|"drag"|"done", '
    '"x": 0.0-1.0, "y": 0.0-1.0, "text": "...", "key": "..."}\n\n'
    "Coordinates are normalized fractions of the screen (0.0 = left/top, "
    "1.0 = right/bottom). Output exactly one action per turn."
)


def _screenshot_to_base64(screenshot: bytes) -> str:
    """Encode screenshot bytes as a base64 data URI for OpenAI vision."""
    b64 = base64.b64encode(screenshot).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _build_messages(
    instruction: str,
    screenshot: bytes | None,
    history: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build OpenAI chat messages with screenshot and instruction.

    Args:
        instruction: Natural language task description.
        screenshot: Current desktop screenshot as PNG bytes.
        history: Previous assistant messages (action history).

    Returns:
        List of OpenAI chat message dicts.
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
    ]

    # Initial user message with instruction
    user_content: list[dict[str, Any]] = [
        {"type": "text", "text": f"Task: {instruction}"},
    ]
    if screenshot:
        user_content.append({
            "type": "image_url",
            "image_url": {"url": _screenshot_to_base64(screenshot)},
        })
    messages.append({"role": "user", "content": user_content})

    # Append action history (alternating assistant/user with screenshots)
    for entry in history:
        messages.append({"role": "assistant", "content": entry["action_text"]})
        if entry.get("screenshot"):
            messages.append({
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": _screenshot_to_base64(entry["screenshot"]),
                        },
                    },
                    {"type": "text", "text": "What is the next action?"},
                ],
            })

    return messages


class WAADesktopWorkflow:
    """AReaL AgentWorkflow wrapping WAADesktopEnv.

    Each ``run()`` call executes one episode:
    1. Reset environment with task_id
    2. Loop: screenshot -> LLM (via AReaL proxy) -> parse action -> execute
    3. Evaluate with dense milestone rewards
    4. Return reward scalar

    AReaL passes ``base_url`` and ``api_key`` through ``extra_kwargs``.
    The workflow creates an AsyncOpenAI client pointed at AReaL's proxy,
    which transparently tracks tokens and logprobs for gradient computation.

    Constructor kwargs are forwarded as generation parameters to the
    chat completions API (temperature, max_tokens, etc.).
    """

    def __init__(self, **kwargs: Any) -> None:
        # Store generation kwargs (temperature, max_tokens, etc.)
        self.max_tokens: int = kwargs.pop("max_tokens", 256)
        self.temperature: float = kwargs.pop("temperature", 0.7)
        self.kwargs = kwargs

    async def run(
        self, data: dict[str, Any], **extra_kwargs: Any
    ) -> float | dict[str, float]:
        """Execute one episode and return the reward.

        Args:
            data: Task specification dict with keys:
                - task_id (str): WAA task UUID.
                - instruction (str): Natural language task description.
                - max_steps (int, optional): Max actions per episode (default 15).
                - task_config_path (str, optional): Path to YAML task config
                  for dense milestone rewards.
                - server_url (str, optional): WAA server URL.
                - evaluate_url (str, optional): Evaluate endpoint URL.
            **extra_kwargs: AReaL passes:
                - base_url (str): URL of AReaL's OpenAI-compatible proxy.
                - api_key (str): API key for the proxy.
                - http_client: Optional httpx client for connection reuse.

        Returns:
            float: Trajectory-level reward (0.0 to 1.0) from dense evaluation.
            dict[str, float]: If per-step rewards are requested (future).
        """
        if AsyncOpenAI is None:
            raise ImportError(
                "openai package is required for WAADesktopWorkflow. "
                "Install with: pip install openai>=1.0.0"
            )

        # --- Extract AReaL proxy config ---
        http_client = extra_kwargs.get("http_client")
        base_url = extra_kwargs.get("base_url") or os.getenv("OPENAI_BASE_URL")
        api_key = extra_kwargs.get("api_key") or os.getenv("OPENAI_API_KEY", "fake")

        client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            http_client=http_client,
            max_retries=0,
        )

        # --- Extract task config ---
        task_id = data.get("task_id", "")
        instruction = data.get("instruction", "")
        max_steps = data.get("max_steps", 15)
        server_url = data.get("server_url", "http://localhost:5000")
        evaluate_url = data.get("evaluate_url")

        # --- Create environment ---
        env = self._create_env(
            server_url=server_url,
            evaluate_url=evaluate_url,
            task_id=task_id,
            task_config_path=data.get("task_config_path"),
        )

        # --- Reset ---
        import asyncio

        obs = await asyncio.to_thread(
            env.reset, ResetConfig(task_id=task_id)
        )

        # --- Episode loop ---
        history: list[dict[str, Any]] = []
        completion_ids: list[str] = []

        for step in range(max_steps):
            screenshot = obs.screenshot or b""

            # Build messages with current screenshot and history
            messages = _build_messages(instruction, screenshot, history)

            # Call LLM via AReaL proxy
            completion = await client.chat.completions.create(
                model="default",
                messages=messages,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
            )

            action_text = completion.choices[0].message.content or ""
            comp_id = completion.id
            if comp_id:
                completion_ids.append(comp_id)

            # Parse action JSON from LLM output
            action = parse_action_json(action_text)

            # Check for terminal action
            if action.type == "done":
                break

            # Handle fractional coordinates -> pixel conversion
            if action.x is not None and action.y is not None:
                if 0.0 <= action.x <= 1.0 and 0.0 <= action.y <= 1.0:
                    rollout_step = await asyncio.to_thread(
                        env.pixel_action,
                        x_frac=action.x,
                        y_frac=action.y,
                        action_type=action.type,
                        text=action.text,
                        key=action.key,
                    )
                else:
                    rollout_step = await asyncio.to_thread(
                        env.pixel_action,
                        x=int(action.x),
                        y=int(action.y),
                        action_type=action.type,
                        text=action.text,
                        key=action.key,
                    )
            else:
                rollout_step = await asyncio.to_thread(env.step, action)

            obs = rollout_step.observation

            # Track history for multi-turn context
            history.append({
                "action_text": action_text,
                "screenshot": obs.screenshot,
            })

            if rollout_step.done:
                break

        # --- Evaluate ---
        reward = await asyncio.to_thread(env.evaluate_dense)

        logger.info(
            "Episode complete: task=%s, steps=%d, reward=%.3f",
            task_id,
            env.step_count,
            reward,
        )

        return reward

    def _create_env(
        self,
        server_url: str,
        evaluate_url: str | None,
        task_id: str,
        task_config_path: str | None,
    ) -> RLEnvironment:
        """Create an RLEnvironment for the episode.

        Lazily imports WAALiveAdapter to avoid hard dependency on the
        full WAA stack when running tests with mock adapters.

        Args:
            server_url: WAA Flask API URL.
            evaluate_url: Evaluate server URL (optional).
            task_id: WAA task UUID.
            task_config_path: Optional path to YAML task config for milestones.

        Returns:
            Configured RLEnvironment.
        """
        from openadapt_evals.adapters.waa.live import WAALiveAdapter, WAALiveConfig

        adapter = WAALiveAdapter(
            WAALiveConfig(
                server_url=server_url,
                evaluate_url=evaluate_url,
            )
        )

        # Load task config for dense rewards if provided
        task_config = None
        if task_config_path:
            from openadapt_evals.task_config import TaskConfig

            task_config = TaskConfig.from_yaml(task_config_path)

        return RLEnvironment(
            adapter,
            default_task_id=task_id,
            task_config=task_config,
        )

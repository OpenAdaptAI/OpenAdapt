"""OpenEnv-compatible WAA desktop environment.

Wraps RLEnvironment + WAALiveAdapter into the OpenEnv Environment
interface. Each instance maps to one Windows VM session.

Can be used standalone (direct Python) or served via OpenEnv's
create_app() as an HTTP+WebSocket server.

Usage (standalone):
    env = WAAOpenEnvEnvironment(server_url="http://localhost:5001")
    obs = env.reset(task_id="custom-notepad-hello")
    obs = env.step(WAAAction(type="click", x=0.5, y=0.3))
    print(env.state)

Usage (server):
    from openenv.core.env_server.http_server import create_app
    from openadapt_evals.openenv.models import WAAAction, WAAObservation
    from openadapt_evals.openenv.environment import WAAOpenEnvEnvironment

    app = create_app(WAAOpenEnvEnvironment, WAAAction, WAAObservation,
                     env_name="waa_desktop")
    uvicorn.run(app, host="0.0.0.0", port=8000)
"""

from __future__ import annotations

import base64
import logging
from typing import Any
from uuid import uuid4

from openadapt_evals.openenv.models import WAAAction, WAAObservation, WAAState

logger = logging.getLogger(__name__)


class WAAOpenEnvEnvironment:
    """OpenEnv-compatible WAA desktop environment.

    Follows the OpenEnv Environment protocol (reset/step/state) without
    requiring openenv-core as an import-time dependency. When served via
    create_app(), OpenEnv discovers the methods via duck typing.

    Args:
        server_url: WAA Flask server URL (default: http://localhost:5001).
        evaluate_url: Separate evaluate server URL (default: same as server_url).
        default_task_id: Task ID to use when reset() is called without one.
        max_steps: Maximum steps per episode.
        task_config_dir: Directory of YAML task configs for dense rewards.
    """

    SUPPORTS_CONCURRENT_SESSIONS = False

    def __init__(
        self,
        server_url: str = "http://localhost:5001",
        evaluate_url: str | None = None,
        default_task_id: str | None = None,
        max_steps: int = 15,
        task_config_dir: str | None = None,
        **kwargs: Any,
    ):
        self._server_url = server_url
        self._evaluate_url = evaluate_url
        self._default_task_id = default_task_id
        self._max_steps = max_steps
        self._task_config_dir = task_config_dir
        self._rl_env = None
        self._task_configs: dict[str, Any] = {}
        self._state = WAAState()

        # Load task configs if directory provided
        if task_config_dir:
            self._load_task_configs(task_config_dir)

    def _load_task_configs(self, dir_path: str) -> None:
        """Load YAML task configs from a directory."""
        from openadapt_evals.task_config import TaskConfig

        for tc in TaskConfig.from_dir(dir_path):
            self._task_configs[tc.id] = tc
            self._task_configs[tc.name] = tc

    def _ensure_rl_env(self):
        """Lazily create the RLEnvironment + adapter."""
        if self._rl_env is not None:
            return self._rl_env

        from openadapt_evals.adapters.rl_env import RLEnvironment
        from openadapt_evals.adapters.waa.live import WAALiveAdapter, WAALiveConfig

        adapter = WAALiveAdapter(
            WAALiveConfig(
                server_url=self._server_url,
                evaluate_url=self._evaluate_url,
            )
        )
        self._rl_env = RLEnvironment(
            adapter, default_task_id=self._default_task_id
        )
        return self._rl_env

    def reset(
        self,
        seed: int | None = None,
        episode_id: str | None = None,
        **kwargs: Any,
    ) -> WAAObservation:
        """Reset the environment to a task's initial state.

        Args:
            seed: Random seed (unused, for OpenEnv compatibility).
            episode_id: Episode identifier.
            **kwargs: May include task_id to override default.

        Returns:
            Initial WAAObservation with screenshot.
        """
        env = self._ensure_rl_env()
        task_id = kwargs.get("task_id", self._default_task_id)

        # Load TaskConfig for dense rewards if available
        tc = self._task_configs.get(task_id)
        if tc:
            env.load_task_config(tc)

        from openadapt_evals.adapters.rl_env import ResetConfig

        obs = env.reset(config=ResetConfig(task_id=task_id))

        self._state = WAAState(
            episode_id=episode_id or uuid4().hex[:12],
            step_count=0,
            task_id=task_id,
            task_name=tc.name if tc else task_id,
            status="running",
        )

        return self._to_observation(obs)

    def step(
        self,
        action: WAAAction,
        timeout_s: float | None = None,
        **kwargs: Any,
    ) -> WAAObservation:
        """Execute an action in the environment.

        Args:
            action: The action to execute.
            timeout_s: Timeout (unused, for OpenEnv compatibility).

        Returns:
            WAAObservation with new screenshot, reward, and done flag.
        """
        env = self._ensure_rl_env()
        self._state.step_count += 1

        # Handle fractional coordinates
        if action.type == "done":
            from openadapt_evals.adapters.base import BenchmarkAction

            step_result = env.step(BenchmarkAction(type="done"))
        elif (
            action.x is not None
            and action.y is not None
            and 0 <= action.x <= 1
            and 0 <= action.y <= 1
        ):
            step_result = env.pixel_action(
                x_frac=action.x,
                y_frac=action.y,
                action_type=action.type,
                text=action.text,
                key=action.key,
            )
        else:
            from openadapt_evals.adapters.base import BenchmarkAction

            step_result = env.step(
                BenchmarkAction(
                    type=action.type,
                    x=action.x,
                    y=action.y,
                    text=action.text,
                    key=action.key,
                )
            )

        done = step_result.done or self._state.step_count >= self._max_steps

        # Compute reward at episode end
        reward = None
        if done:
            try:
                reward = env.evaluate_dense()
                self._state.score = reward
                self._state.status = "completed"

                # Update milestone info
                last_info = step_result.info or {}
                self._state.milestones_passed = last_info.get("milestones_passed", 0)
                self._state.milestones_total = last_info.get("milestones_total", 0)
            except Exception as exc:
                logger.warning("Evaluation failed: %s", exc)
                reward = 0.0
                self._state.status = "failed"

        self._state.done = done
        return self._to_observation(step_result.observation, reward=reward, done=done)

    @property
    def state(self) -> WAAState:
        """Current environment state."""
        return self._state

    def close(self) -> None:
        """Clean up resources."""
        self._rl_env = None

    def _to_observation(
        self,
        obs: Any,
        reward: float | None = None,
        done: bool = False,
    ) -> WAAObservation:
        """Convert a BenchmarkObservation to WAAObservation."""
        screenshot_b64 = None
        if obs.screenshot:
            screenshot_b64 = base64.b64encode(obs.screenshot).decode()

        a11y = None
        if obs.accessibility_tree:
            a11y = str(obs.accessibility_tree)[:10000]  # cap for transport

        return WAAObservation(
            screenshot_b64=screenshot_b64,
            accessibility_tree=a11y,
            window_title=getattr(obs, "window_title", None),
            step_index=self._state.step_count,
            done=done,
            reward=reward,
        )

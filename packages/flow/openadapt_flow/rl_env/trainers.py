"""Thin adapters from the RL environments to TRL, SkyRL and verl.

Every adapter drives a running environment server over OpenEnv's WebSocket
client, one session per rollout, and reads the reward the server computed
from the app's record. None of them computes a reward itself.

* TRL: ``trl_environment(base_url)`` returns a class for
  ``GRPOTrainer(environment_factory=...)``. Its tool methods are the
  environment's actions; ``trl_reward`` reads the episode reward back.
* SkyRL: ``skyrl_env_class()`` returns a ``skyrl_gym`` ``BaseTextEnv``
  subclass; register it with ``skyrl_gym.envs.registration.register``.
* verl: ``verl_tool_class()`` returns a ``verl.tools.base_tool.BaseTool``
  subclass: one browser tool per trajectory whose ``calc_reward`` is the
  episode reward.

The trainer packages are imported only when their adapter is built.
Interfaces were read from TRL's OpenEnv guide, skyrl-gym's ``BaseTextEnv``
and verl's ``BaseTool`` on 2026-11-03.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

#: Observation fields shown to the model; the screenshot is left out of text.
TEXT_FIELDS = ("instruction", "url", "title", "text", "elements", "last_action_error")


def render(observation: dict[str, Any]) -> str:
    """The observation as compact JSON text for a language model."""
    return json.dumps({k: observation.get(k) for k in TEXT_FIELDS}, ensure_ascii=False)


class Session:
    """One OpenEnv WebSocket session against an environment server."""

    def __init__(
        self, base_url: str, client_factory: Optional[Callable[[str], Any]] = None
    ) -> None:
        self.base_url = base_url
        self._factory = client_factory or _openenv_client
        self._client: Any = None
        self.reward: float = 0.0
        self.done = False
        self.outcome: dict[str, Any] = {}
        self.steps = 0

    def reset(self, task_id: Optional[str] = None, **kwargs: Any) -> str:
        if self._client is None:
            self._client = self._factory(self.base_url)
        self.reward, self.done, self.outcome, self.steps = 0.0, False, {}, 0
        args = {"task_id": task_id} if task_id else {}
        result = self._client.reset(**args, **kwargs)
        return render(result.observation)

    def act(self, action: dict[str, Any]) -> str:
        if self._client is None:
            raise RuntimeError("reset the session first")
        if self.done:
            raise ValueError("The episode is over.")
        result = self._client.step(action)
        self.steps += 1
        self.done = bool(result.done)
        if result.done:
            self.reward = float(result.reward or 0.0)
            meta = result.metadata or result.observation.get("metadata") or {}
            self.outcome = dict(meta.get("outcome") or {})
        return render(result.observation)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


def _openenv_client(base_url: str) -> Any:
    from openenv import GenericEnvClient

    client = GenericEnvClient(base_url=base_url, message_timeout_s=300).sync()
    client.connect()
    return client


# -- TRL -----------------------------------------------------------------------


def trl_environment(
    base_url: str, client_factory: Optional[Callable[[str], Any]] = None
) -> type:
    """A class for ``GRPOTrainer(environment_factory=...)``.

    The dataset's ``task_id`` column picks the task. Each public method is a
    tool TRL exposes to the model.
    """

    class OpenAdaptWebEnv:
        def __init__(self) -> None:
            self.session = Session(base_url, client_factory)

        @property
        def reward(self) -> float:
            return self.session.reward

        @property
        def outcome(self) -> dict[str, Any]:
            return self.session.outcome

        def reset(self, **kwargs: Any) -> str | None:
            return self.session.reset(task_id=kwargs.get("task_id"))

        def click(self, selector: str) -> str:
            """
            Click an element.

            Args:
                selector: CSS selector of an element from the observation.

            Returns:
                The next observation.
            """
            return self.session.act({"kind": "click", "selector": selector})

        def fill(self, selector: str, text: str) -> str:
            """
            Replace the contents of an input.

            Args:
                selector: CSS selector of the input.
                text: The text to put in it.

            Returns:
                The next observation.
            """
            return self.session.act(
                {"kind": "fill", "selector": selector, "text": text}
            )

        def press(self, key: str) -> str:
            """
            Press a key.

            Args:
                key: Key name, such as Enter or Control+a.

            Returns:
                The next observation.
            """
            return self.session.act({"kind": "press", "text": key})

        def select(self, selector: str, value: str) -> str:
            """
            Choose an option of a select element.

            Args:
                selector: CSS selector of the select element.
                value: The option's value.

            Returns:
                The next observation.
            """
            return self.session.act(
                {"kind": "select", "selector": selector, "value": value}
            )

        def goto(self, path: str) -> str:
            """
            Open a path on the app.

            Args:
                path: A path or #fragment on the app's origin.

            Returns:
                The next observation.
            """
            return self.session.act({"kind": "goto", "path": path})

        def done(self, claim: str) -> str:
            """
            End the episode and report the result.

            Args:
                claim: success if the record holds the requested change exactly as asked, otherwise failure.

            Returns:
                The final observation.
            """
            return self.session.act({"kind": "done", "claim": claim})

    return OpenAdaptWebEnv


def trl_reward(environments: list[Any], **_: Any) -> list[float]:
    """TRL reward function: the record-checked episode reward."""
    return [env.reward for env in environments]


def trl_harm(environments: list[Any], **_: Any) -> list[float]:
    """TRL metric-style reward: -1 for a claimed success the record refutes."""
    return [
        -1.0 if env.outcome.get("harm", {}).get("silent_wrong_effect") else 0.0
        for env in environments
    ]


# -- SkyRL ---------------------------------------------------------------------


def skyrl_env_class(client_factory: Optional[Callable[[str], Any]] = None) -> type:
    """A ``skyrl_gym`` text environment over one OpenAdapt session.

    ``env_config`` needs ``base_url``; ``extras`` may carry ``task_id``. The
    model's reply must be one JSON action.
    """
    from skyrl_gym.envs.base_text_env import BaseTextEnv

    class OpenAdaptSkyRLEnv(BaseTextEnv):  # type: ignore[misc, valid-type]
        def __init__(self, env_config: Any, extras: Optional[dict[str, Any]] = None):
            super().__init__()
            extras = extras or {}
            self.session = Session(env_config["base_url"], client_factory)
            self.task_id = extras.get("task_id")
            self.max_turns = int(extras.get("max_turns", 40))

        def init(
            self, prompt: list[dict[str, str]]
        ) -> tuple[list[dict[str, str]], dict]:
            first = self.session.reset(task_id=self.task_id)
            return [*prompt, {"role": "user", "content": first}], {}

        def step(self, action: str) -> dict[str, Any]:
            self.turns += 1
            try:
                text = self.session.act(json.loads(action))
            except (ValueError, TypeError) as exc:
                text = json.dumps({"error": f"not a valid action: {exc}"})
            done = self.session.done or self.turns >= self.max_turns
            return {
                "observations": [] if done else [{"role": "user", "content": text}],
                "reward": self.session.reward if self.session.done else 0.0,
                "done": done,
                "metadata": {"outcome": self.session.outcome},
            }

        def close(self) -> None:
            self.session.close()

    return OpenAdaptSkyRLEnv


# -- verl ----------------------------------------------------------------------


def verl_tool_class(client_factory: Optional[Callable[[str], Any]] = None) -> type:
    """A verl tool that runs one browser session per trajectory.

    The tool config needs ``base_url``; ``create(task_id=...)`` picks the task.
    Each call's ``action`` parameter is one environment action, and
    ``calc_reward`` returns the episode reward.
    """
    from verl.tools.base_tool import BaseTool
    from verl.tools.schemas import ToolResponse

    class OpenAdaptWebTool(BaseTool):  # type: ignore[misc, valid-type]
        def __init__(self, config: dict, tool_schema: Any) -> None:
            super().__init__(config, tool_schema)
            self.sessions: dict[str, Session] = {}

        async def create(self, instance_id: Optional[str] = None, **kwargs: Any):
            instance_id, _ = await super().create(instance_id)
            session = Session(self.config["base_url"], client_factory)
            self.sessions[instance_id] = session
            first = session.reset(task_id=kwargs.get("task_id"))
            return instance_id, ToolResponse(text=first)

        async def execute(self, instance_id: str, parameters: dict[str, Any], **_: Any):
            session = self.sessions[instance_id]
            try:
                text = session.act(parameters.get("action") or parameters)
            except ValueError as exc:
                text = json.dumps({"error": str(exc)})
            return ToolResponse(text=text), 0.0, {"done": session.done}

        async def calc_reward(self, instance_id: str, **_: Any) -> float:
            return self.sessions[instance_id].reward

        async def release(self, instance_id: str, **_: Any) -> None:
            session = self.sessions.pop(instance_id, None)
            if session is not None:
                session.close()

    return OpenAdaptWebTool

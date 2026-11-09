"""The TRL, SkyRL and verl adapters pass the server's reward through untouched."""

from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace
from typing import Any

import pytest

from openadapt_flow.rl_env import trainers


class FakeClient:
    """Stands in for OpenEnv's sync client: a two-step episode."""

    def __init__(self, reward: float = 1.0) -> None:
        self.reward = reward
        self.actions: list[dict[str, Any]] = []
        self.reset_args: dict[str, Any] = {}
        self.closed = False

    def reset(self, **kwargs: Any) -> Any:
        self.reset_args = kwargs
        return SimpleNamespace(
            observation={"instruction": "do it", "text": "form"},
            done=False,
            reward=None,
            metadata={},
        )

    def step(self, action: dict[str, Any]) -> Any:
        self.actions.append(action)
        done = action["kind"] == "done"
        outcome = {
            "completion": done and self.reward == 1.0,
            "harm": {"silent_wrong_effect": done and self.reward == 0.0},
        }
        return SimpleNamespace(
            observation={"text": "after", "verdict": "CONFIRMED" if done else None},
            done=done,
            reward=self.reward if done else 0.0,
            metadata={"outcome": outcome} if done else {},
        )

    def close(self) -> None:
        self.closed = True


def _factory(client: FakeClient):
    return lambda base_url: client


def test_trl_environment_exposes_documented_tools_and_reads_the_reward() -> None:
    client = FakeClient(reward=1.0)
    cls = trainers.trl_environment("http://env", client_factory=_factory(client))
    env = cls()
    assert "do it" in env.reset(task_id="mockmed-01", prompt="ignored")
    assert client.reset_args == {"task_id": "mockmed-01"}
    tools = [
        n
        for n, m in inspect.getmembers(cls, inspect.isfunction)
        if not n.startswith("_") and n != "reset"
    ]
    assert set(tools) == {"click", "fill", "press", "select", "goto", "done"}
    for name in tools:
        doc = inspect.getdoc(getattr(cls, name)) or ""
        assert "Args:" in doc and "Returns:" in doc, name
    env.fill("#note", "x")
    env.done("success")
    assert trainers.trl_reward([env]) == [1.0]
    assert trainers.trl_harm([env]) == [0.0]
    with pytest.raises(ValueError, match="over"):
        env.click("#again")


def test_trl_harm_flags_a_refuted_success_claim() -> None:
    env = trainers.trl_environment(
        "http://env", client_factory=_factory(FakeClient(reward=0.0))
    )()
    env.reset()
    env.done("success")
    assert trainers.trl_reward([env]) == [0.0] and trainers.trl_harm([env]) == [-1.0]


def test_skyrl_text_env_steps_json_actions() -> None:
    pytest.importorskip("skyrl_gym")
    client = FakeClient(reward=1.0)
    cls = trainers.skyrl_env_class(client_factory=_factory(client))
    env = cls({"base_url": "http://env"}, {"task_id": "mockmed-01"})
    prompt, _ = env.init([{"role": "system", "content": "sys"}])
    assert prompt[-1]["role"] == "user" and "do it" in prompt[-1]["content"]
    out = env.step("not json")
    assert (
        out["done"] is False
        and "not a valid action" in out["observations"][0]["content"]
    )
    out = env.step(json.dumps({"kind": "done", "claim": "success"}))
    assert (out["done"], out["reward"]) == (True, 1.0)
    env.close()
    assert client.closed


def test_verl_tool_runs_one_session_per_instance() -> None:
    pytest.importorskip("verl.tools.base_tool")
    from verl.tools.schemas import OpenAIFunctionToolSchema

    schema = OpenAIFunctionToolSchema.model_validate(
        {
            "type": "function",
            "function": {
                "name": "web",
                "description": "act",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    )
    client = FakeClient(reward=1.0)
    tool = trainers.verl_tool_class(client_factory=_factory(client))(
        {"base_url": "http://env"}, schema
    )

    async def episode() -> float:
        iid, first = await tool.create(task_id="mockmed-01")
        assert "do it" in first.text
        await tool.execute(
            iid, {"action": {"kind": "fill", "selector": "#n", "text": "x"}}
        )
        _, _, info = await tool.execute(
            iid, {"action": {"kind": "done", "claim": "success"}}
        )
        assert info["done"] is True
        reward = await tool.calc_reward(iid)
        await tool.release(iid)
        return reward

    assert asyncio.run(episode()) == 1.0 and client.closed


class DroppingClient(FakeClient):
    """A session whose WebSocket drops on the given step (1-based)."""

    def __init__(self, drop_on: int, reward: float = 1.0) -> None:
        super().__init__(reward=reward)
        self.drop_on = drop_on

    def step(self, action: dict[str, Any]) -> Any:
        if len(self.actions) + 1 == self.drop_on:
            self.actions.append(action)
            raise ConnectionError("websocket closed: 1006")
        return super().step(action)


def test_a_dropped_session_fails_closed_with_reward_zero() -> None:
    client = DroppingClient(drop_on=2)
    env = trainers.trl_environment("http://env", client_factory=_factory(client))()
    env.reset(task_id="mockmed-01")
    env.fill("#note", "x")
    # The done step would have earned 1.0, but the session dropped under it.
    with pytest.raises(trainers.SessionDropped, match="1006"):
        env.done("success")
    assert trainers.trl_reward([env]) == [0.0]
    assert env.session.done is True and client.closed
    assert env.outcome["dropped"]["during"] == "step"
    assert "completion" not in env.outcome  # nothing read from the record
    with pytest.raises(ValueError, match="over"):
        env.click("#again")  # the dropped episode cannot continue


def test_a_session_that_drops_at_reset_fails_closed() -> None:
    class DeadOnReset(FakeClient):
        def reset(self, **kwargs: Any) -> Any:
            raise OSError("connection refused")

    client = DeadOnReset()
    session = trainers.Session("http://env", client_factory=_factory(client))
    with pytest.raises(trainers.SessionDropped, match="reset"):
        session.reset(task_id="mockmed-01")
    assert (session.done, session.reward) == (True, 0.0) and client.closed
    with pytest.raises(ValueError, match="over"):
        session.act({"kind": "click", "selector": "#x"})


def test_a_malformed_server_reply_fails_closed() -> None:
    class Garbled(FakeClient):
        def step(self, action: dict[str, Any]) -> Any:
            return SimpleNamespace(observation=None, done=True, reward=1.0)

    session = trainers.Session("http://env", client_factory=_factory(Garbled()))
    session.reset()
    with pytest.raises(trainers.SessionDropped):
        session.act({"kind": "done", "claim": "success"})
    assert session.reward == 0.0


def test_each_rollout_gets_its_own_websocket_session() -> None:
    made: list[FakeClient] = []

    def factory(base_url: str) -> FakeClient:
        made.append(DroppingClient(drop_on=1) if not made else FakeClient())
        return made[-1]

    env = trainers.trl_environment("http://env", client_factory=factory)()
    env.reset(task_id="mockmed-01")
    with pytest.raises(trainers.SessionDropped):
        env.click("#x")
    # The next rollout opens a fresh session and is not tainted by the drop.
    env.reset(task_id="mockmed-01")
    assert len(made) == 2 and made[0].closed and not made[1].closed
    assert env.outcome == {} and env.session.done is False
    env.done("success")
    assert trainers.trl_reward([env]) == [1.0]
    env.reset(task_id="mockmed-02")
    assert len(made) == 3 and made[1].closed


def test_a_non_object_action_is_refused_before_it_reaches_the_server() -> None:
    client = FakeClient()
    session = trainers.Session("http://env", client_factory=_factory(client))
    session.reset()
    with pytest.raises(TypeError):
        session.act(["click"])  # type: ignore[arg-type]
    assert client.actions == [] and session.done is False

"""MockMed as an RL environment, end to end in a real browser.

The scripted solution earns 1 on a clean save and on an honest failure
report under a dropped note; trusting the "saved" banner earns 0 there, as
does reporting failure without trying; and the agent's browser cannot reach
the record the reward reads.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("playwright")
pytest.importorskip("openenv")

from openadapt_flow.rl_env.environment import (  # noqa: E402
    OpenAdaptWebEnvironment,
    WebAction,
)

MOCKMED = Path(__file__).resolve().parents[2] / "environments" / "openenv" / "mockmed"


@pytest.fixture(scope="module")
def env():
    environment = OpenAdaptWebEnvironment(MOCKMED)
    yield environment
    environment.close()


def _play(env, task_id: str, claim: str | None = None):
    task = env.env.task(task_id)
    obs = env.reset(task_id=task_id)
    assert "fault" not in obs.url and obs.instruction == task.instruction
    seen = obs.model_dump()
    assert "task_id" not in seen and f"fault={task.fault}" not in json.dumps(seen)
    for action in task.solution:
        if claim and action["kind"] == "done":
            action = {"kind": "done", "claim": claim}
        obs = env.step(WebAction(**action))
    assert obs.done
    return obs


def test_clean_save_is_confirmed(env) -> None:
    obs = _play(env, "mockmed-01")
    assert (obs.verdict, obs.reward) == ("CONFIRMED", 1.0)


def test_dropped_note_pays_only_the_honest_report(env) -> None:
    honest = _play(env, "mockmed-07")
    assert (honest.verdict, honest.reward) == ("REFUTED", 1.0)
    trusting = _play(env, "mockmed-07", claim="success")
    assert (trusting.verdict, trusting.reward) == ("REFUTED", 0.0)
    assert trusting.metadata["judgement"]["silent_wrong_effect"] is True


def test_failure_report_without_trying_earns_nothing(env) -> None:
    env.reset(task_id="mockmed-07")
    obs = env.step(WebAction(kind="done", claim="failure"))
    assert (obs.verdict, obs.reward) == ("REFUTED", 0.0)
    assert obs.metadata["judgement"]["attempted"] is False


def test_agent_cannot_read_or_reset_the_record(env) -> None:
    env.reset(task_id="mockmed-01")
    obs = env.step(WebAction(kind="goto", path="/api/db"))
    assert obs.last_action_error
    assert '"records"' not in obs.text
    obs = env.step(WebAction(kind="goto", path="http://127.0.0.1:1/x"))
    assert "path on the app's origin" in (obs.last_action_error or "")
    assert obs.reward == 0.0 and not obs.done


def test_reset_leaves_no_state_from_the_last_episode(env) -> None:
    import httpx

    from openadapt_flow.rl_env.agents import RandomAgent

    obs = env.reset(task_id="mockmed-05")
    policy = RandomAgent(7, steps=10)
    step = 0
    while not obs.done:
        obs = env.step(WebAction(**policy.act(obs.model_dump(), step)))
        step += 1
    _play(env, "mockmed-01")
    env.reset(task_id="mockmed-11")
    rows = httpx.get(env._app_url + "/api/db").json()["records"]
    # Only the concurrent colleague's row this task seeds; nothing carried over.
    assert [r["source"] for r in rows] == ["other"]

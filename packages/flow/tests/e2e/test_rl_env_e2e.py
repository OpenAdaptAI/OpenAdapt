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


def test_mcp_tools_and_typed_actions_reach_the_same_reward(env) -> None:
    from openenv.core.env_server.mcp_types import CallToolAction, ListToolsAction

    assert {t.name for t in env.step(ListToolsAction()).tools} >= {"click", "done"}
    obs = env.reset(task_id="mockmed-01")
    node = {n.get("css_selector"): n["node_id"] for n in obs.computer_state["nodes"]}
    obs = env.step(
        WebAction(
            computer_action={"type": "click", "target": {"node_id": node["#signin"]}}
        )
    )
    assert obs.action_result["success"] is True
    task = env.env.task("mockmed-01")
    for action in task.solution[3:]:
        args = {k: v for k, v in action.items() if k != "kind"}
        result = env.step(CallToolAction(tool_name=action["kind"], arguments=args))
        assert result.error is None
    assert (result.done, result.reward, result.result["verdict"]) == (
        True,
        1.0,
        "CONFIRMED",
    )


def _typed(raw: dict) -> WebAction:
    return WebAction(computer_action=raw)


def test_invalid_typed_actions_spend_the_step_budget(env, monkeypatch) -> None:
    monkeypatch.setattr(env.env.manifest.agent, "max_steps", 2)
    env.reset(task_id="mockmed-01")
    bad = _typed({"type": "open_app", "app_name": "x"})
    first = env.step(bad)
    assert (first.done, first.reward) == (False, 0.0)
    assert "not available" in (first.last_action_error or "")
    second = env.step(bad)
    assert second.done is True and env.state.done is True
    assert env.state.step_count == 2
    third = env.step(bad)
    assert third.done is True and third.last_action_error == "episode is over"
    assert env.state.step_count == 2


def test_an_invalid_typed_action_after_the_episode_ends_stays_done(env) -> None:
    env.reset(task_id="mockmed-01")
    assert env.step(WebAction(kind="done", claim="failure")).done is True
    steps = env.state.step_count
    for raw in (
        {"type": "open_app", "app_name": "x"},
        {"type": "click", "target": {"node_id": "n999"}},
    ):
        obs = env.step(_typed(raw))
        assert obs.done is True and env.state.done is True
        assert obs.reward == 0.0 and obs.last_action_error == "episode is over"
    assert env.state.step_count == steps


def test_a_double_click_is_refused_not_sent_as_one_click(env) -> None:
    obs = env.reset(task_id="mockmed-01")
    node = next(
        n for n in obs.computer_state["nodes"] if n.get("css_selector") == "#signin"
    )
    selector = node["css_selector"]
    # The browser lives on the environment's own thread.
    env._on_worker(
        lambda js, arg: env._page.evaluate(js, arg),
        """(sel) => {
          window.__clicks = 0; window.__dbl = 0;
          const el = document.querySelector(sel);
          el.addEventListener('click', () => { window.__clicks += 1; });
          el.addEventListener('dblclick', () => { window.__dbl += 1; });
        }""",
        selector,
    )
    obs = env.step(
        _typed({"type": "double_click", "target": {"node_id": node["node_id"]}})
    )
    assert obs.action_result["success"] is False
    assert "not available" in (obs.last_action_error or "")
    counts = env._on_worker(
        lambda: env._page.evaluate("() => [window.__clicks, window.__dbl]")
    )
    assert counts == [0, 0]
    assert obs.done is False


def test_a_stuck_step_that_wakes_later_cannot_touch_the_next_episode(
    monkeypatch,
) -> None:
    import os
    import threading
    import time

    import httpx

    from openadapt_flow.rl_env import environment as E

    env = OpenAdaptWebEnvironment(MOCKMED)
    try:
        env.reset(task_id="mockmed-01")
        old_url, driver = env._app_url, env._driver_pid
        assert driver is not None
        task = env.env.task("mockmed-01")
        woke = threading.Event()

        def stuck_then_late_write(*_a, **_k):
            time.sleep(2)  # past the watchdog, then the "browser" finally saves
            try:
                httpx.post(
                    f"{old_url}/api/encounter?fault=ok", json=task.params, timeout=2
                )
            except httpx.HTTPError:
                pass
            woke.set()

        monkeypatch.setattr(E, "STEP_TIMEOUT_S", 0.5)
        env._step = stuck_then_late_write  # type: ignore[method-assign]
        obs = env.step(WebAction(kind="wait", seconds=1))
        assert (obs.done, obs.reward, obs.verdict) == (True, 0.0, "INDETERMINATE")
        with pytest.raises(ProcessLookupError):
            os.kill(driver, 0)  # the stuck browser's driver is gone
        del env._step
        monkeypatch.undo()  # the new episode runs under the normal watchdog

        env.reset(task_id="mockmed-01")
        assert env._app_url != old_url  # a fresh app copy and store
        assert woke.wait(10)
        assert httpx.get(env._app_url + "/api/db").json()["records"] == []
        for action in task.solution:
            obs = env.step(WebAction(**action))
        assert (obs.verdict, obs.reward) == ("CONFIRMED", 1.0)
    finally:
        env.close()


def test_trl_adapter_over_a_live_server_reads_the_record_reward() -> None:
    from openadapt_flow.rl_env.trainers import trl_environment, trl_reward
    from openadapt_flow.rl_env.validate import _free_port, start_server

    port = _free_port()
    server = start_server(MOCKMED, port)
    try:
        env = trl_environment(f"http://127.0.0.1:{port}")()
        assert "MockMed" in env.reset(task_id="mockmed-01")
        task = OpenAdaptWebEnvironment(MOCKMED).env.task("mockmed-01")
        for action in task.solution[:-1]:
            if action["kind"] == "fill":
                env.fill(action["selector"], action["text"])
            elif action["kind"] == "click":
                env.click(action["selector"])
            else:
                env.session.act(action)
        env.done("success")
        assert trl_reward([env]) == [1.0] and env.outcome["completion"] is True
        env.session.close()
    finally:
        server.terminate()
        server.wait(timeout=30)

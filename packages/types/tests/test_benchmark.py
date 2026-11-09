"""Tests for the canonical Benchmark* vocabulary."""

from openadapt_types import (
    BenchmarkAction,
    BenchmarkAgent,
    BenchmarkObservation,
    BenchmarkTask,
)


def test_benchmark_task_defaults():
    task = BenchmarkTask(task_id="t1", instruction="do it", domain="desktop")
    assert task.task_id == "t1"
    assert task.instruction == "do it"
    assert task.domain == "desktop"
    assert task.initial_state_ref is None
    assert task.time_limit_steps is None
    assert task.raw_config == {}
    assert task.evaluation_spec is None


def test_benchmark_task_raw_config_is_independent():
    a = BenchmarkTask(task_id="a", instruction="i", domain="web")
    b = BenchmarkTask(task_id="b", instruction="i", domain="web")
    a.raw_config["k"] = "v"
    assert b.raw_config == {}


def test_benchmark_observation_defaults():
    obs = BenchmarkObservation()
    assert obs.screenshot is None
    assert obs.viewport is None
    assert obs.raw_observation is None


def test_benchmark_observation_fields():
    obs = BenchmarkObservation(
        screenshot=b"png",
        viewport=(1920, 1080),
        url="https://example.com",
        window_title="Notepad",
    )
    assert obs.screenshot == b"png"
    assert obs.viewport == (1920, 1080)
    assert obs.url == "https://example.com"
    assert obs.window_title == "Notepad"


def test_benchmark_action_click():
    action = BenchmarkAction(type="click", x=0.5, y=0.25)
    assert action.type == "click"
    assert action.x == 0.5
    assert action.y == 0.25
    assert action.text is None


def test_benchmark_action_type_and_key():
    typing = BenchmarkAction(type="type", text="hello")
    assert typing.text == "hello"
    key = BenchmarkAction(type="key", key="Enter", modifiers=["ctrl"])
    assert key.key == "Enter"
    assert key.modifiers == ["ctrl"]


def test_benchmark_agent_is_abstract():
    # BenchmarkAgent is an ABC; instantiating directly must fail.
    try:
        BenchmarkAgent()  # type: ignore[abstract]
    except TypeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("BenchmarkAgent should be abstract")


def test_benchmark_agent_subclass_roundtrip():
    class Echo(BenchmarkAgent):
        def act(self, observation, task, history=None):
            return BenchmarkAction(type="done")

    agent = Echo()
    obs = BenchmarkObservation()
    task = BenchmarkTask(task_id="t", instruction="i", domain="desktop")
    action = agent.act(obs, task)
    assert action.type == "done"
    # Default reset is a no-op and must not raise.
    agent.reset()

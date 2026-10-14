"""Tests for OpenEnv-compatible WAA desktop environment."""

from __future__ import annotations

import base64
import textwrap
from unittest.mock import MagicMock

from openadapt_evals.adapters.base import (
    BenchmarkObservation,
    BenchmarkResult,
    BenchmarkTask,
)
from openadapt_evals.openenv.environment import WAAOpenEnvEnvironment
from openadapt_evals.openenv.models import WAAAction, WAAObservation, WAAState


def _make_mock_adapter():
    adapter = MagicMock()
    _fake_obs = BenchmarkObservation(
        screenshot=b"\x89PNG\r\n\x1a\n" + b"\x00" * 100,
        viewport=(1280, 720),
        raw_observation={},
    )
    adapter.observe.return_value = _fake_obs
    adapter.step.return_value = (_fake_obs, False, {})
    adapter.reset.return_value = _fake_obs
    adapter.pixel_action.return_value = (_fake_obs, False, {})
    adapter.load_task.return_value = BenchmarkTask(
        task_id="test-task", instruction="Test", domain="desktop"
    )
    adapter.load_task_from_json.return_value = BenchmarkTask(
        task_id="test-task", instruction="Test", domain="desktop"
    )
    adapter.evaluate.return_value = BenchmarkResult(
        task_id="test-task", success=False, score=0.0
    )
    adapter.config = MagicMock(server_url="http://mock:5001")
    adapter.screen_size = (1280, 720)
    return adapter


class TestWAAOpenEnvModels:
    def test_action_creation(self):
        action = WAAAction(type="click", x=0.5, y=0.3)
        assert action.type == "click"
        assert action.x == 0.5

    def test_observation_creation(self):
        obs = WAAObservation(
            screenshot_b64="aGVsbG8=",
            done=False,
            reward=0.5,
            step_index=3,
        )
        assert obs.screenshot_b64 == "aGVsbG8="
        assert obs.reward == 0.5
        assert obs.step_index == 3

    def test_state_creation(self):
        state = WAAState(
            episode_id="ep-001",
            step_count=5,
            task_id="test",
            status="running",
        )
        assert state.episode_id == "ep-001"
        assert state.step_count == 5

    def test_action_done(self):
        action = WAAAction(type="done")
        assert action.type == "done"
        assert action.x is None

    def test_action_type_text(self):
        action = WAAAction(type="type", text="hello world")
        assert action.text == "hello world"

    def test_observation_serialization(self):
        obs = WAAObservation(screenshot_b64="abc", done=True, reward=1.0)
        data = obs.model_dump()
        assert data["screenshot_b64"] == "abc"
        assert data["done"] is True
        assert data["reward"] == 1.0

    def test_state_serialization(self):
        state = WAAState(task_id="t1", milestones_passed=2, milestones_total=5)
        data = state.model_dump()
        assert data["milestones_passed"] == 2


def _make_env_with_mock(max_steps=15):
    """Create a WAAOpenEnvEnvironment with a pre-injected mock RLEnvironment."""
    from openadapt_evals.adapters.rl_env import RLEnvironment

    adapter = _make_mock_adapter()
    rl_env = RLEnvironment(adapter)

    env = WAAOpenEnvEnvironment(
        server_url="http://mock:5001",
        default_task_id="test-task",
        max_steps=max_steps,
    )
    env._rl_env = rl_env  # inject mock, skip lazy init
    return env


class TestWAAOpenEnvEnvironment:
    def test_reset_returns_observation(self):
        env = _make_env_with_mock()
        obs = env.reset(task_id="test-task")

        assert isinstance(obs, WAAObservation)
        assert obs.screenshot_b64 is not None
        assert obs.done is False
        assert env.state.status == "running"
        assert env.state.step_count == 0

    def test_step_returns_observation(self):
        env = _make_env_with_mock()
        env.reset(task_id="test-task")
        obs = env.step(WAAAction(type="click", x=0.5, y=0.3))

        assert isinstance(obs, WAAObservation)
        assert env.state.step_count == 1

    def test_done_action_triggers_evaluation(self):
        env = _make_env_with_mock()
        # Make step return done
        env._rl_env._adapter.step.return_value = (
            BenchmarkObservation(screenshot=b"\x89PNG" + b"\x00" * 50, raw_observation={}),
            True,
            {},
        )
        env.reset(task_id="test-task")
        obs = env.step(WAAAction(type="done"))

        assert obs.done is True
        assert env.state.status == "completed"
        assert env.state.score is not None

    def test_max_steps_triggers_done(self):
        env = _make_env_with_mock(max_steps=2)
        env.reset(task_id="test-task")
        env.step(WAAAction(type="click", x=0.1, y=0.1))
        obs = env.step(WAAAction(type="click", x=0.2, y=0.2))

        assert obs.done is True
        assert env.state.step_count == 2

    def test_state_initial(self):
        env = WAAOpenEnvEnvironment()
        assert env.state.status == "idle"
        assert env.state.step_count == 0
        assert env.state.done is False

    def test_close(self):
        env = WAAOpenEnvEnvironment()
        env.close()
        assert env._rl_env is None

    def test_screenshot_base64_encoding(self):
        """Verify screenshots are properly base64-encoded."""
        raw_png = b"\x89PNG\r\n\x1a\n" + b"\x42" * 50
        expected_b64 = base64.b64encode(raw_png).decode()

        env = WAAOpenEnvEnvironment()
        obs = env._to_observation(
            BenchmarkObservation(screenshot=raw_png, raw_observation={})
        )
        assert obs.screenshot_b64 == expected_b64

    def test_to_observation_no_screenshot(self):
        env = WAAOpenEnvEnvironment()
        obs = env._to_observation(
            BenchmarkObservation(screenshot=None, raw_observation={})
        )
        assert obs.screenshot_b64 is None

    def test_task_config_loading(self, tmp_path):
        task_yaml = tmp_path / "test.yaml"
        task_yaml.write_text(
            textwrap.dedent("""\
            name: "Test task"
            id: test-task
            evaluate:
              - check: screenshot
                description: "ok"
            milestones:
              - name: "M1"
                check: screenshot
                description: "step done"
            """)
        )

        env = WAAOpenEnvEnvironment(task_config_dir=str(tmp_path))
        assert "test-task" in env._task_configs
        assert "Test task" in env._task_configs


class TestWAAOpenEnvProtocol:
    """Verify the environment follows OpenEnv's duck-typed protocol."""

    def test_has_reset_method(self):
        env = WAAOpenEnvEnvironment()
        assert callable(getattr(env, "reset", None))

    def test_has_step_method(self):
        env = WAAOpenEnvEnvironment()
        assert callable(getattr(env, "step", None))

    def test_has_state_property(self):
        env = WAAOpenEnvEnvironment()
        assert hasattr(env, "state")
        state = env.state
        assert isinstance(state, WAAState)

    def test_has_close_method(self):
        env = WAAOpenEnvEnvironment()
        assert callable(getattr(env, "close", None))

    def test_has_supports_concurrent_sessions(self):
        assert WAAOpenEnvEnvironment.SUPPORTS_CONCURRENT_SESSIONS is False

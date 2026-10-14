"""Tests for the VAGEN/verl-agent environment adapter.

Verifies that WAADesktopEnv implements the GymImageEnv protocol correctly
using the WAAMockAdapter (no VM required).
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from openadapt_evals.adapters.base import BenchmarkObservation
from openadapt_evals.adapters.rl_env import RLEnvironment
from openadapt_evals.adapters.verl_env import (
    _ACTION_PATTERN,
    ENV_CLASS_PATH,
    ENV_REGISTRY_KEY,
    WAADesktopEnv,
    _build_obs_dict,
    _parse_action_str,
    generate_env_spec,
    register_in_vagen,
)
from openadapt_evals.adapters.waa.mock import WAAMockAdapter
from openadapt_evals.errors import ActionParseError

# --- Action parsing tests ---


class TestParseActionStr:
    def test_click(self):
        action = _parse_action_str("CLICK(x=0.50, y=0.30)")
        assert action.type == "click"
        assert action.x == pytest.approx(0.50)
        assert action.y == pytest.approx(0.30)

    def test_type(self):
        action = _parse_action_str('TYPE(text="hello world")')
        assert action.type == "type"
        assert action.text == "hello world"

    def test_type_escaped_quotes(self):
        action = _parse_action_str(r'TYPE(text="say \"hi\"")')
        assert action.type == "type"
        assert action.text == r'say \"hi\"'

    def test_key(self):
        action = _parse_action_str('KEY(key="enter")')
        assert action.type == "key"
        assert action.key == "enter"

    def test_wait(self):
        action = _parse_action_str("WAIT()")
        assert action.type == "wait"

    def test_done(self):
        action = _parse_action_str("DONE()")
        assert action.type == "done"

    def test_scroll_with_direction(self):
        action = _parse_action_str('SCROLL(x=0.50, y=0.50, direction="down")')
        assert action.type == "scroll"
        assert action.scroll_direction == "down"

    def test_scroll_up(self):
        action = _parse_action_str('SCROLL(x=0.50, y=0.50, direction="up")')
        assert action.scroll_direction == "up"

    def test_scroll_requires_direction(self):
        with pytest.raises(ActionParseError, match="direction"):
            _parse_action_str("SCROLL(x=0.50, y=0.50)")

    def test_invalid_is_not_task_completion(self):
        with pytest.raises(ActionParseError):
            _parse_action_str("random garbage text")

    def test_drag_with_end_coords(self):
        action = _parse_action_str("DRAG(x=0.20, y=0.30, end_x=0.80, end_y=0.70)")
        assert action.type == "drag"
        assert action.x == pytest.approx(0.20)
        assert action.y == pytest.approx(0.30)
        assert action.end_x == pytest.approx(0.80)
        assert action.end_y == pytest.approx(0.70)

    def test_drag_without_end_coords(self):
        with pytest.raises(ActionParseError, match="end_x"):
            _parse_action_str("DRAG(x=0.20, y=0.30)")

    @pytest.mark.parametrize(
        "text",
        [
            "CLICK(x=0.5)",
            "CLICK(x=-0.1, y=0.5)",
            "CLICK(x=nan, y=0.5)",
            "SCROLL(x=0.5)",
            'SCROLL(x=0.5, y=0.5, direction="left")',
            "TYPE()",
            "KEY()",
        ],
    )
    def test_missing_or_invalid_arguments_are_rejected(self, text):
        with pytest.raises(ActionParseError):
            _parse_action_str(text)

    def test_with_thinking(self):
        action = _parse_action_str(
            "<think>I need to click the button</think>\nCLICK(x=0.25, y=0.75)"
        )
        assert action.type == "click"
        assert action.x == pytest.approx(0.25)
        assert action.y == pytest.approx(0.75)

    def test_invalid_action_not_matched(self):
        """Unparseable input should not match the action pattern."""
        assert _ACTION_PATTERN.search("random garbage") is None

    def test_explicit_done_is_matched(self):
        """Explicit DONE() should match the action pattern."""
        assert _ACTION_PATTERN.search("DONE()") is not None


# --- Observation building tests ---


class TestBuildObsDict:
    def test_with_screenshot(self):
        """Test obs dict with PNG bytes."""
        import io

        from PIL import Image

        img = Image.new("RGB", (100, 100), color="red")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_bytes = buf.getvalue()

        from openadapt_evals.adapters.base import BenchmarkObservation

        obs = BenchmarkObservation(screenshot=png_bytes)
        result = _build_obs_dict(obs, prefix="Test:")

        assert "<image>" in result["obs_str"]
        assert "multi_modal_input" in result
        assert "<image>" in result["multi_modal_input"]
        assert len(result["multi_modal_input"]["<image>"]) == 1

    def test_without_screenshot(self):
        """Test obs dict falls back to a11y text."""
        from openadapt_evals.adapters.base import BenchmarkObservation

        obs = BenchmarkObservation(
            screenshot=None, accessibility_tree={"role": "window", "name": "Desktop"}
        )
        result = _build_obs_dict(obs, prefix="Test:")

        assert "<image>" not in result["obs_str"]
        assert "multi_modal_input" not in result
        assert "window" in result["obs_str"]


# --- WAADesktopEnv protocol tests ---


def _make_mock_env() -> WAADesktopEnv:
    """Create a WAADesktopEnv with a mock adapter injected."""
    mock_adapter = WAAMockAdapter(num_tasks=5)
    task_id = mock_adapter.list_tasks()[0].task_id

    env = WAADesktopEnv.__new__(WAADesktopEnv)
    env.config = {}
    env._server_url = "mock"
    env._task_id = task_id
    env._max_steps = 15
    env._evaluate_at_done = True
    env._use_fractional = True
    env._step_count = 0

    # Inject mock adapter instead of creating a real WAALiveAdapter
    env._rl_env = RLEnvironment(mock_adapter, default_task_id=task_id)
    return env


class TestWAADesktopEnv:
    def test_system_prompt(self):
        env = _make_mock_env()
        result = asyncio.run(env.system_prompt())
        assert "obs_str" in result
        assert "CLICK" in result["obs_str"]
        assert "TYPE" in result["obs_str"]
        assert "DRAG" in result["obs_str"]
        assert "DONE" in result["obs_str"]

    def test_reset_returns_obs_dict(self):
        env = _make_mock_env()
        obs, info = asyncio.run(env.reset(seed=42))
        assert "obs_str" in obs
        assert isinstance(info, dict)

    def test_step_click(self):
        env = _make_mock_env()
        asyncio.run(env.reset(seed=42))
        obs, reward, done, info = asyncio.run(env.step("CLICK(x=0.10, y=0.10)"))
        assert "obs_str" in obs
        assert isinstance(reward, float)
        assert isinstance(done, bool)

    def test_step_type(self):
        env = _make_mock_env()
        asyncio.run(env.reset(seed=42))
        obs, reward, done, info = asyncio.run(env.step('TYPE(text="hello")'))
        assert not done
        assert reward == 0.0

    def test_step_done_triggers_eval(self):
        env = _make_mock_env()
        asyncio.run(env.reset(seed=42))
        obs, reward, done, info = asyncio.run(env.step("DONE()"))
        assert done is True
        assert isinstance(reward, float)

    def test_step_does_not_turn_evaluator_failure_into_zero_reward(self):
        env = _make_mock_env()
        asyncio.run(env.reset(seed=42))
        env._rl_env.evaluate = MagicMock(side_effect=RuntimeError("evaluator offline"))

        with pytest.raises(RuntimeError, match="evaluator offline"):
            asyncio.run(env.step("DONE()"))

    def test_partial_reward_is_not_reported_as_success(self):
        env = _make_mock_env()
        asyncio.run(env.reset(seed=42))
        env._rl_env.evaluate = MagicMock(return_value=0.75)

        _, reward, done, info = asyncio.run(env.step("DONE()"))

        assert done is True
        assert reward == 0.75
        assert info["success"] is False

    @pytest.mark.parametrize(
        "action_text, expected",
        [
            ("CLICK(x=1.0, y=1.0)", (1919, 1199, None, None)),
            (
                "DRAG(x=1.0, y=1.0, end_x=1.0, end_y=1.0)",
                (1919, 1199, 1919, 1199),
            ),
        ],
    )
    def test_fractional_edge_stays_inside_viewport(self, action_text, expected):
        env = _make_mock_env()
        asyncio.run(env.reset(seed=42))

        asyncio.run(env.step(action_text))

        action = env._rl_env.trajectory[-1].action
        assert (action.x, action.y, action.end_x, action.end_y) == expected

    def test_fractional_action_rejects_invalid_viewport_dimensions(self):
        env = _make_mock_env()
        asyncio.run(env.reset(seed=42))
        env._rl_env._last_obs = BenchmarkObservation(viewport=(0, 1200))

        with pytest.raises(ValueError, match="dimension must be positive"):
            asyncio.run(env.step("CLICK(x=0.5, y=0.5)"))

    def test_max_steps_triggers_done(self):
        env = _make_mock_env()
        env._max_steps = 3
        asyncio.run(env.reset(seed=42))

        for i in range(3):
            obs, reward, done, info = asyncio.run(
                env.step(f"CLICK(x=0.{i+1}0, y=0.{i+1}0)")
            )

        assert done is True

    def test_close(self):
        env = _make_mock_env()
        asyncio.run(env.close())
        assert env._rl_env is None

    def test_full_episode_flow(self):
        """Test a complete episode: reset -> multiple steps -> done -> evaluate."""
        env = _make_mock_env()
        env._max_steps = 5

        obs, info = asyncio.run(env.reset(seed=1))
        assert "obs_str" in obs

        obs, r, done, _ = asyncio.run(env.step("CLICK(x=0.05, y=0.08)"))
        assert not done
        assert r == 0.0

        obs, r, done, _ = asyncio.run(env.step('TYPE(text="test input")'))
        assert not done
        assert r == 0.0

        obs, r, done, info = asyncio.run(env.step("DONE()"))
        assert done
        assert isinstance(r, float)

    def test_protocol_has_required_methods(self):
        """Verify WAADesktopEnv has all GymImageEnv protocol methods."""
        env = _make_mock_env()
        for method in ("reset", "step", "close", "system_prompt", "health_check"):
            assert hasattr(env, method)
            assert callable(getattr(env, method))

    def test_is_action_valid_on_good_action(self):
        """Valid actions (parseable) should have is_action_valid=True."""
        env = _make_mock_env()
        asyncio.run(env.reset(seed=42))
        _, _, _, info = asyncio.run(env.step("CLICK(x=0.5, y=0.5)"))
        assert info["is_action_valid"] is True

    def test_is_action_valid_on_done(self):
        """Explicit DONE() should have is_action_valid=True."""
        env = _make_mock_env()
        asyncio.run(env.reset(seed=42))
        _, _, _, info = asyncio.run(env.step("DONE()"))
        assert info["is_action_valid"] is True

    def test_garbage_does_not_end_the_episode(self):
        env = _make_mock_env()
        asyncio.run(env.reset(seed=42))
        with pytest.raises(ActionParseError):
            asyncio.run(env.step("random garbage text"))

    def test_obs_contains_image_placeholder(self):
        """Test that observations with screenshots include <image> placeholder."""
        env = _make_mock_env()
        obs, _ = asyncio.run(env.reset(seed=42))
        assert "obs_str" in obs
        assert isinstance(obs["obs_str"], str)

    # --- health_check tests ---

    def test_health_check_not_initialized(self):
        """Health check before reset returns not_initialized."""
        env = WAADesktopEnv.__new__(WAADesktopEnv)
        env._rl_env = None
        env._server_url = "mock"
        env._step_count = 0
        result = asyncio.run(env.health_check())
        assert result["status"] == "not_initialized"

    def test_health_check_ready_after_episode(self):
        """Health check after completed episode returns ready."""
        env = _make_mock_env()
        asyncio.run(env.reset(seed=42))
        asyncio.run(env.step("DONE()"))
        result = asyncio.run(env.health_check())
        assert result["status"] == "ready"

    def test_health_check_busy_mid_episode(self):
        """Health check mid-episode returns busy."""
        env = _make_mock_env()
        asyncio.run(env.reset(seed=42))
        asyncio.run(env.step("CLICK(x=0.5, y=0.5)"))
        result = asyncio.run(env.health_check())
        assert result["status"] == "busy"



# --- VAGEN registration helpers tests ---


class TestGenerateEnvSpec:
    def test_default_spec(self):
        spec = generate_env_spec()
        assert spec["name"] == ENV_REGISTRY_KEY
        assert spec["n_envs"] == 8
        assert spec["max_turns"] == 15
        assert spec["config"]["server_url"] == "http://localhost:5000"
        assert spec["config"]["action_type"] == "fractional"

    def test_custom_spec(self):
        spec = generate_env_spec(
            server_url="http://10.0.0.5:5001",
            task_id="abc-123",
            n_envs=4,
            max_turns=20,
        )
        assert spec["config"]["server_url"] == "http://10.0.0.5:5001"
        assert spec["config"]["task_id"] == "abc-123"
        assert spec["n_envs"] == 4
        assert spec["max_turns"] == 20
        assert spec["config"]["max_steps"] == 20


class TestRegisterInVagen:
    def test_register_creates_yaml_entry(self, tmp_path):
        """Test that register_in_vagen adds entry to env_registry.yaml."""
        registry = tmp_path / "env_registry.yaml"
        registry.write_text("env_registry:\n  Sokoban: vagen.envs.sokoban.Sokoban\n")

        result = register_in_vagen(registry)
        assert result is True

        content = registry.read_text()
        assert ENV_REGISTRY_KEY in content
        assert ENV_CLASS_PATH in content
        # Existing entries preserved
        assert "Sokoban" in content

    def test_register_idempotent(self, tmp_path):
        """Test that registering twice doesn't duplicate the entry."""
        registry = tmp_path / "env_registry.yaml"
        registry.write_text(
            f"env_registry:\n  {ENV_REGISTRY_KEY}: {ENV_CLASS_PATH}\n"
        )

        result = register_in_vagen(registry)
        assert result is True
        # Key should appear exactly once as a YAML key (indented with colon)
        content = registry.read_text()
        assert content.count(f"  {ENV_REGISTRY_KEY}:") == 1

    def test_register_no_file_returns_false(self):
        """Test that missing file returns False."""
        result = register_in_vagen("/nonexistent/path/env_registry.yaml")
        assert result is False

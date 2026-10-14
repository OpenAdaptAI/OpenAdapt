"""End-to-end tests for the DemoExecutor.

Exercises the full run() pipeline with a mock WAA environment.
No GPU, no WAA server, no API keys required.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from openadapt_evals.adapters.base import BenchmarkAction, BenchmarkObservation
from openadapt_evals.adapters.rl_env import ResetConfig, RolloutStep
from openadapt_evals.agents.demo_executor import DemoExecutor
from openadapt_evals.demo_library import Demo, DemoStep
from openadapt_evals.errors import ActionExecutionError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_png_bytes(width: int = 10, height: int = 10) -> bytes:
    """Create a minimal valid PNG image as bytes."""
    from PIL import Image

    img = Image.new("RGB", (width, height), color=(128, 128, 128))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


_SCREENSHOT = _make_png_bytes()


@dataclass
class _TaskConfig:
    """Minimal stand-in for TaskConfig used in DemoExecutor.run()."""

    id: str = "test-task-1"
    name: str = "Test Task"
    milestones: list[Any] = field(default_factory=list)


class MockEnv:
    """Mock RLEnvironment that records dispatched actions.

    Provides the same surface area that DemoExecutor.run() calls:
    reset(), step(), pixel_action(), evaluate(), evaluate_dense(),
    check_milestones_incremental().
    """

    def __init__(self, screenshot: bytes = _SCREENSHOT) -> None:
        self._screenshot = screenshot
        self.actions: list[dict[str, Any]] = []

    def _obs(self) -> BenchmarkObservation:
        return BenchmarkObservation(screenshot=self._screenshot)

    def _rollout_step(self, action: BenchmarkAction) -> RolloutStep:
        return RolloutStep(
            observation=self._obs(),
            action=action,
            reward=0.0,
            done=False,
        )

    # --- methods called by DemoExecutor.run() ---

    def reset(self, config: ResetConfig | None = None) -> BenchmarkObservation:
        return self._obs()

    def step(self, action: BenchmarkAction) -> RolloutStep:
        self.actions.append({
            "method": "step",
            "type": action.type,
            "key": action.key,
            "text": action.text,
            "x": action.x,
            "y": action.y,
        })
        return self._rollout_step(action)

    def pixel_action(self, **kwargs: Any) -> RolloutStep:
        self.actions.append({"method": "pixel_action", **kwargs})
        action = BenchmarkAction(
            type=kwargs.get("action_type", "click"),
            x=kwargs.get("x_frac") or kwargs.get("x"),
            y=kwargs.get("y_frac") or kwargs.get("y"),
            text=kwargs.get("text"),
            key=kwargs.get("key"),
        )
        return self._rollout_step(action)

    def evaluate(self) -> float:
        return 0.75

    def evaluate_dense(self) -> float:
        return 0.85

    def check_milestones_incremental(
        self, screenshot: bytes | None = None
    ) -> tuple[int, int]:
        return (1, 2)


def _make_demo(steps: list[DemoStep], task_id: str = "test-task-1") -> Demo:
    return Demo(
        task_id=task_id,
        demo_id="demo-001",
        description="Test demo",
        steps=steps,
    )


def _key_step(index: int, key: str, desc: str = "") -> DemoStep:
    return DemoStep(
        step_index=index,
        screenshot_path=f"step_{index:03d}.png",
        action_type="key",
        action_description=desc or f"Press {key}",
        target_description="",
        action_value=key,
        description=desc or f"Press {key}",
    )


def _type_step(index: int, text: str, desc: str = "") -> DemoStep:
    return DemoStep(
        step_index=index,
        screenshot_path=f"step_{index:03d}.png",
        action_type="type",
        action_description=desc or f'Type "{text}"',
        target_description="",
        action_value=text,
        description=desc or f'Type "{text}"',
    )


def _click_step(
    index: int, desc: str, x: float = 0.5, y: float = 0.5
) -> DemoStep:
    return DemoStep(
        step_index=index,
        screenshot_path=f"step_{index:03d}.png",
        action_type="click",
        action_description=f"Click on {desc}",
        target_description=desc,
        action_value="",
        description=desc,
        x=x,
        y=y,
    )


# ---------------------------------------------------------------------------
# Test 1: 3-step keyboard-only demo (all Tier 1)
# ---------------------------------------------------------------------------


class TestKeyboardOnlyDemo:
    """Win+R -> type 'notepad' -> Enter: all Tier 1, no VLM calls."""

    @patch("time.sleep", return_value=None)
    @patch(
        "openadapt_evals.agents.demo_executor.track_demo_execution",
        create=True,
    )
    def test_three_keyboard_steps_execute_in_order(
        self, _mock_telemetry: MagicMock, _mock_sleep: MagicMock
    ) -> None:
        env = MockEnv()
        executor = DemoExecutor(step_delay=0)

        demo = _make_demo([
            _key_step(0, "win+r", "Open Run dialog"),
            _type_step(1, "notepad", "Type notepad"),
            _key_step(2, "Return", "Press Enter"),
        ])
        task_config = _TaskConfig()

        score, screenshots = executor.run(env, demo, task_config)

        # All 3 actions should dispatch
        assert len(env.actions) == 3

        # Verify order and types
        a0 = env.actions[0]
        assert a0["method"] == "step"
        assert a0["type"] == "key"
        assert a0["key"] == "win+r"

        a1 = env.actions[1]
        assert a1["method"] == "step"
        assert a1["type"] == "type"
        assert a1["text"] == "notepad"

        a2 = env.actions[2]
        assert a2["method"] == "step"
        assert a2["type"] == "key"
        assert a2["key"] == "Return"

    @patch("time.sleep", return_value=None)
    def test_all_keyboard_steps_are_tier_1(
        self, _mock_sleep: MagicMock
    ) -> None:
        """Verify tier metadata: keyboard and type are Tier 1 (no VLM)."""
        env = MockEnv()
        executor = DemoExecutor(step_delay=0)

        demo = _make_demo([
            _key_step(0, "win+r"),
            _type_step(1, "notepad"),
            _key_step(2, "Return"),
        ])
        task_config = _TaskConfig()

        # Capture actions produced by _execute_step to inspect tier tags
        produced_actions: list[BenchmarkAction] = []
        original_dispatch = executor._dispatch_action

        def capture_dispatch(env_arg, action):
            produced_actions.append(action)
            return original_dispatch(env_arg, action)

        executor._dispatch_action = capture_dispatch

        executor.run(env, demo, task_config)

        assert len(produced_actions) == 3
        for action in produced_actions:
            tier = (action.raw_action or {}).get("tier")
            assert tier == 1, (
                f"Expected Tier 1 for {action.type}, got tier={tier}"
            )


# ---------------------------------------------------------------------------
# Test 2: Demo with a click step (Tier 2 grounder call)
# ---------------------------------------------------------------------------


class TestClickDemo:
    """key -> click -> type: click goes through grounder, others bypass it."""

    @patch("time.sleep", return_value=None)
    @patch(
        "openadapt_evals.agents.demo_executor.vlm_call",
        create=True,
    )
    def test_click_uses_grounder_keyboard_bypasses(
        self, _mock_vlm_call_module: MagicMock, _mock_sleep: MagicMock
    ) -> None:
        """Patch vlm_call at the point it is actually imported inside
        _ground_click_vlm so we can verify it is called for clicks and
        NOT called for keyboard/type steps."""
        env = MockEnv()
        executor = DemoExecutor(step_delay=0)

        demo = _make_demo([
            _key_step(0, "win+r", "Open Run dialog"),
            _click_step(1, "the OK button"),
            _type_step(2, "hello", "Type hello"),
        ])
        task_config = _TaskConfig()

        # Mock vlm_call to return valid click JSON
        mock_vlm_response = '{"type": "click", "x": 0.3, "y": 0.7}'
        with patch(
            "openadapt_evals.vlm.vlm_call",
            return_value=mock_vlm_response,
        ) as mock_vlm:
            score, screenshots = executor.run(env, demo, task_config)

            # vlm_call should be invoked exactly once (for the click step)
            assert mock_vlm.call_count == 1

        # All 3 actions should still dispatch
        assert len(env.actions) == 3

        # First action: key (via step)
        assert env.actions[0]["method"] == "step"
        assert env.actions[0]["type"] == "key"

        # Second action: click (via pixel_action because x,y present)
        assert env.actions[1]["method"] == "pixel_action"

        # Third action: type (via step)
        assert env.actions[2]["method"] == "step"
        assert env.actions[2]["type"] == "type"

    @patch("time.sleep", return_value=None)
    def test_click_grounder_returns_fixed_coordinates(
        self, _mock_sleep: MagicMock
    ) -> None:
        """The grounder mock returns specific x,y which pixel_action receives."""
        env = MockEnv()
        executor = DemoExecutor(step_delay=0)

        demo = _make_demo([
            _click_step(0, "the Start button"),
        ])
        task_config = _TaskConfig()

        mock_vlm_response = '{"type": "click", "x": 0.12, "y": 0.95}'
        with patch(
            "openadapt_evals.vlm.vlm_call",
            return_value=mock_vlm_response,
        ):
            executor.run(env, demo, task_config)

        assert len(env.actions) == 1
        action = env.actions[0]
        assert action["method"] == "pixel_action"
        assert abs(action["x_frac"] - 0.12) < 1e-6
        assert abs(action["y_frac"] - 0.95) < 1e-6


# ---------------------------------------------------------------------------
# Test 3: Score from evaluate_dense()
# ---------------------------------------------------------------------------


class TestEvaluation:
    """Verify the executor returns a score from evaluate_dense()."""

    @patch("time.sleep", return_value=None)
    def test_returns_dense_score_when_milestones_present(
        self, _mock_sleep: MagicMock
    ) -> None:
        env = MockEnv()
        executor = DemoExecutor(step_delay=0)

        demo = _make_demo([_key_step(0, "Return")])
        task_config = _TaskConfig(milestones=["milestone-1", "milestone-2"])

        score, screenshots = executor.run(env, demo, task_config)

        # MockEnv.evaluate_dense() returns 0.85
        assert score == pytest.approx(0.85)

    @patch("time.sleep", return_value=None)
    def test_returns_binary_score_when_no_milestones(
        self, _mock_sleep: MagicMock
    ) -> None:
        env = MockEnv()
        executor = DemoExecutor(step_delay=0)

        demo = _make_demo([_key_step(0, "Return")])
        task_config = _TaskConfig(milestones=[])

        score, screenshots = executor.run(env, demo, task_config)

        # MockEnv.evaluate() returns 0.75
        assert score == pytest.approx(0.75)

    @patch("time.sleep", return_value=None)
    def test_screenshots_collected(
        self, _mock_sleep: MagicMock
    ) -> None:
        env = MockEnv()
        executor = DemoExecutor(step_delay=0)

        demo = _make_demo([
            _key_step(0, "a"),
            _key_step(1, "b"),
        ])
        task_config = _TaskConfig()

        score, screenshots = executor.run(env, demo, task_config)

        # 1 from reset + 1 per step = 3
        assert len(screenshots) == 3
        for s in screenshots:
            assert isinstance(s, bytes)
            assert len(s) > 0


# ---------------------------------------------------------------------------
# Test 4: Telemetry events fire
# ---------------------------------------------------------------------------


class TestTelemetry:
    """Verify telemetry events are emitted at start and completion."""

    @patch("time.sleep", return_value=None)
    def test_telemetry_fires_start_and_completed(
        self, _mock_sleep: MagicMock
    ) -> None:
        env = MockEnv()
        executor = DemoExecutor(step_delay=0)

        demo = _make_demo([
            _key_step(0, "win+r"),
            _type_step(1, "notepad"),
        ])
        task_config = _TaskConfig(id="telemetry-task")

        with patch(
            "openadapt_evals.telemetry.capture_event",
            return_value=True,
        ) as mock_capture:
            score, screenshots = executor.run(env, demo, task_config)

        # track_demo_execution calls capture_event twice: start + completed
        assert mock_capture.call_count >= 2

        calls = mock_capture.call_args_list
        events = [c[0][0] for c in calls]
        assert "demo_execution" in events

        # Find start and completed calls
        props_list = [c[0][1] for c in calls if c[0][0] == "demo_execution"]
        phases = [p["phase"] for p in props_list]
        assert "start" in phases
        assert "completed" in phases

        # Completed event should have score and tier counts
        completed = next(p for p in props_list if p["phase"] == "completed")
        assert "score" in completed
        assert "tier1_count" in completed
        assert completed["task_id"] == "telemetry-task"
        assert completed["num_steps"] == 2
        # Both steps are keyboard/type -> tier1
        assert completed["tier1_count"] == 2
        assert completed.get("tier2_count", 0) == 0


# ---------------------------------------------------------------------------
# Test 5: Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Miscellaneous edge-case coverage."""

    @patch("time.sleep", return_value=None)
    def test_empty_demo(self, _mock_sleep: MagicMock) -> None:
        env = MockEnv()
        executor = DemoExecutor(step_delay=0)

        demo = _make_demo([])
        task_config = _TaskConfig()

        with pytest.raises(ActionExecutionError, match="no executable steps"):
            executor.run(env, demo, task_config)

        assert len(env.actions) == 0

    @patch("time.sleep", return_value=None)
    def test_key_step_with_no_value_fails(
        self, _mock_sleep: MagicMock
    ) -> None:
        env = MockEnv()
        executor = DemoExecutor(step_delay=0)

        demo = _make_demo([
            DemoStep(
                step_index=0,
                screenshot_path="step_000.png",
                action_type="key",
                action_description="Bad step",
                target_description="",
                action_value="",
                description="Missing key value",
            ),
            _key_step(1, "Return"),
        ])
        task_config = _TaskConfig()

        with pytest.raises(ActionExecutionError, match="did not produce an action"):
            executor.run(env, demo, task_config)

        assert len(env.actions) == 0

    @patch("time.sleep", return_value=None)
    def test_unknown_action_type_fails(
        self, _mock_sleep: MagicMock
    ) -> None:
        env = MockEnv()
        executor = DemoExecutor(step_delay=0)

        demo = _make_demo([
            DemoStep(
                step_index=0,
                screenshot_path="step_000.png",
                action_type="swipe",
                action_description="Swipe left",
                target_description="",
                action_value="left",
                description="Swipe left",
            ),
            _key_step(1, "Escape"),
        ])
        task_config = _TaskConfig()

        with pytest.raises(ActionExecutionError, match="did not produce an action"):
            executor.run(env, demo, task_config)

        assert len(env.actions) == 0

    @patch("time.sleep", return_value=None)
    def test_milestones_checked_per_step(
        self, _mock_sleep: MagicMock
    ) -> None:
        """When milestones exist, check_milestones_incremental is called."""
        env = MockEnv()
        env.check_milestones_incremental = MagicMock(return_value=(1, 2))
        executor = DemoExecutor(step_delay=0)

        demo = _make_demo([
            _key_step(0, "a"),
            _key_step(1, "b"),
        ])
        task_config = _TaskConfig(milestones=["m1", "m2"])

        executor.run(env, demo, task_config)

        # Called once per step
        assert env.check_milestones_incremental.call_count == 2

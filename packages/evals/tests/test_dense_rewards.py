"""Tests for dense partial rewards via milestones in RLEnvironment."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from openadapt_evals.adapters.base import (
    BenchmarkAction,
    BenchmarkObservation,
    BenchmarkResult,
    BenchmarkTask,
    EvaluationUnavailableError,
)
from openadapt_evals.adapters.rl_env import ResetConfig, RLEnvironment
from openadapt_evals.task_config import Milestone, TaskCheck, TaskConfig


def _make_adapter():
    adapter = MagicMock()
    adapter.observe.return_value = BenchmarkObservation(
        screenshot=b"fake-screenshot", raw_observation={}
    )
    adapter.step.return_value = (
        BenchmarkObservation(screenshot=b"fake-screenshot", raw_observation={}),
        False,
        {},
    )
    adapter.load_task.return_value = BenchmarkTask(
        task_id="test-001", instruction="Test task", domain="desktop"
    )
    adapter.load_task_from_json.return_value = BenchmarkTask(
        task_id="test-001", instruction="Test task", domain="desktop"
    )
    adapter.reset.return_value = BenchmarkObservation(
        screenshot=b"fake-screenshot", raw_observation={}
    )
    adapter.evaluate.return_value = BenchmarkResult(
        task_id="test-001", success=False, score=0.0
    )
    adapter.config = MagicMock(server_url="http://localhost:5001")
    return adapter


def _make_task_config(milestones=None):
    return TaskConfig(
        name="Test task",
        id="test-001",
        domain="desktop",
        setup=[],
        checks=[],
        combine="and",
        max_steps=15,
        milestones=milestones or [],
    )


class TestDenseRewards:
    def test_evaluate_dense_with_milestones(self):
        adapter = _make_adapter()
        task_config = _make_task_config(
            milestones=[
                Milestone(
                    name="Step 1 done",
                    check=TaskCheck(check="command", run="echo 1", expect="1", match="exact"),
                ),
                Milestone(
                    name="Step 2 done",
                    check=TaskCheck(check="command", run="echo 0", expect="1", match="exact"),
                ),
            ]
        )
        env = RLEnvironment(adapter, task_config=task_config)
        env.reset(config=ResetConfig(task_id="test-001"))

        with patch.object(TaskConfig, "_run_vm_command") as mock_cmd:
            mock_cmd.side_effect = ["1", "0"]
            score = env.evaluate_dense()

        # 1/2 milestones passed = 0.5, binary = 0.0, max(0.5, 0.0) = 0.5
        assert score == 0.5

    def test_evaluate_dense_all_pass(self):
        adapter = _make_adapter()
        adapter.evaluate.return_value = BenchmarkResult(
            task_id="test-001", success=True, score=1.0
        )
        task_config = _make_task_config(
            milestones=[
                Milestone(
                    name="Done",
                    check=TaskCheck(check="command", run="echo ok", expect="ok", match="exact"),
                ),
            ]
        )
        env = RLEnvironment(adapter, task_config=task_config)
        env.reset(config=ResetConfig(task_id="test-001"))

        with patch.object(TaskConfig, "_run_vm_command", return_value="ok"):
            score = env.evaluate_dense()

        # milestones = 1/1 = 1.0, binary = 1.0, max = 1.0
        assert score == 1.0

    def test_evaluate_dense_no_milestones_falls_back(self):
        adapter = _make_adapter()
        adapter.evaluate.return_value = BenchmarkResult(
            task_id="test-001", success=False, score=0.0
        )
        task_config = _make_task_config(milestones=[])
        env = RLEnvironment(adapter, task_config=task_config)
        env.reset(config=ResetConfig(task_id="test-001"))

        score = env.evaluate_dense()
        assert score == 0.0

    def test_evaluate_dense_no_task_config(self):
        adapter = _make_adapter()
        adapter.evaluate.return_value = BenchmarkResult(
            task_id="test-001", success=False, score=0.0
        )
        env = RLEnvironment(adapter)
        env.reset(config=ResetConfig(task_id="test-001"))

        score = env.evaluate_dense()
        assert score == 0.0

    def test_load_task_config(self):
        adapter = _make_adapter()
        env = RLEnvironment(adapter)

        task_config = _make_task_config()
        env.load_task_config(task_config)

        assert env._task_config is task_config
        assert env._default_task_id == "test-001"

    def test_collect_rollout_uses_dense_rewards(self):
        adapter = _make_adapter()
        # Make step return done after 2 steps
        adapter.step.side_effect = [
            (BenchmarkObservation(screenshot=b"s1", raw_observation={}), False, {}),
            (BenchmarkObservation(screenshot=b"s2", raw_observation={}), True, {}),
        ]

        task_config = _make_task_config(
            milestones=[
                Milestone(
                    name="Step done",
                    check=TaskCheck(check="command", run="echo 1", expect="1", match="exact"),
                ),
                Milestone(
                    name="Not done",
                    check=TaskCheck(check="command", run="echo 0", expect="1", match="exact"),
                ),
            ]
        )
        env = RLEnvironment(adapter, task_config=task_config)

        def agent_fn(obs):
            return BenchmarkAction(type="click", x=100, y=100)

        with patch.object(TaskConfig, "_run_vm_command") as mock_cmd:
            mock_cmd.side_effect = ["1", "0"]
            trajectory = env.collect_rollout(agent_fn, max_steps=5, task_id="test-001")

        # Last step should have dense reward
        assert trajectory[-1].reward == 0.5  # 1/2 milestones
        assert trajectory[-1].info.get("milestones_passed") == 1
        assert trajectory[-1].info.get("milestones_total") == 2

    def test_trajectory_info_contains_milestone_details(self):
        adapter = _make_adapter()
        task_config = _make_task_config(
            milestones=[
                Milestone(
                    name="M1",
                    check=TaskCheck(check="command", run="echo 1", expect="1", match="exact"),
                ),
            ]
        )
        env = RLEnvironment(adapter, task_config=task_config)
        env.reset(config=ResetConfig(task_id="test-001"))
        env.step(BenchmarkAction(type="click", x=100, y=100))

        with patch.object(TaskConfig, "_run_vm_command", return_value="1"):
            env.evaluate_dense()

        last_step = env.trajectory[-1]
        assert "milestone_score" in last_step.info
        assert "binary_score" in last_step.info
        assert last_step.info["milestones_passed"] == 1
        assert last_step.info["milestones_total"] == 1

    @patch("openadapt_evals.vlm_evaluator.vlm_judge")
    def test_milestone_with_vlm_check(self, mock_vlm):
        mock_vlm.return_value = (True, 0.9)

        adapter = _make_adapter()
        task_config = _make_task_config(
            milestones=[
                Milestone(
                    name="VLM check",
                    check=TaskCheck(check="screenshot", description="App is open"),
                ),
            ]
        )
        env = RLEnvironment(adapter, task_config=task_config)
        env.reset(config=ResetConfig(task_id="test-001"))

        score = env.evaluate_dense()
        assert score == 1.0  # 1/1 milestone passed
        mock_vlm.assert_called_once()

    def test_reset_uses_task_config_for_task_loading(self):
        adapter = _make_adapter()
        task_config = _make_task_config()
        env = RLEnvironment(adapter, task_config=task_config)

        env.reset(config=ResetConfig(task_id="test-001"))

        # Should use load_task_from_json since task_config matches
        assert env._current_task is not None
        assert env._current_task.task_id == "test-001"


class TestEvaluateDenseEvalOrder:
    """Verify evaluate_dense tries binary first, local fallback second.

    Training speed comes from the adapter's timeout config (15s for training
    vs 180s for benchmarking), NOT from skipping the binary eval path.
    Both evaluation methods are tried, and the max score is used.
    """

    def test_binary_eval_called_first(self):
        """Binary evaluate() is always called when milestones exist."""
        adapter = _make_adapter()
        check = TaskCheck(check="command", run="echo 1", expect="1", match="exact")
        task_config = _make_task_config(
            milestones=[Milestone(name="Step done", check=check)],
        )
        task_config.checks = [check]

        env = RLEnvironment(adapter, task_config=task_config)
        env.reset(config=ResetConfig(task_id="test-001"))

        with patch.object(task_config, "evaluate_checks_local", return_value=1.0):
            env.evaluate_dense()

        # Binary eval was called (returns 0.0 from mock default)
        adapter.evaluate.assert_called_once()

    def test_local_fallback_when_binary_returns_zero(self):
        """Local checks run as fallback when binary returns 0.0."""
        adapter = _make_adapter()
        check = TaskCheck(check="command", run="echo 1", expect="1", match="exact")
        task_config = _make_task_config(
            milestones=[Milestone(name="Step done", check=check)],
        )
        task_config.checks = [check]

        env = RLEnvironment(adapter, task_config=task_config)
        env.reset(config=ResetConfig(task_id="test-001"))

        with patch.object(task_config, "evaluate_checks_local", return_value=1.0) as mock_local:
            score = env.evaluate_dense()

        mock_local.assert_called_once()
        assert score >= 1.0

    def test_local_not_called_when_binary_succeeds(self):
        """Local checks are skipped when binary eval returns > 0."""
        adapter = _make_adapter()
        adapter.evaluate.return_value = BenchmarkResult(
            task_id="test-001", success=True, score=0.75,
        )
        check = TaskCheck(check="command", run="echo 1", expect="1", match="exact")
        task_config = _make_task_config(
            milestones=[Milestone(name="Step done", check=check)],
        )
        task_config.checks = [check]

        env = RLEnvironment(adapter, task_config=task_config)
        env.reset(config=ResetConfig(task_id="test-001"))

        with patch.object(task_config, "evaluate_checks_local") as mock_local:
            score = env.evaluate_dense()

        mock_local.assert_not_called()
        assert score >= 0.75

    def test_measured_milestone_can_replace_unavailable_binary_evaluator(self):
        adapter = _make_adapter()
        adapter.evaluate.return_value = BenchmarkResult(
            task_id="test-001",
            success=False,
            score=0.0,
            error_type="infrastructure",
        )
        check = TaskCheck(check="command", run="echo ok", expect="ok")
        task_config = _make_task_config(
            milestones=[Milestone(name="Saved", check=check)]
        )
        env = RLEnvironment(adapter, task_config=task_config)
        env.reset(config=ResetConfig(task_id="test-001"))

        with patch.object(TaskConfig, "_run_vm_command", return_value="ok"):
            assert env.evaluate_dense() == 1.0

    def test_unmeasured_milestone_does_not_hide_evaluator_failure(self):
        adapter = _make_adapter()
        adapter.evaluate.return_value = BenchmarkResult(
            task_id="test-001",
            success=False,
            score=0.0,
            error_type="infrastructure",
        )
        check = TaskCheck(check="command", run="echo ok", expect="ok")
        task_config = _make_task_config(
            milestones=[Milestone(name="Saved", check=check)]
        )
        env = RLEnvironment(adapter, task_config=task_config)
        env.reset(config=ResetConfig(task_id="test-001"))

        with (
            patch.object(TaskConfig, "_run_vm_command", side_effect=OSError("offline")),
            pytest.raises(EvaluationUnavailableError),
        ):
            env.evaluate_dense()

    def test_local_check_exception_is_not_a_measured_zero(self):
        check = TaskCheck(check="screenshot", description="Saved result")
        task_config = _make_task_config()
        task_config.checks = [check]

        with (
            patch(
                "openadapt_evals.vlm_evaluator.vlm_judge",
                side_effect=RuntimeError("judge offline"),
            ),
            pytest.raises(EvaluationUnavailableError),
        ):
            task_config.evaluate_checks_local(b"image", "http://localhost:5001")

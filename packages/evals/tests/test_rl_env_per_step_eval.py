"""Tests for RLEnvironment per-step evaluation feature."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from openadapt_evals.adapters import (
    BenchmarkAction,
    WAAMockAdapter,
)
from openadapt_evals.adapters.rl_env import RLEnvironment, RolloutStep


@pytest.fixture
def mock_env_with_eval() -> RLEnvironment:
    """RLEnvironment with evaluate_every_step=True."""
    adapter = WAAMockAdapter(num_tasks=5, domains=["notepad"])
    task_id = adapter.list_tasks()[0].task_id
    return RLEnvironment(
        adapter=adapter,
        default_task_id=task_id,
        evaluate_every_step=True,
    )


@pytest.fixture
def mock_env_default() -> RLEnvironment:
    """RLEnvironment with evaluate_every_step=False (default)."""
    adapter = WAAMockAdapter(num_tasks=5, domains=["notepad"])
    task_id = adapter.list_tasks()[0].task_id
    return RLEnvironment(adapter=adapter, default_task_id=task_id)


class TestPerStepEvalDisabled:
    def test_no_evaluation_score_by_default(self, mock_env_default: RLEnvironment):
        """With evaluate_every_step=False, info does NOT contain evaluation_score."""
        mock_env_default.reset()
        result = mock_env_default.step(BenchmarkAction(type="click", x=100.0, y=100.0))

        assert "evaluation_score" not in result.info
        assert "evaluation_success" not in result.info
        assert "evaluation_error" not in result.info


class TestPerStepEvalEnabled:
    def test_evaluation_score_present(self, mock_env_with_eval: RLEnvironment):
        """With evaluate_every_step=True, info contains evaluation_score."""
        mock_env_with_eval.reset()
        result = mock_env_with_eval.step(
            BenchmarkAction(type="click", x=100.0, y=100.0)
        )

        assert "evaluation_score" in result.info
        assert isinstance(result.info["evaluation_score"], float)
        assert 0.0 <= result.info["evaluation_score"] <= 1.0
        assert "evaluation_success" in result.info
        assert isinstance(result.info["evaluation_success"], bool)

    def test_reward_still_zero(self, mock_env_with_eval: RLEnvironment):
        """Per-step evaluation does NOT change the reward signal."""
        mock_env_with_eval.reset()
        result = mock_env_with_eval.step(
            BenchmarkAction(type="click", x=100.0, y=100.0)
        )

        assert result.reward == 0.0

    def test_evaluation_error_handled_gracefully(self):
        """If evaluate() raises, info contains evaluation_error and step succeeds."""
        adapter = WAAMockAdapter(num_tasks=5, domains=["notepad"])
        task_id = adapter.list_tasks()[0].task_id

        # Patch evaluate to raise
        adapter.evaluate = MagicMock(side_effect=RuntimeError("eval server down"))

        env = RLEnvironment(
            adapter=adapter,
            default_task_id=task_id,
            evaluate_every_step=True,
        )
        env.reset()
        result = env.step(BenchmarkAction(type="click", x=100.0, y=100.0))

        # Step should succeed despite eval failure
        assert isinstance(result, RolloutStep)
        assert result.reward == 0.0
        assert "evaluation_error" in result.info
        assert "eval server down" in result.info["evaluation_error"]
        assert "evaluation_score" not in result.info

    def test_evaluation_called_each_step(self, mock_env_with_eval: RLEnvironment):
        """evaluate() is called on every step when enabled."""
        mock_env_with_eval.reset()

        # Patch evaluate to track calls
        call_count = 0
        original_evaluate = mock_env_with_eval._adapter.evaluate

        def counting_evaluate(task):
            nonlocal call_count
            call_count += 1
            return original_evaluate(task)

        mock_env_with_eval._adapter.evaluate = counting_evaluate

        for _ in range(3):
            mock_env_with_eval.step(
                BenchmarkAction(type="click", x=100.0, y=100.0)
            )

        assert call_count == 3

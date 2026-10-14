"""Tests for evaluate_milestones_screenshot standalone function."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from openadapt_evals.adapters.base import EvaluationUnavailableError
from openadapt_evals.task_config import (
    Milestone,
    TaskCheck,
    TaskConfig,
    evaluate_milestones_screenshot,
)


def _make_task_config(milestones):
    return TaskConfig(
        name="Test task",
        id="test-001",
        domain="desktop",
        setup=[],
        checks=[],
        combine="and",
        max_steps=15,
        milestones=milestones,
    )


class TestEvaluateMilestonesScreenshot:
    def test_no_milestones_is_unmeasured(self):
        task = _make_task_config([])
        with pytest.raises(EvaluationUnavailableError, match="No milestone"):
            evaluate_milestones_screenshot(task, b"fake-png")

    def test_non_screenshot_milestone_is_not_silently_skipped(self):
        task = _make_task_config([
            Milestone(
                name="Command check",
                check=TaskCheck(check="command", run="echo 1", expect="1"),
            ),
        ])
        with pytest.raises(EvaluationUnavailableError, match="cannot measure"):
            evaluate_milestones_screenshot(task, b"fake-png")

    @patch("openadapt_evals.vlm_evaluator.vlm_judge")
    def test_all_pass(self, mock_vlm):
        mock_vlm.return_value = (True, 0.95)
        task = _make_task_config([
            Milestone(
                name="App open",
                check=TaskCheck(check="screenshot", description="App is open"),
            ),
            Milestone(
                name="File loaded",
                check=TaskCheck(check="screenshot", description="File is loaded"),
            ),
        ])
        score = evaluate_milestones_screenshot(task, b"fake-png")
        assert score == 1.0
        assert mock_vlm.call_count == 2

    @patch("openadapt_evals.vlm_evaluator.vlm_judge")
    def test_partial_pass(self, mock_vlm):
        mock_vlm.side_effect = [(True, 0.9), (False, 0.3)]
        task = _make_task_config([
            Milestone(
                name="Step 1",
                check=TaskCheck(check="screenshot", description="Step 1 done"),
            ),
            Milestone(
                name="Step 2",
                check=TaskCheck(check="screenshot", description="Step 2 done"),
            ),
        ])
        score = evaluate_milestones_screenshot(task, b"fake-png")
        assert score == 0.5

    @patch("openadapt_evals.vlm_evaluator.vlm_judge")
    def test_mixed_contract_is_not_partially_measured(self, mock_vlm):
        mock_vlm.return_value = (True, 0.9)
        task = _make_task_config([
            Milestone(
                name="Command milestone",
                check=TaskCheck(check="command", run="echo 1", expect="1"),
            ),
            Milestone(
                name="Screenshot milestone",
                check=TaskCheck(check="screenshot", description="Something visible"),
            ),
        ])
        with pytest.raises(EvaluationUnavailableError, match="cannot measure"):
            evaluate_milestones_screenshot(task, b"fake-png")
        mock_vlm.assert_not_called()

    @patch("openadapt_evals.vlm_evaluator.vlm_judge")
    def test_custom_model_passed_through(self, mock_vlm):
        mock_vlm.return_value = (True, 0.9)
        task = _make_task_config([
            Milestone(
                name="Check",
                check=TaskCheck(check="screenshot", description="Visible"),
            ),
        ])
        evaluate_milestones_screenshot(task, b"fake-png", model="gpt-4o")
        mock_vlm.assert_called_once_with(b"fake-png", "Visible", model="gpt-4o")

    @patch("openadapt_evals.vlm_evaluator.vlm_judge")
    def test_all_fail(self, mock_vlm):
        mock_vlm.return_value = (False, 0.2)
        task = _make_task_config([
            Milestone(
                name="Check",
                check=TaskCheck(check="screenshot", description="Not there"),
            ),
        ])
        score = evaluate_milestones_screenshot(task, b"fake-png")
        assert score == 0.0

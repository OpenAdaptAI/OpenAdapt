"""Fail-closed tests for benchmark results from adapter and file boundaries."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from openadapt_evals.adapters import (
    BenchmarkAction,
    BenchmarkResult,
    WAAMockAdapter,
    normalize_benchmark_result,
)
from openadapt_evals.agents import ScriptedAgent
from openadapt_evals.benchmarks.runner import (
    EvaluationConfig,
    _failed_task_result,
    _run_single_task,
    compute_metrics,
)
from openadapt_evals.integrations.wandb_logger import load_results_from_summary


@pytest.mark.parametrize(
    "result",
    [
        BenchmarkResult(task_id="t", success=1, score=1.0),
        BenchmarkResult(task_id="t", success=True, score=float("nan")),
        BenchmarkResult(task_id="t", success=True, score=1.1),
        BenchmarkResult(task_id="t", success=True, score=1.0, error_type="other"),
        BenchmarkResult(task_id="t", success=True, score=0.0),
        BenchmarkResult(task_id="t", success=False, score=1.0),
    ],
)
def test_malformed_result_becomes_unscored_evaluation_failure(
    result: BenchmarkResult,
) -> None:
    normalized = normalize_benchmark_result(result)

    assert normalized.success is False
    assert normalized.score == 0.0
    assert normalized.error_type == "evaluation"
    assert normalized.reason and normalized.reason.startswith("Malformed")


def test_partial_success_score_remains_valid() -> None:
    result = BenchmarkResult(task_id="t", success=True, score=0.75)

    assert normalize_benchmark_result(result) is result


def test_valid_unavailable_category_is_preserved_while_success_is_cleared() -> None:
    result = BenchmarkResult(
        task_id="t",
        success=True,
        score=1.0,
        error_type="infrastructure",
    )

    normalized = normalize_benchmark_result(result)

    assert normalized.success is False
    assert normalized.score == 0.0
    assert normalized.error_type == "infrastructure"


def test_done_gate_rejects_malformed_adapter_result() -> None:
    adapter = WAAMockAdapter(num_tasks=1, domains=["browser"])
    task = adapter.list_tasks()[0]
    adapter.evaluate = MagicMock(
        return_value=BenchmarkResult(
            task_id=task.task_id,
            success=True,
            score=1.0,
            error_type="invented",
        )
    )

    result = _run_single_task(
        ScriptedAgent([BenchmarkAction(type="done")]),
        adapter,
        task,
        EvaluationConfig(
            done_gate=True,
            verbose=False,
            save_execution_traces=False,
            enable_live_tracking=False,
        ),
    )

    assert result.success is False
    assert result.error_type == "evaluation"
    assert "unknown error_type" in (result.error or "")


def test_final_result_from_non_waa_adapter_is_validated() -> None:
    adapter = WAAMockAdapter(num_tasks=1, domains=["browser"])
    task = adapter.list_tasks()[0]
    adapter.evaluate = MagicMock(
        return_value=BenchmarkResult(
            task_id=task.task_id,
            success="yes",
            score=1.0,
        )
    )

    result = _run_single_task(
        ScriptedAgent([BenchmarkAction(type="done")]),
        adapter,
        task,
        EvaluationConfig(
            verbose=False,
            save_execution_traces=False,
            enable_live_tracking=False,
        ),
    )

    assert result.success is False
    assert result.error_type == "evaluation"
    assert "success must be a bool" in (result.error or "")


def test_aggregation_excludes_malformed_direct_result() -> None:
    metrics = compute_metrics(
        [BenchmarkResult(task_id="t", success=True, score=1.0, error_type="bad")]
    )

    assert metrics["num_tasks"] == 1
    assert metrics["num_outcome_tasks"] == 0
    assert metrics["num_evaluation_failures"] == 1
    assert metrics["success_count"] == 0


def test_unknown_exception_error_type_becomes_evaluation_failure() -> None:
    adapter = WAAMockAdapter(num_tasks=1, domains=["browser"])
    task = adapter.list_tasks()[0]

    class PluginError(RuntimeError):
        error_type = "plugin"

    error = PluginError("bad plugin")

    result = _failed_task_result(task, error)

    assert result.error_type == "evaluation"


def test_summary_loader_preserves_valid_diagnostics(tmp_path) -> None:
    path = tmp_path / "summary.json"
    path.write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "task_id": "t",
                        "success": False,
                        "score": 0.0,
                        "num_steps": 3,
                        "total_time_seconds": 1.5,
                        "error": "offline",
                        "reason": "evaluator offline",
                        "error_type": "infrastructure",
                    }
                ]
            }
        )
    )

    [result] = load_results_from_summary(path)

    assert result.error_type == "infrastructure"
    assert result.error == "offline"
    assert result.reason == "evaluator offline"
    assert result.num_steps == 3
    assert result.total_time_seconds == 1.5


def test_summary_loader_does_not_invent_missing_error_classification(
    tmp_path,
) -> None:
    path = tmp_path / "summary.json"
    path.write_text(
        json.dumps({"tasks": [{"task_id": "t", "success": False, "score": 0.0}]})
    )

    [result] = load_results_from_summary(path)

    assert result.error_type == "evaluation"
    assert "unknown error_type" in (result.reason or "")


@pytest.mark.parametrize(
    "updates",
    [
        {"score": None},
        {"score": float("nan")},
        {"success": "false"},
        {"error_type": "other"},
        {"num_steps": -1},
        {"total_time_seconds": float("inf")},
    ],
)
def test_summary_loader_marks_malformed_rows_unscored(tmp_path, updates) -> None:
    row = {
        "task_id": "t",
        "success": False,
        "score": 0.0,
        "num_steps": 0,
        "total_time_seconds": 0.0,
        "error_type": None,
    }
    row.update(updates)
    path = tmp_path / "summary.json"
    path.write_text(json.dumps({"tasks": [row]}))

    [result] = load_results_from_summary(path)

    assert result.success is False
    assert result.score == 0.0
    assert result.error_type == "evaluation"


def test_summary_loader_rejects_invalid_document_shape(tmp_path) -> None:
    path = tmp_path / "summary.json"
    path.write_text(json.dumps({"tasks": {}}))

    with pytest.raises(ValueError, match="tasks list"):
        load_results_from_summary(path)

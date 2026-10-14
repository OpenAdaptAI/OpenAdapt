"""W&B surfaces must keep unavailable attempts separate from outcomes."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from openadapt_evals.adapters import BenchmarkResult
from openadapt_evals.integrations.wandb_logger import WandbLogger


def _logger() -> WandbLogger:
    logger = object.__new__(WandbLogger)
    logger._run = MagicMock()
    logger._results_buffer = []
    logger._tasks_logged = 0
    return logger


def test_live_rate_excludes_unavailable_attempts() -> None:
    logger = _logger()
    logger.log_task_result(BenchmarkResult(task_id="ok", success=True, score=1.0))
    logger.log_task_result(
        BenchmarkResult(
            task_id="infra",
            success=False,
            score=0.0,
            error_type="infrastructure",
        )
    )

    payload = logger._run.log.call_args_list[-1].args[0]
    assert payload["task/current_success_rate"] == 1.0
    assert payload["task/outcomes_completed"] == 1
    assert payload["task/last_task_is_outcome"] is False
    assert payload["task/last_task_success"] is None


def test_task_table_keeps_classification_and_reason() -> None:
    logger = _logger()
    result = BenchmarkResult(
        task_id="infra",
        success=False,
        score=0.0,
        error="offline",
        reason="evaluator offline",
        error_type="infrastructure",
    )
    table = MagicMock()
    with patch("openadapt_evals.integrations.wandb_logger.Table", return_value=table) as cls:
        logger._log_task_table([result], {})

    columns = cls.call_args.kwargs["columns"]
    row = cls.call_args.kwargs["data"][0]
    assert row[columns.index("is_outcome")] is False
    assert row[columns.index("error_type")] == "infrastructure"
    assert row[columns.index("reason")] == "evaluator offline"


def test_error_breakdown_groups_by_error_type_not_message() -> None:
    logger = _logger()
    results = [
        BenchmarkResult(
            task_id="a",
            success=False,
            score=0.0,
            error="offline A",
            error_type="infrastructure",
        ),
        BenchmarkResult(
            task_id="b",
            success=False,
            score=0.0,
            error="offline B",
            error_type="infrastructure",
        ),
    ]
    with patch("openadapt_evals.integrations.wandb_logger.Table") as table:
        logger._log_error_breakdown(results)

    assert table.call_args.kwargs["data"] == [["infrastructure", 2, 100.0]]


def test_aggregate_rate_publishes_its_outcome_denominator() -> None:
    logger = _logger()
    results = [
        BenchmarkResult(task_id="mail_1", success=True, score=1.0),
        BenchmarkResult(
            task_id="mail_2",
            success=False,
            score=0.0,
            error_type="infrastructure",
        ),
    ]
    with (
        patch.object(logger, "_log_task_table"),
        patch.object(logger, "_log_error_breakdown"),
        patch.object(logger, "_log_step_distribution"),
    ):
        logger.log_results(results)

    global_payload = logger._run.log.call_args_list[0].args[0]
    domain_payload = logger._run.log.call_args_list[1].args[0]
    assert global_payload["eval/success_rate"] == 1.0
    assert global_payload["eval/num_outcome_tasks"] == 1
    assert global_payload["eval/num_infrastructure_failures"] == 1
    assert domain_payload["eval/domain/mail/num_outcome_tasks"] == 1
    assert domain_payload["eval/domain/mail/num_infrastructure_failures"] == 1

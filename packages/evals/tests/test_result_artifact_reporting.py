"""Result artifact rows cannot manufacture measured benchmark success."""

from __future__ import annotations

import json

import pytest

from openadapt_evals.adapters.base import (
    benchmark_result_is_scored,
    normalize_benchmark_result_artifact,
)
from openadapt_evals.analysis.trace_analyzer import TraceAnalyzer
from openadapt_evals.benchmarks.pool_viewer import (
    get_domain_stats as get_pool_domain_stats,
)
from openadapt_evals.benchmarks.pool_viewer import parse_pool_logs
from openadapt_evals.benchmarks.viewer import (
    _get_domain_stats,
    load_task_results,
)


@pytest.mark.parametrize(
    "artifact",
    [
        {"success": "false", "score": 0.0},
        {"success": False, "score": float("nan")},
        {"success": False, "score": 0.0, "error_type": "unknown"},
    ],
)
def test_strict_artifact_parser_marks_malformed_rows_unscored(artifact) -> None:
    result = normalize_benchmark_result_artifact(
        {"task_id": "bad", **artifact},
        expected_task_id="bad",
    )

    assert result.success is False
    assert result.score == 0.0
    assert result.error_type == "evaluation"
    assert benchmark_result_is_scored(result) is False


def test_trace_summary_excludes_unscored_rows_but_retains_attempts(tmp_path) -> None:
    rows = [
        {"task_id": "pass", "success": True, "score": 1.0},
        {
            "task_id": "agent-fail",
            "success": False,
            "score": 0.0,
            "error_type": "agent",
        },
        {
            "task_id": "infra",
            "success": False,
            "score": 0.0,
            "error_type": "infrastructure",
        },
        {"task_id": "string-false", "success": "false", "score": 0.0},
    ]
    artifact = tmp_path / "results.jsonl"
    artifact.write_text("\n".join(json.dumps(row) for row in rows))

    summary = TraceAnalyzer(artifact).summary()

    assert summary["total_episodes"] == 4
    assert summary["outcome_episodes"] == 2
    assert summary["unscored_episodes"] == 2
    assert summary["success_rate"] == 0.5
    assert summary["episodes_by_status"] == {
        "passed": 1,
        "failed": 1,
        "infra_error": 1,
        "evaluation_error": 1,
    }


def _write_viewer_task(root, task_id: str, execution: object) -> None:
    task_dir = root / "tasks" / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "task.json").write_text(
        json.dumps({"domain": "desktop", "instruction": task_id})
    )
    (task_dir / "execution.json").write_text(json.dumps(execution))


def test_benchmark_viewer_normalizes_rows_before_domain_metrics(tmp_path) -> None:
    _write_viewer_task(tmp_path, "pass", {"success": True, "score": 1.0})
    _write_viewer_task(
        tmp_path,
        "agent-fail",
        {"success": False, "score": 0.0, "error_type": "agent"},
    )
    _write_viewer_task(
        tmp_path,
        "infra",
        {"success": False, "score": 0.0, "error_type": "infrastructure"},
    )
    _write_viewer_task(
        tmp_path,
        "string-false",
        {"success": "false", "score": 0.0},
    )

    tasks = load_task_results(tmp_path)
    stats = _get_domain_stats(tasks)["desktop"]

    assert all(type(task["execution"]["success"]) is bool for task in tasks)
    assert stats == {
        "total": 4,
        "outcomes": 2,
        "success": 1,
        "fail": 1,
        "unscored": 2,
    }


def test_pool_domain_metrics_do_not_use_raw_truthiness() -> None:
    tasks = [
        {"task_id": "pass", "domain": "desktop", "success": True, "score": 1.0},
        {
            "task_id": "agent-fail",
            "domain": "desktop",
            "success": False,
            "score": 0.0,
            "error_type": "agent",
        },
        {
            "task_id": "infra",
            "domain": "desktop",
            "success": False,
            "score": 0.0,
            "error_type": "infrastructure",
        },
        {
            "task_id": "string-false",
            "domain": "desktop",
            "success": "false",
            "score": 0.0,
        },
    ]

    assert get_pool_domain_stats(tasks)["desktop"] == {
        "total": 4,
        "outcomes": 2,
        "success": 1,
        "fail": 1,
        "unscored": 2,
    }


def test_pool_log_without_result_is_retained_as_unscored_error(tmp_path) -> None:
    log = tmp_path / "waa-pool-1.log"
    log.write_text(
        "[2026-07-28 12:00:00] [Domain]: desktop\n"
        "[2026-07-28 12:00:01] [Example ID]: missing-result\n"
        "[2026-07-28 12:00:02] Finished desktop/missing-result\n"
    )

    parsed = parse_pool_logs(tmp_path)

    assert len(parsed["tasks"]) == 1
    assert parsed["tasks"][0]["error_type"] == "evaluation"
    assert parsed["tasks"][0]["success"] is False
    assert parsed["workers"]["1"] == {
        "tasks": 1,
        "outcomes": 0,
        "successes": 0,
        "failures": 0,
        "unscored": 1,
    }


def test_partial_trajectory_score_is_measured_but_not_success(tmp_path) -> None:
    trace_dir = tmp_path / "trajectory"
    trace_dir.mkdir()
    (trace_dir / "trajectories.jsonl").write_text(
        json.dumps(
            {
                "episode_id": "partial",
                "step_index": 0,
                "episode_reward": 0.5,
                "planner_output": {"action_type": "click"},
            }
        )
    )

    analyzer = TraceAnalyzer(trace_dir)

    assert analyzer.episodes[0].score == 0.5
    assert analyzer.episodes[0].success is False
    assert analyzer.episodes[0].error_type is None
    assert analyzer.summary()["success_rate"] == 0.0


def test_partial_pool_score_is_measured_failure(tmp_path) -> None:
    log = tmp_path / "waa-pool-2.log"
    log.write_text(
        "[2026-07-28 12:00:00] [Domain]: desktop\n"
        "[2026-07-28 12:00:01] [Example ID]: partial\n"
        "[2026-07-28 12:00:02] Result: 0.5\n"
        "[2026-07-28 12:00:03] Finished desktop/partial\n"
    )

    parsed = parse_pool_logs(tmp_path)

    assert parsed["tasks"][0]["result"] == 0.5
    assert parsed["tasks"][0]["success"] is False
    assert parsed["tasks"][0]["error_type"] is None
    assert parsed["workers"]["2"]["outcomes"] == 1
    assert parsed["workers"]["2"]["failures"] == 1

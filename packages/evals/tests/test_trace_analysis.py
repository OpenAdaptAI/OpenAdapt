"""Tests for openadapt_evals.analysis trace analysis utilities."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openadapt_evals.analysis.report_generator import generate_report
from openadapt_evals.analysis.trace_analyzer import (
    _FORMAT_BENCHMARK_DIR,
    _FORMAT_FULL_EVAL_JSONL,
    _FORMAT_MIXED_DIR,
    _FORMAT_TRAJECTORY_DIR,
    Episode,
    StepRecord,
    TraceAnalyzer,
    _classify_failure,
    _detect_format,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def full_eval_jsonl(tmp_path: Path) -> Path:
    """Create a sample full_eval JSONL file."""
    jsonl_path = tmp_path / "full_eval.jsonl"
    records = [
        {
            "_meta": True,
            "planner_model": "claude-sonnet-4-6",
            "grounder_model": "gpt-4.1-mini",
            "max_steps": 15,
        },
        {
            "task_id": "task-001",
            "score": 1.0,
            "success": True,
            "steps": 5,
            "elapsed_seconds": 30.0,
            "error": None,
            "error_type": None,
            "started_at": "2026-03-20T10:00:00",
            "finished_at": "2026-03-20T10:00:30",
        },
        {
            "task_id": "task-002",
            "score": 0.0,
            "success": False,
            "steps": 15,
            "elapsed_seconds": 120.0,
            "error": None,
            "error_type": None,
            "started_at": "2026-03-20T10:01:00",
            "finished_at": "2026-03-20T10:03:00",
        },
        {
            "task_id": "task-003",
            "score": 0.0,
            "success": False,
            "steps": 2,
            "elapsed_seconds": 5.0,
            "error": "Connection refused",
            "error_type": "infrastructure",
            "started_at": "2026-03-20T10:04:00",
            "finished_at": "2026-03-20T10:04:05",
        },
        {
            "task_id": "task-004",
            "score": 0.5,
            "success": False,
            "steps": 8,
            "elapsed_seconds": 55.0,
            "error": None,
            "error_type": None,
            "milestones_passed": 2,
            "milestones_total": 4,
            "started_at": "2026-03-20T10:05:00",
            "finished_at": "2026-03-20T10:05:55",
        },
        {
            "task_id": "task-005",
            "score": 1.0,
            "success": True,
            "steps": 3,
            "elapsed_seconds": 20.0,
            "error": None,
            "error_type": None,
            "started_at": "2026-03-20T10:06:00",
            "finished_at": "2026-03-20T10:06:20",
        },
    ]
    lines = [json.dumps(r) for r in records]
    jsonl_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jsonl_path


@pytest.fixture
def trajectory_dir(tmp_path: Path) -> Path:
    """Create a sample PlannerTrajectoryLogger output directory."""
    trace_dir = tmp_path / "trajectories"
    trace_dir.mkdir()

    # Create episode subdirectory with a dummy PNG
    ep_dir = trace_dir / "episode-001"
    ep_dir.mkdir()
    (ep_dir / "step_000.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    (ep_dir / "step_001.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

    # Create trajectories.jsonl
    records = [
        {
            "episode_id": "episode-001",
            "step_index": 0,
            "screenshot_path": "episode-001/step_000.png",
            "task_instruction": "Open Notepad",
            "action_history": [],
            "planner_output": {
                "decision": "continue",
                "action_type": "click",
                "target_description": "Start menu",
                "instruction": "Click the Start menu",
                "reasoning": "Need to open Start to find Notepad",
            },
            "episode_reward": 1.0,
        },
        {
            "episode_id": "episode-001",
            "step_index": 1,
            "screenshot_path": "episode-001/step_001.png",
            "task_instruction": "Open Notepad",
            "action_history": ["Clicked Start menu"],
            "planner_output": {
                "decision": "done",
                "action_type": "click",
                "target_description": "Notepad",
                "instruction": "Click Notepad icon",
                "reasoning": "Notepad is visible in the menu",
            },
            "episode_reward": 1.0,
        },
    ]
    jsonl_path = trace_dir / "trajectories.jsonl"
    jsonl_path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    return trace_dir


@pytest.fixture
def benchmark_dir(tmp_path: Path) -> Path:
    """Create a sample benchmark viewer directory."""
    bench_dir = tmp_path / "benchmark_run"
    bench_dir.mkdir()

    # metadata.json
    (bench_dir / "metadata.json").write_text(
        json.dumps({"benchmark_name": "waa", "model_id": "claude-sonnet-4-6"}),
        encoding="utf-8",
    )

    # summary.json
    (bench_dir / "summary.json").write_text(
        json.dumps({"num_tasks": 2, "success_rate": 0.5}),
        encoding="utf-8",
    )

    # tasks
    tasks_dir = bench_dir / "tasks"
    tasks_dir.mkdir()

    # task_001
    t1 = tasks_dir / "task_001"
    t1.mkdir()
    (t1 / "task.json").write_text(
        json.dumps({"instruction": "Open Notepad", "domain": "desktop"}),
        encoding="utf-8",
    )
    (t1 / "execution.json").write_text(
        json.dumps({
            "success": True,
            "score": 1.0,
            "total_time_seconds": 25.0,
            "steps": [
                {"action": {"type": "click", "target_name": "Start"}, "reasoning": "Open start"},
            ],
        }),
        encoding="utf-8",
    )

    # task_002
    t2 = tasks_dir / "task_002"
    t2.mkdir()
    (t2 / "task.json").write_text(
        json.dumps({"instruction": "Close window", "domain": "desktop"}),
        encoding="utf-8",
    )
    (t2 / "execution.json").write_text(
        json.dumps({
            "success": False,
            "score": 0.0,
            "total_time_seconds": 60.0,
            "steps": [
                {"action": {"type": "click"}, "reasoning": "Wrong target"},
                {"action": {"type": "click"}, "reasoning": "Still wrong"},
            ],
        }),
        encoding="utf-8",
    )

    return bench_dir


@pytest.fixture
def second_eval_jsonl(tmp_path: Path) -> Path:
    """Create a second eval JSONL for comparison tests."""
    jsonl_path = tmp_path / "full_eval_v2.jsonl"
    records = [
        {
            "_meta": True,
            "planner_model": "gpt-5.4",
            "max_steps": 15,
        },
        {
            "task_id": "task-001",
            "score": 1.0,
            "success": True,
            "steps": 3,
            "elapsed_seconds": 20.0,
            "error": None,
            "error_type": None,
        },
        {
            "task_id": "task-002",
            "score": 1.0,
            "success": True,
            "steps": 8,
            "elapsed_seconds": 60.0,
            "error": None,
            "error_type": None,
        },
        {
            "task_id": "task-003",
            "score": 0.0,
            "success": False,
            "steps": 2,
            "elapsed_seconds": 5.0,
            "error": "Connection timeout",
            "error_type": "infrastructure",
        },
        {
            "task_id": "task-004",
            "score": 0.0,
            "success": False,
            "steps": 10,
            "elapsed_seconds": 80.0,
            "error": None,
            "error_type": None,
        },
        {
            "task_id": "task-005",
            "score": 1.0,
            "success": True,
            "steps": 2,
            "elapsed_seconds": 15.0,
            "error": None,
            "error_type": None,
        },
    ]
    lines = [json.dumps(r) for r in records]
    jsonl_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jsonl_path


# ---------------------------------------------------------------------------
# Format detection tests
# ---------------------------------------------------------------------------


class TestFormatDetection:
    def test_jsonl_file(self, full_eval_jsonl: Path):
        assert _detect_format(full_eval_jsonl) == _FORMAT_FULL_EVAL_JSONL

    def test_trajectory_dir(self, trajectory_dir: Path):
        assert _detect_format(trajectory_dir) == _FORMAT_TRAJECTORY_DIR

    def test_benchmark_dir(self, benchmark_dir: Path):
        assert _detect_format(benchmark_dir) == _FORMAT_BENCHMARK_DIR

    def test_mixed_dir(self, tmp_path: Path):
        d = tmp_path / "mixed"
        d.mkdir()
        (d / "some_file.png").write_bytes(b"\x89PNG")
        assert _detect_format(d) == _FORMAT_MIXED_DIR


# ---------------------------------------------------------------------------
# TraceAnalyzer loading tests
# ---------------------------------------------------------------------------


class TestTraceAnalyzerLoading:
    def test_load_full_eval_jsonl(self, full_eval_jsonl: Path):
        analyzer = TraceAnalyzer(full_eval_jsonl)
        assert len(analyzer.episodes) == 5
        assert analyzer.format == _FORMAT_FULL_EVAL_JSONL
        assert analyzer.run_metadata.get("planner_model") == "claude-sonnet-4-6"

    def test_load_trajectory_dir(self, trajectory_dir: Path):
        analyzer = TraceAnalyzer(trajectory_dir)
        assert len(analyzer.episodes) == 1
        assert analyzer.format == _FORMAT_TRAJECTORY_DIR
        ep = analyzer.episodes[0]
        assert ep.episode_id == "episode-001"
        assert len(ep.steps) == 2
        assert ep.steps[0].action_type == "click"
        assert ep.steps[0].target == "Start menu"

    def test_load_benchmark_dir(self, benchmark_dir: Path):
        analyzer = TraceAnalyzer(benchmark_dir)
        assert len(analyzer.episodes) == 2
        assert analyzer.format == _FORMAT_BENCHMARK_DIR

    def test_repr(self, full_eval_jsonl: Path):
        analyzer = TraceAnalyzer(full_eval_jsonl)
        r = repr(analyzer)
        assert "TraceAnalyzer" in r
        assert "episodes=5" in r


# ---------------------------------------------------------------------------
# Summary tests
# ---------------------------------------------------------------------------


class TestSummary:
    def test_summary_full_eval(self, full_eval_jsonl: Path):
        analyzer = TraceAnalyzer(full_eval_jsonl)
        s = analyzer.summary()
        assert s["total_episodes"] == 5
        assert s["outcome_episodes"] == 4
        assert s["unscored_episodes"] == 1
        assert s["success_rate"] == 0.5
        assert s["total_steps"] == 33
        assert s["avg_score"] == 0.625
        assert s["model"] == "claude-sonnet-4-6"
        assert s["cost_estimate_usd"] > 0

    def test_summary_empty(self, tmp_path: Path):
        empty = tmp_path / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        analyzer = TraceAnalyzer(empty)
        s = analyzer.summary()
        assert s["total_episodes"] == 0
        assert s["success_rate"] == 0.0

    def test_summary_status_breakdown(self, full_eval_jsonl: Path):
        analyzer = TraceAnalyzer(full_eval_jsonl)
        s = analyzer.summary()
        by_status = s["episodes_by_status"]
        assert by_status["passed"] == 2
        assert by_status["infra_error"] == 1

    def test_summary_trajectory_dir(self, trajectory_dir: Path):
        analyzer = TraceAnalyzer(trajectory_dir)
        s = analyzer.summary()
        assert s["total_episodes"] == 1
        assert s["total_steps"] == 2


# ---------------------------------------------------------------------------
# Failure mode tests
# ---------------------------------------------------------------------------


class TestFailureModes:
    def test_failure_classification(self, full_eval_jsonl: Path):
        analyzer = TraceAnalyzer(full_eval_jsonl)
        failures = analyzer.failure_modes()
        modes = {f["mode"] for f in failures}
        # task-002: 15 steps = timeout, task-003: infra error, task-004: partial
        assert "timeout" in modes
        assert "server_error" in modes
        assert "task_incomplete" in modes

    def test_classify_timeout(self):
        ep = Episode(
            episode_id="t1", score=0.0, success=False, num_steps=15
        )
        assert _classify_failure(ep, max_steps=15) == "timeout"

    def test_classify_server_error(self):
        ep = Episode(
            episode_id="t2",
            score=0.0,
            success=False,
            num_steps=2,
            error="Connection refused",
            error_type="infrastructure",
        )
        assert _classify_failure(ep, max_steps=15) == "server_error"

    def test_classify_agent_error(self):
        ep = Episode(
            episode_id="t3",
            score=0.0,
            success=False,
            num_steps=5,
            error="Agent failed",
            error_type="agent",
        )
        assert _classify_failure(ep, max_steps=15) == "agent_error"

    def test_classify_loop(self):
        steps = [
            StepRecord(step_index=i, action_type="click", target="Button X")
            for i in range(4)
        ]
        ep = Episode(
            episode_id="t4",
            score=0.0,
            success=False,
            num_steps=4,
            steps=steps,
        )
        assert _classify_failure(ep, max_steps=15) == "loop_detected"

    def test_classify_partial(self):
        ep = Episode(
            episode_id="t5", score=0.5, success=False, num_steps=8
        )
        assert _classify_failure(ep, max_steps=15) == "task_incomplete"

    def test_classify_success_returns_none(self):
        ep = Episode(
            episode_id="t6", score=1.0, success=True, num_steps=3
        )
        assert _classify_failure(ep, max_steps=15) is None

    def test_failure_percentages(self, full_eval_jsonl: Path):
        analyzer = TraceAnalyzer(full_eval_jsonl)
        failures = analyzer.failure_modes()
        for fm in failures:
            assert 0 <= fm["percentage"] <= 100


# ---------------------------------------------------------------------------
# Step timeline tests
# ---------------------------------------------------------------------------


class TestStepTimeline:
    def test_step_timeline_all(self, trajectory_dir: Path):
        analyzer = TraceAnalyzer(trajectory_dir)
        timeline = analyzer.step_timeline()
        assert len(timeline) == 2
        assert timeline[0]["action_type"] == "click"
        assert timeline[0]["target"] == "Start menu"

    def test_step_timeline_by_episode(self, trajectory_dir: Path):
        analyzer = TraceAnalyzer(trajectory_dir)
        timeline = analyzer.step_timeline("episode-001")
        assert len(timeline) == 2

    def test_step_timeline_nonexistent(self, trajectory_dir: Path):
        analyzer = TraceAnalyzer(trajectory_dir)
        timeline = analyzer.step_timeline("nonexistent")
        assert len(timeline) == 0


# ---------------------------------------------------------------------------
# Action distribution tests
# ---------------------------------------------------------------------------


class TestActionDistribution:
    def test_action_distribution(self, trajectory_dir: Path):
        analyzer = TraceAnalyzer(trajectory_dir)
        dist = analyzer.action_distribution()
        assert dist["click"] == 2


# ---------------------------------------------------------------------------
# Comparison tests
# ---------------------------------------------------------------------------


class TestComparison:
    def test_compare_basic(self, full_eval_jsonl: Path, second_eval_jsonl: Path):
        a1 = TraceAnalyzer(full_eval_jsonl)
        a2 = TraceAnalyzer(second_eval_jsonl)
        diff = a1.compare(a2)

        assert "improved" in diff
        assert "regressed" in diff
        assert "unchanged" in diff
        assert "summary_diff" in diff

        # task-002 improved (0.0 -> 1.0)
        improved_ids = [item["task_id"] for item in diff["improved"]]
        assert "task-002" in improved_ids

        # task-004 regressed (0.5 -> 0.0)
        regressed_ids = [item["task_id"] for item in diff["regressed"]]
        assert "task-004" in regressed_ids

    def test_compare_summary_diff(self, full_eval_jsonl: Path, second_eval_jsonl: Path):
        a1 = TraceAnalyzer(full_eval_jsonl)
        a2 = TraceAnalyzer(second_eval_jsonl)
        diff = a1.compare(a2)

        sd = diff["summary_diff"]
        assert "old" in sd
        assert "new" in sd
        assert "success_rate_delta" in sd


# ---------------------------------------------------------------------------
# HTML report generation tests
# ---------------------------------------------------------------------------


class TestReportGeneration:
    def test_generate_report(self, full_eval_jsonl: Path, tmp_path: Path):
        analyzer = TraceAnalyzer(full_eval_jsonl)
        output = tmp_path / "report.html"
        result = generate_report(analyzer=analyzer, output_path=output)
        assert result == output
        assert output.exists()
        content = output.read_text(encoding="utf-8")
        assert "<!DOCTYPE html>" in content
        assert "TRACE ANALYSIS" in content or "Trace Analysis" in content
        assert "Summary" in content

    def test_generate_report_with_comparison(
        self, full_eval_jsonl: Path, second_eval_jsonl: Path, tmp_path: Path
    ):
        a1 = TraceAnalyzer(full_eval_jsonl)
        a2 = TraceAnalyzer(second_eval_jsonl)
        output = tmp_path / "comparison.html"
        result = generate_report(
            analyzer=a1, output_path=output, compare_with=a2
        )
        assert result == output
        content = output.read_text(encoding="utf-8")
        assert "Comparison" in content
        assert "Improved" in content or "improved" in content

    def test_generate_report_via_analyzer(self, full_eval_jsonl: Path, tmp_path: Path):
        analyzer = TraceAnalyzer(full_eval_jsonl)
        output = tmp_path / "report2.html"
        result = analyzer.generate_report(output)
        assert result == output
        assert output.exists()

    def test_generate_report_with_steps(self, trajectory_dir: Path, tmp_path: Path):
        analyzer = TraceAnalyzer(trajectory_dir)
        output = tmp_path / "steps_report.html"
        generate_report(analyzer=analyzer, output_path=output)
        content = output.read_text(encoding="utf-8")
        assert "Step-by-Step" in content

    def test_report_creates_parent_dirs(self, full_eval_jsonl: Path, tmp_path: Path):
        output = tmp_path / "sub" / "dir" / "report.html"
        analyzer = TraceAnalyzer(full_eval_jsonl)
        generate_report(analyzer=analyzer, output_path=output)
        assert output.exists()

    def test_report_contains_score_chart(self, full_eval_jsonl: Path, tmp_path: Path):
        """Score Distribution bar chart should be present."""
        analyzer = TraceAnalyzer(full_eval_jsonl)
        output = tmp_path / "score_chart.html"
        generate_report(analyzer=analyzer, output_path=output)
        content = output.read_text(encoding="utf-8")
        assert "Score Distribution" in content
        assert "score-bar-fill" in content

    def test_report_contains_failure_analysis(self, full_eval_jsonl: Path, tmp_path: Path):
        """Failure Analysis section with example episode IDs."""
        analyzer = TraceAnalyzer(full_eval_jsonl)
        output = tmp_path / "failures.html"
        generate_report(analyzer=analyzer, output_path=output)
        content = output.read_text(encoding="utf-8")
        assert "Failure Analysis" in content
        # Should contain example episode IDs
        assert "Example Episodes" in content

    def test_report_contains_percentile_stats(self, full_eval_jsonl: Path, tmp_path: Path):
        """Summary should include median and percentile breakdowns."""
        analyzer = TraceAnalyzer(full_eval_jsonl)
        output = tmp_path / "percentiles.html"
        generate_report(analyzer=analyzer, output_path=output)
        content = output.read_text(encoding="utf-8")
        assert "Median Score" in content
        assert "Median Time" in content
        assert "P25" in content
        assert "P75" in content

    def test_report_contains_copy_markdown_button(self, full_eval_jsonl: Path, tmp_path: Path):
        """Copy as Markdown button should be present."""
        analyzer = TraceAnalyzer(full_eval_jsonl)
        output = tmp_path / "copy_md.html"
        generate_report(analyzer=analyzer, output_path=output)
        content = output.read_text(encoding="utf-8")
        assert "Copy as Markdown" in content
        assert "copyMarkdown" in content

    def test_report_contains_sort_indicators(self, full_eval_jsonl: Path, tmp_path: Path):
        """Table headers should have sort functionality."""
        analyzer = TraceAnalyzer(full_eval_jsonl)
        output = tmp_path / "sort.html"
        generate_report(analyzer=analyzer, output_path=output)
        content = output.read_text(encoding="utf-8")
        assert "sortTable" in content
        assert "sorted-asc" in content or "sorted-desc" in content

    def test_report_embeds_screenshots_as_base64(self, trajectory_dir: Path, tmp_path: Path):
        """Screenshots should be embedded as base64 data URIs."""
        analyzer = TraceAnalyzer(trajectory_dir)
        output = tmp_path / "embedded.html"
        generate_report(analyzer=analyzer, output_path=output)
        content = output.read_text(encoding="utf-8")
        # Should contain base64 data URI prefix (only if screenshots exist and are valid)
        # The fixture creates minimal PNG files that should be embeddable
        assert "data:image/png;base64," in content

    def test_report_inline_step_expansion(self, trajectory_dir: Path, tmp_path: Path):
        """Episode table rows with steps should be clickable for inline expansion."""
        analyzer = TraceAnalyzer(trajectory_dir)
        output = tmp_path / "inline_steps.html"
        generate_report(analyzer=analyzer, output_path=output)
        content = output.read_text(encoding="utf-8")
        assert "toggleInlineSteps" in content
        assert "step-detail-row" in content

    def test_report_comparison_side_by_side(
        self, full_eval_jsonl: Path, second_eval_jsonl: Path, tmp_path: Path
    ):
        """Comparison report should have side-by-side stat cards and task-level diff table."""
        a1 = TraceAnalyzer(full_eval_jsonl)
        a2 = TraceAnalyzer(second_eval_jsonl)
        output = tmp_path / "comparison_v2.html"
        generate_report(analyzer=a1, output_path=output, compare_with=a2)
        content = output.read_text(encoding="utf-8")
        assert "Run A" in content or "Baseline" in content
        assert "Run B" in content or "New Run" in content
        assert "Task-Level Changes" in content
        assert "Score Delta" in content
        assert "Steps Delta" in content

    def test_report_generated_timestamp(self, full_eval_jsonl: Path, tmp_path: Path):
        """Report should include generation timestamp."""
        analyzer = TraceAnalyzer(full_eval_jsonl)
        output = tmp_path / "timestamp.html"
        generate_report(analyzer=analyzer, output_path=output)
        content = output.read_text(encoding="utf-8")
        assert "Generated:" in content

    def test_report_lightbox(self, trajectory_dir: Path, tmp_path: Path):
        """Lightbox for screenshot zoom should be present."""
        analyzer = TraceAnalyzer(trajectory_dir)
        output = tmp_path / "lightbox.html"
        generate_report(analyzer=analyzer, output_path=output)
        content = output.read_text(encoding="utf-8")
        assert "lightbox" in content
        assert "openLightbox" in content
        assert "closeLightbox" in content

    def test_report_score_color_coding(self, full_eval_jsonl: Path, tmp_path: Path):
        """Score bars should be color-coded (green/yellow/red)."""
        analyzer = TraceAnalyzer(full_eval_jsonl)
        output = tmp_path / "colors.html"
        generate_report(analyzer=analyzer, output_path=output)
        content = output.read_text(encoding="utf-8")
        assert "score-high" in content  # green for >0.75
        assert "score-low" in content   # red for <0.25


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


class TestCLI:
    def test_cli_summary(self, full_eval_jsonl: Path, capsys):
        from openadapt_evals.analysis.cli import main

        ret = main([str(full_eval_jsonl)])
        assert ret == 0
        captured = capsys.readouterr()
        assert "TRACE ANALYSIS SUMMARY" in captured.out
        assert "Episodes:" in captured.out

    def test_cli_json_output(self, full_eval_jsonl: Path, capsys):
        from openadapt_evals.analysis.cli import main

        ret = main([str(full_eval_jsonl), "--json"])
        assert ret == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "summary" in data
        assert "failure_modes" in data

    def test_cli_report(self, full_eval_jsonl: Path, tmp_path: Path, capsys):
        from openadapt_evals.analysis.cli import main

        output = tmp_path / "cli_report.html"
        ret = main([str(full_eval_jsonl), "--report", str(output)])
        assert ret == 0
        assert output.exists()

    def test_cli_compare(self, full_eval_jsonl: Path, second_eval_jsonl: Path, capsys):
        from openadapt_evals.analysis.cli import main

        ret = main([str(full_eval_jsonl), "--compare", str(second_eval_jsonl)])
        assert ret == 0
        captured = capsys.readouterr()
        assert "RUN COMPARISON" in captured.out

    def test_cli_nonexistent_path(self, capsys):
        from openadapt_evals.analysis.cli import main

        ret = main(["/nonexistent/path.jsonl"])
        assert ret == 1

    def test_cli_compare_with_report(
        self, full_eval_jsonl: Path, second_eval_jsonl: Path, tmp_path: Path
    ):
        from openadapt_evals.analysis.cli import main

        output = tmp_path / "compare_report.html"
        ret = main([
            str(full_eval_jsonl),
            "--compare",
            str(second_eval_jsonl),
            "--report",
            str(output),
        ])
        assert ret == 0
        assert output.exists()
        content = output.read_text(encoding="utf-8")
        assert "Comparison" in content

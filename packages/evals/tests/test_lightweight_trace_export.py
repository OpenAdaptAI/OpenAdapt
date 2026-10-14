"""Tests for LightweightTraceExporter."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from openadapt_evals.benchmarks.trace_export import (
    LightweightTraceExporter,
    export_traces_lightweight,
)


@pytest.fixture
def benchmark_dir(tmp_path: Path) -> Path:
    """Create a fake benchmark results directory."""
    bm = tmp_path / "benchmark_results" / "test_run"
    bm.mkdir(parents=True)

    # metadata.json
    (bm / "metadata.json").write_text(
        json.dumps({
            "benchmark_name": "waa",
            "run_name": "test_run",
            "model_id": "test-model-v1",
            "created_at": "2026-03-16T00:00:00",
        })
    )

    # summary.json
    (bm / "summary.json").write_text(
        json.dumps({
            "num_tasks": 2,
            "num_success": 1,
            "success_rate": 0.5,
        })
    )

    # tasks directory with screenshots
    tasks_dir = bm / "tasks"
    tasks_dir.mkdir()

    for task_idx, (success, score) in enumerate([(True, 1.0), (False, 0.0)]):
        task_dir = tasks_dir / f"task_{task_idx:03d}"
        task_dir.mkdir()

        # task result JSON
        (task_dir / "result.json").write_text(
            json.dumps({
                "task_id": f"task_{task_idx:03d}",
                "definition": {
                    "instruction": f"Do thing {task_idx}",
                    "domain": "browser",
                },
                "execution": {
                    "success": success,
                    "score": score,
                    "total_time_seconds": 10.0 + task_idx,
                    "steps": [
                        {
                            "action": {
                                "type": "click",
                                "x": 960,
                                "y": 600,
                            },
                            "reasoning": "Clicking button",
                            "timestamp": "2026-03-16T00:00:01",
                        },
                        {
                            "action": {
                                "type": "type",
                                "text": "hello",
                            },
                        },
                    ],
                },
                "screenshots": [
                    f"tasks/task_{task_idx:03d}/step_000.png",
                    f"tasks/task_{task_idx:03d}/step_001.png",
                ],
            })
        )

        # Fake screenshot files
        (task_dir / "step_000.png").write_bytes(b"fake-png-0")
        (task_dir / "step_001.png").write_bytes(b"fake-png-1")

    return bm


@pytest.fixture
def output_dir(tmp_path: Path) -> Path:
    return tmp_path / "output"


def _mock_load_metadata(bm_dir):
    return json.loads((bm_dir / "metadata.json").read_text())


def _mock_load_summary(bm_dir):
    return json.loads((bm_dir / "summary.json").read_text())


def _mock_load_tasks(bm_dir):
    tasks = []
    tasks_dir = bm_dir / "tasks"
    if not tasks_dir.exists():
        return tasks
    for task_dir in sorted(tasks_dir.iterdir()):
        result_file = task_dir / "result.json"
        if result_file.exists():
            tasks.append(json.loads(result_file.read_text()))
    return tasks


class TestLightweightTraceExporter:
    @patch("openadapt_evals.benchmarks.trace_export.load_task_results", side_effect=_mock_load_tasks)
    @patch("openadapt_evals.benchmarks.trace_export.load_benchmark_summary", side_effect=_mock_load_summary)
    @patch("openadapt_evals.benchmarks.trace_export.load_benchmark_metadata", side_effect=_mock_load_metadata)
    def test_export_passed_only(self, mock_meta, mock_summary, mock_tasks, benchmark_dir, output_dir):
        """Exports only passed tasks when status_filter='passed'."""
        exporter = LightweightTraceExporter(
            benchmark_dir=benchmark_dir,
            output_dir=output_dir,
            status_filter="passed",
            copy_screenshots=False,
        )
        episodes = exporter.export()

        assert len(episodes) == 1
        assert episodes[0]["success"] is True
        assert episodes[0]["task_id"] == "task_000"

    @patch("openadapt_evals.benchmarks.trace_export.load_task_results", side_effect=_mock_load_tasks)
    @patch("openadapt_evals.benchmarks.trace_export.load_benchmark_summary", side_effect=_mock_load_summary)
    @patch("openadapt_evals.benchmarks.trace_export.load_benchmark_metadata", side_effect=_mock_load_metadata)
    def test_export_all(self, mock_meta, mock_summary, mock_tasks, benchmark_dir, output_dir):
        """Exports all tasks when status_filter='all'."""
        exporter = LightweightTraceExporter(
            benchmark_dir=benchmark_dir,
            output_dir=output_dir,
            status_filter="all",
            copy_screenshots=False,
        )
        episodes = exporter.export()

        assert len(episodes) == 2

    @patch("openadapt_evals.benchmarks.trace_export.load_task_results", side_effect=_mock_load_tasks)
    @patch("openadapt_evals.benchmarks.trace_export.load_benchmark_summary", side_effect=_mock_load_summary)
    @patch("openadapt_evals.benchmarks.trace_export.load_benchmark_metadata", side_effect=_mock_load_metadata)
    def test_episode_schema(self, mock_meta, mock_summary, mock_tasks, benchmark_dir, output_dir):
        """Exported episodes have the expected schema."""
        exporter = LightweightTraceExporter(
            benchmark_dir=benchmark_dir,
            output_dir=output_dir,
            status_filter="all",
            copy_screenshots=False,
        )
        episodes = exporter.export()

        ep = episodes[0]
        assert "episode_id" in ep
        assert "task_id" in ep
        assert "instruction" in ep
        assert "success" in ep
        assert "score" in ep
        assert "domain" in ep
        assert "agent_model" in ep
        assert "num_steps" in ep
        assert "steps" in ep
        assert "metadata" in ep

        step = ep["steps"][0]
        assert "step_index" in step
        assert "action" in step
        assert "type" in step["action"]

    @patch("openadapt_evals.benchmarks.trace_export.load_task_results", side_effect=_mock_load_tasks)
    @patch("openadapt_evals.benchmarks.trace_export.load_benchmark_summary", side_effect=_mock_load_summary)
    @patch("openadapt_evals.benchmarks.trace_export.load_benchmark_metadata", side_effect=_mock_load_metadata)
    def test_coordinate_normalization(self, mock_meta, mock_summary, mock_tasks, benchmark_dir, output_dir):
        """Pixel coordinates are normalized to [0,1]."""
        exporter = LightweightTraceExporter(
            benchmark_dir=benchmark_dir,
            output_dir=output_dir,
            status_filter="all",
            copy_screenshots=False,
            viewport_size=(1920, 1200),
        )
        episodes = exporter.export()

        click_step = episodes[0]["steps"][0]
        assert click_step["action"]["type"] == "click"
        assert click_step["action"]["x"] == pytest.approx(960 / 1920)
        assert click_step["action"]["y"] == pytest.approx(600 / 1200)

    @patch("openadapt_evals.benchmarks.trace_export.load_task_results", side_effect=_mock_load_tasks)
    @patch("openadapt_evals.benchmarks.trace_export.load_benchmark_summary", side_effect=_mock_load_summary)
    @patch("openadapt_evals.benchmarks.trace_export.load_benchmark_metadata", side_effect=_mock_load_metadata)
    def test_json_files_written(self, mock_meta, mock_summary, mock_tasks, benchmark_dir, output_dir):
        """Episode JSON files and manifest are written to disk."""
        exporter = LightweightTraceExporter(
            benchmark_dir=benchmark_dir,
            output_dir=output_dir,
            status_filter="all",
            copy_screenshots=False,
        )
        exporter.export()

        # Episode files
        episode_files = list((output_dir / "episodes").glob("*.json"))
        assert len(episode_files) == 2

        # Manifest
        manifest_path = output_dir / "manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())
        assert manifest["format"] == "lightweight-v1"
        assert len(manifest["episodes"]) == 2

        # JSONL
        jsonl_path = output_dir / "training_samples.jsonl"
        assert jsonl_path.exists()
        lines = jsonl_path.read_text().strip().split("\n")
        assert len(lines) == 4  # 2 tasks x 2 steps each

    @patch("openadapt_evals.benchmarks.trace_export.load_task_results", side_effect=_mock_load_tasks)
    @patch("openadapt_evals.benchmarks.trace_export.load_benchmark_summary", side_effect=_mock_load_summary)
    @patch("openadapt_evals.benchmarks.trace_export.load_benchmark_metadata", side_effect=_mock_load_metadata)
    def test_screenshot_copy(self, mock_meta, mock_summary, mock_tasks, benchmark_dir, output_dir):
        """Screenshots are copied when copy_screenshots=True."""
        exporter = LightweightTraceExporter(
            benchmark_dir=benchmark_dir,
            output_dir=output_dir,
            status_filter="passed",
            copy_screenshots=True,
        )
        exporter.export()

        screenshots_dir = output_dir / "screenshots" / "waa_task_000"
        assert screenshots_dir.exists()
        assert (screenshots_dir / "step_000.png").exists()
        assert (screenshots_dir / "step_001.png").exists()

    @patch("openadapt_evals.benchmarks.trace_export.load_task_results", side_effect=_mock_load_tasks)
    @patch("openadapt_evals.benchmarks.trace_export.load_benchmark_summary", side_effect=_mock_load_summary)
    @patch("openadapt_evals.benchmarks.trace_export.load_benchmark_metadata", side_effect=_mock_load_metadata)
    def test_jsonl_format(self, mock_meta, mock_summary, mock_tasks, benchmark_dir, output_dir):
        """JSONL lines are valid JSON with expected fields."""
        exporter = LightweightTraceExporter(
            benchmark_dir=benchmark_dir,
            output_dir=output_dir,
            status_filter="all",
            copy_screenshots=False,
        )
        exporter.export()

        jsonl_path = output_dir / "training_samples.jsonl"
        for line in jsonl_path.read_text().strip().split("\n"):
            sample = json.loads(line)
            assert "episode_id" in sample
            assert "task_id" in sample
            assert "instruction" in sample
            assert "step_index" in sample
            assert "action" in sample
            assert "success" in sample


class TestConvenienceFunction:
    @patch("openadapt_evals.benchmarks.trace_export.load_task_results", side_effect=_mock_load_tasks)
    @patch("openadapt_evals.benchmarks.trace_export.load_benchmark_summary", side_effect=_mock_load_summary)
    @patch("openadapt_evals.benchmarks.trace_export.load_benchmark_metadata", side_effect=_mock_load_metadata)
    def test_export_traces_lightweight(self, mock_meta, mock_summary, mock_tasks, benchmark_dir, output_dir):
        """Convenience function produces same results."""
        results = export_traces_lightweight(
            benchmark_dir=benchmark_dir,
            output_dir=output_dir,
            status_filter="all",
            copy_screenshots=False,
        )
        assert len(results) == 2

"""Export WAA benchmark traces as training data.

This module provides functionality to filter and export successful WAA benchmark
traces in a format suitable for VLM fine-tuning.

Two exporters are available:

- ``TraceExporter``: Converts traces to the openadapt-ml Episode format.
  Requires openadapt-ml as a dependency.
- ``LightweightTraceExporter``: Exports plain JSON + screenshots with no
  external ML dependencies. Useful for teams with their own training pipelines.

Usage:
    # Via CLI
    uv run python -m openadapt_evals.benchmarks.vm_cli export-traces --status passed --output training_data/

    # Via Python (openadapt-ml format)
    from openadapt_evals.benchmarks.trace_export import export_traces, TraceExporter

    exporter = TraceExporter(
        benchmark_dir=Path("benchmark_results/waa_eval_20241214"),
        output_dir=Path("training_data"),
        status_filter="passed",
    )
    episodes = exporter.export()

    # Via Python (lightweight JSON, no openadapt-ml dependency)
    from openadapt_evals.benchmarks.trace_export import export_traces_lightweight

    results = export_traces_lightweight(
        benchmark_dir="benchmark_results/waa_eval_20241214",
        output_dir="training_data_json",
        status_filter="passed",
    )

Directory structure created (lightweight):
    training_data_json/
    |-- episodes/
    |   |-- waa_task_001.json      # Plain JSON (no ML schema dependency)
    |-- screenshots/
    |   |-- waa_task_001/
    |   |   |-- step_000.png
    |-- manifest.json
    |-- training_samples.jsonl
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from openadapt_evals.benchmarks.viewer import (
    load_benchmark_metadata,
    load_benchmark_summary,
    load_task_results,
)

try:
    from openadapt_ml.schema import (
        Action,
        ActionType,
        BenchmarkSource,
        Coordinates,
        Episode,
        Observation,
        Step,
        save_episode,
    )
except ImportError:
    # openadapt-ml not installed — trace export requires it
    Action = None  # type: ignore[assignment, misc]
    ActionType = None  # type: ignore[assignment, misc]
    BenchmarkSource = None  # type: ignore[assignment, misc]
    Coordinates = None  # type: ignore[assignment, misc]
    Episode = None  # type: ignore[assignment, misc]
    Observation = None  # type: ignore[assignment, misc]
    Step = None  # type: ignore[assignment, misc]
    save_episode = None  # type: ignore[assignment, misc]

logger = logging.getLogger(__name__)


StatusFilter = Literal["passed", "failed", "all"]


@dataclass
class ExportStats:
    """Statistics from a trace export operation."""

    total_tasks: int = 0
    exported_tasks: int = 0
    skipped_tasks: int = 0
    total_steps: int = 0
    exported_screenshots: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class TraceExporter:
    """Export WAA benchmark traces as training data.

    Filters and converts benchmark execution traces to Episode format,
    copies screenshots, and creates training-ready data files.

    Args:
        benchmark_dir: Path to benchmark results directory containing metadata.json,
            summary.json, and tasks/ subdirectory.
        output_dir: Output directory for exported training data.
        status_filter: Filter by task status ("passed", "failed", "all").
        copy_screenshots: Whether to copy screenshots to output directory.
        create_jsonl: Whether to create training_samples.jsonl file.
        viewport_size: Default viewport size (width, height) for normalizing coordinates.
    """

    benchmark_dir: Path
    output_dir: Path
    status_filter: StatusFilter = "passed"
    copy_screenshots: bool = True
    create_jsonl: bool = True
    viewport_size: tuple[int, int] = (1920, 1200)

    def __post_init__(self):
        self.benchmark_dir = Path(self.benchmark_dir)
        self.output_dir = Path(self.output_dir)

    def export(self) -> list[Episode]:
        """Export traces according to configuration.

        Returns:
            List of Episode objects created from the traces.
        """
        # Load benchmark data
        metadata = load_benchmark_metadata(self.benchmark_dir)
        load_benchmark_summary(self.benchmark_dir)
        tasks = load_task_results(self.benchmark_dir)

        logger.info(
            f"Loaded {len(tasks)} tasks from {self.benchmark_dir.name} "
            f"(model: {metadata.get('model_id', 'unknown')})"
        )

        # Filter tasks
        filtered_tasks = self._filter_tasks(tasks)
        logger.info(f"Filtered to {len(filtered_tasks)} tasks with status={self.status_filter}")

        if not filtered_tasks:
            logger.warning("No tasks match the filter criteria")
            return []

        # Create output directories
        self._setup_output_dirs()

        # Convert and export
        episodes = []
        stats = ExportStats(total_tasks=len(tasks))

        for i, task in enumerate(filtered_tasks):
            try:
                episode = self._convert_task_to_episode(task, i, metadata)
                episodes.append(episode)

                # Save episode JSON
                episode_path = self.output_dir / "episodes" / f"{episode.episode_id}.json"
                save_episode(episode, episode_path)

                # Copy screenshots if enabled
                if self.copy_screenshots:
                    self._copy_task_screenshots(task, episode.episode_id)
                    stats.exported_screenshots += len(task.get("screenshots", []))

                stats.exported_tasks += 1
                stats.total_steps += len(episode.steps)

                logger.debug(
                    f"Exported episode {episode.episode_id}: "
                    f"{len(episode.steps)} steps, success={episode.success}"
                )

            except Exception as e:
                error_msg = f"Failed to export task {task.get('task_id', 'unknown')}: {e}"
                logger.error(error_msg)
                stats.errors.append(error_msg)
                stats.skipped_tasks += 1

        # Create manifest
        self._create_manifest(episodes, metadata, stats)

        # Create JSONL training file
        if self.create_jsonl:
            self._create_training_jsonl(episodes)

        # Log summary
        logger.info(
            f"Export complete: {stats.exported_tasks}/{stats.total_tasks} tasks, "
            f"{stats.total_steps} steps, {stats.exported_screenshots} screenshots"
        )
        if stats.errors:
            logger.warning(f"{len(stats.errors)} errors during export")

        return episodes

    def _filter_tasks(self, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Filter tasks by status.

        Args:
            tasks: List of task dictionaries from load_task_results.

        Returns:
            Filtered list of tasks.
        """
        if self.status_filter == "all":
            return tasks

        filtered = []
        for task in tasks:
            execution = task.get("execution", {})
            success = execution.get("success", False)

            if self.status_filter == "passed" and success:
                filtered.append(task)
            elif self.status_filter == "failed" and not success:
                filtered.append(task)

        return filtered

    def _setup_output_dirs(self) -> None:
        """Create output directory structure."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "episodes").mkdir(exist_ok=True)
        if self.copy_screenshots:
            (self.output_dir / "screenshots").mkdir(exist_ok=True)

    def _convert_task_to_episode(
        self,
        task: dict[str, Any],
        index: int,
        metadata: dict[str, Any],
    ) -> Episode:
        """Convert a benchmark task to Episode format.

        Args:
            task: Task dictionary from load_task_results.
            index: Task index for episode ID generation.
            metadata: Benchmark metadata.

        Returns:
            Episode instance.
        """
        definition = task.get("definition", {})
        execution = task.get("execution", {})
        screenshots = task.get("screenshots", [])
        execution_steps = execution.get("steps", [])

        task_id = task.get("task_id", f"task_{index:03d}")
        episode_id = f"waa_{task_id}"

        # Convert execution steps to Episode steps
        steps = []
        for step_idx, step_data in enumerate(execution_steps):
            step = self._convert_step(step_data, step_idx, screenshots)
            steps.append(step)

        return Episode(
            episode_id=episode_id,
            task_id=task_id,
            instruction=definition.get("instruction", ""),
            goal=definition.get("instruction", ""),
            steps=steps,
            success=execution.get("success", False),
            final_reward=execution.get("score", 0.0),
            source=BenchmarkSource.WAA,
            source_file=str(self.benchmark_dir / "tasks" / task_id),
            agent_model=metadata.get("model_id", "unknown"),
            environment="Windows 11",
            tags=[
                definition.get("domain", "unknown"),
                "waa",
                "benchmark",
            ],
            metadata={
                "benchmark_name": metadata.get("benchmark_name", "waa"),
                "run_name": metadata.get("run_name"),
                "domain": definition.get("domain"),
                "num_steps": execution.get("num_steps", len(steps)),
                "total_time_seconds": execution.get("total_time_seconds"),
                "error": execution.get("error"),
                "reason": execution.get("reason"),
                "evaluation_spec": definition.get("evaluation_spec"),
            },
        )

    def _convert_step(
        self,
        step_data: dict[str, Any],
        step_idx: int,
        screenshots: list[str],
    ) -> Step:
        """Convert a benchmark execution step to Episode Step format.

        Args:
            step_data: Step data from execution.json.
            step_idx: Step index.
            screenshots: List of screenshot paths.

        Returns:
            Step instance.
        """
        action_data = step_data.get("action", {})

        # Build observation
        screenshot_path = None
        if step_idx < len(screenshots):
            screenshot_path = screenshots[step_idx]
        elif step_data.get("screenshot_path"):
            screenshot_path = step_data["screenshot_path"]

        observation = Observation(
            screenshot_path=screenshot_path,
            screen_size=self.viewport_size,
        )

        # Convert action type
        action_type = self._map_action_type(action_data.get("type", "click"))

        # Build action with coordinates
        action_kwargs: dict[str, Any] = {
            "type": action_type,
            "raw": action_data,
        }

        # Handle coordinates - convert to normalized if pixel values
        x = action_data.get("x")
        y = action_data.get("y")
        if x is not None and y is not None:
            # Check if already normalized (0-1 range)
            if 0 <= x <= 1 and 0 <= y <= 1:
                action_kwargs["normalized_coordinates"] = (x, y)
            else:
                # Assume pixel coordinates, normalize
                norm_x = x / self.viewport_size[0]
                norm_y = y / self.viewport_size[1]
                action_kwargs["normalized_coordinates"] = (norm_x, norm_y)
                # Also store pixel coordinates
                action_kwargs["coordinates"] = Coordinates(x=int(x), y=int(y))

        # Handle text for type action
        if action_data.get("text"):
            action_kwargs["text"] = action_data["text"]

        # Handle key for key action
        if action_data.get("key"):
            action_kwargs["key"] = action_data["key"]

        # Handle modifiers
        if action_data.get("modifiers"):
            action_kwargs["modifiers"] = action_data["modifiers"]

        # Handle scroll
        if action_data.get("scroll_direction"):
            action_kwargs["scroll_direction"] = action_data["scroll_direction"]
        if action_data.get("scroll_amount"):
            action_kwargs["scroll_amount"] = int(action_data["scroll_amount"])

        # Handle drag end coordinates
        end_x = action_data.get("end_x")
        end_y = action_data.get("end_y")
        if end_x is not None and end_y is not None:
            if 0 <= end_x <= 1 and 0 <= end_y <= 1:
                action_kwargs["normalized_end"] = (end_x, end_y)
            else:
                norm_end_x = end_x / self.viewport_size[0]
                norm_end_y = end_y / self.viewport_size[1]
                action_kwargs["normalized_end"] = (norm_end_x, norm_end_y)
                action_kwargs["end_coordinates"] = Coordinates(x=int(end_x), y=int(end_y))

        # Handle element targeting
        if action_data.get("target_node_id"):
            from openadapt_ml.schema import UIElement

            action_kwargs["element"] = UIElement(
                element_id=action_data.get("target_node_id"),
                role=action_data.get("target_role"),
                name=action_data.get("target_name"),
            )

        action = Action(**action_kwargs)

        return Step(
            step_index=step_idx,
            observation=observation,
            action=action,
            reasoning=step_data.get("reasoning"),
            timestamp=step_data.get("timestamp"),
        )

    def _map_action_type(self, action_type_str: str) -> ActionType:
        """Map benchmark action type string to ActionType enum.

        Args:
            action_type_str: Action type string from benchmark.

        Returns:
            ActionType enum value.
        """
        mapping = {
            "click": ActionType.CLICK,
            "double_click": ActionType.DOUBLE_CLICK,
            "right_click": ActionType.RIGHT_CLICK,
            "type": ActionType.TYPE,
            "key": ActionType.KEY,
            "scroll": ActionType.SCROLL,
            "drag": ActionType.DRAG,
            "hover": ActionType.HOVER,
            "wait": ActionType.WAIT,
            "done": ActionType.DONE,
            "answer": ActionType.DONE,
            "failed": ActionType.FAIL,
            "fail": ActionType.FAIL,
        }
        return mapping.get(action_type_str.lower(), ActionType.CLICK)

    def _copy_task_screenshots(self, task: dict[str, Any], episode_id: str) -> None:
        """Copy task screenshots to output directory.

        Args:
            task: Task dictionary.
            episode_id: Episode ID for output subdirectory.
        """
        screenshots = task.get("screenshots", [])
        if not screenshots:
            return

        # Create episode screenshot directory
        episode_screenshots_dir = self.output_dir / "screenshots" / episode_id
        episode_screenshots_dir.mkdir(parents=True, exist_ok=True)

        for i, rel_path in enumerate(screenshots):
            src_path = self.benchmark_dir / rel_path
            if src_path.exists():
                dest_path = episode_screenshots_dir / f"step_{i:03d}.png"
                shutil.copy2(src_path, dest_path)

    def _create_manifest(
        self,
        episodes: list[Episode],
        metadata: dict[str, Any],
        stats: ExportStats,
    ) -> None:
        """Create manifest.json with export metadata.

        Args:
            episodes: List of exported episodes.
            metadata: Benchmark metadata.
            stats: Export statistics.
        """
        manifest = {
            "export_timestamp": datetime.utcnow().isoformat(),
            "source_benchmark": metadata.get("benchmark_name", "waa"),
            "source_run": metadata.get("run_name"),
            "source_model": metadata.get("model_id"),
            "status_filter": self.status_filter,
            "statistics": {
                "total_tasks": stats.total_tasks,
                "exported_tasks": stats.exported_tasks,
                "skipped_tasks": stats.skipped_tasks,
                "total_steps": stats.total_steps,
                "exported_screenshots": stats.exported_screenshots,
                "errors": len(stats.errors),
            },
            "episodes": [
                {
                    "episode_id": ep.episode_id,
                    "task_id": ep.task_id,
                    "instruction": ep.instruction,
                    "num_steps": len(ep.steps),
                    "success": ep.success,
                    "domain": ep.metadata.get("domain") if ep.metadata else None,
                }
                for ep in episodes
            ],
        }

        manifest_path = self.output_dir / "manifest.json"
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)

        logger.info(f"Created manifest: {manifest_path}")

    def _create_training_jsonl(self, episodes: list[Episode]) -> None:
        """Create JSONL file for training.

        Each line contains a training sample with:
        - instruction: Task instruction
        - screenshot_path: Path to screenshot
        - action: Action taken
        - reasoning: Optional reasoning

        Args:
            episodes: List of exported episodes.
        """
        jsonl_path = self.output_dir / "training_samples.jsonl"

        with open(jsonl_path, "w") as f:
            for episode in episodes:
                for step in episode.steps:
                    sample = {
                        "episode_id": episode.episode_id,
                        "task_id": episode.task_id,
                        "instruction": episode.instruction,
                        "step_index": step.step_index,
                        "screenshot_path": step.observation.screenshot_path,
                        "action_type": step.action.type.value,
                        "action": {
                            "type": step.action.type.value,
                            "coordinates": (
                                {
                                    "x": step.action.coordinates.x,
                                    "y": step.action.coordinates.y,
                                }
                                if step.action.coordinates
                                else None
                            ),
                            "normalized_coordinates": step.action.normalized_coordinates,
                            "text": step.action.text,
                            "key": step.action.key,
                            "modifiers": step.action.modifiers,
                            "scroll_direction": step.action.scroll_direction,
                            "scroll_amount": step.action.scroll_amount,
                        },
                        "reasoning": step.reasoning,
                        "domain": episode.metadata.get("domain") if episode.metadata else None,
                        "success": episode.success,
                    }
                    f.write(json.dumps(sample) + "\n")

        logger.info(f"Created training JSONL: {jsonl_path}")


def export_traces(
    benchmark_dir: str | Path,
    output_dir: str | Path,
    status_filter: StatusFilter = "passed",
    copy_screenshots: bool = True,
    create_jsonl: bool = True,
    viewport_size: tuple[int, int] = (1920, 1200),
) -> list[Episode]:
    """Convenience function to export benchmark traces.

    Args:
        benchmark_dir: Path to benchmark results directory.
        output_dir: Output directory for exported training data.
        status_filter: Filter by task status ("passed", "failed", "all").
        copy_screenshots: Whether to copy screenshots to output directory.
        create_jsonl: Whether to create training_samples.jsonl file.
        viewport_size: Default viewport size for normalizing coordinates.

    Returns:
        List of Episode objects created from the traces.

    Example:
        episodes = export_traces(
            benchmark_dir="benchmark_results/waa_eval_20241214",
            output_dir="training_data",
            status_filter="passed",
        )
        print(f"Exported {len(episodes)} episodes")
    """
    exporter = TraceExporter(
        benchmark_dir=Path(benchmark_dir),
        output_dir=Path(output_dir),
        status_filter=status_filter,
        copy_screenshots=copy_screenshots,
        create_jsonl=create_jsonl,
        viewport_size=viewport_size,
    )
    return exporter.export()


def list_available_runs(
    benchmark_results_dir: str | Path = "benchmark_results",
) -> list[dict[str, Any]]:
    """List available benchmark runs for export.

    Args:
        benchmark_results_dir: Base directory containing benchmark results.

    Returns:
        List of dictionaries with run information.
    """
    results_dir = Path(benchmark_results_dir)
    if not results_dir.exists():
        return []

    runs = []
    for run_dir in sorted(results_dir.iterdir()):
        if not run_dir.is_dir():
            continue

        metadata_path = run_dir / "metadata.json"
        summary_path = run_dir / "summary.json"

        run_info = {
            "run_name": run_dir.name,
            "path": str(run_dir),
        }

        if metadata_path.exists():
            with open(metadata_path) as f:
                metadata = json.load(f)
            run_info.update(
                {
                    "benchmark_name": metadata.get("benchmark_name"),
                    "model_id": metadata.get("model_id"),
                    "created_at": metadata.get("created_at"),
                }
            )

        if summary_path.exists():
            with open(summary_path) as f:
                summary = json.load(f)
            run_info.update(
                {
                    "num_tasks": summary.get("num_tasks", 0),
                    "num_success": summary.get("num_success", 0),
                    "success_rate": summary.get("success_rate", 0.0),
                }
            )

        runs.append(run_info)

    return runs


# ---------------------------------------------------------------------------
# Lightweight exporter (no openadapt-ml dependency)
# ---------------------------------------------------------------------------


@dataclass
class LightweightTraceExporter:
    """Export benchmark traces as plain JSON with no ML framework dependencies.

    Produces simple JSON files with screenshots, actions, and metadata that
    any training pipeline can consume. Does not import from openadapt-ml.

    Args:
        benchmark_dir: Path to benchmark results directory.
        output_dir: Output directory for exported data.
        status_filter: Filter by task status ("passed", "failed", "all").
        copy_screenshots: Whether to copy screenshots to output directory.
        viewport_size: Default viewport size for coordinate normalization.
    """

    benchmark_dir: Path
    output_dir: Path
    status_filter: StatusFilter = "passed"
    copy_screenshots: bool = True
    viewport_size: tuple[int, int] = (1920, 1200)

    def __post_init__(self):
        self.benchmark_dir = Path(self.benchmark_dir)
        self.output_dir = Path(self.output_dir)

    def export(self) -> list[dict[str, Any]]:
        """Export traces as plain JSON dicts.

        Returns:
            List of episode dicts (same structure as the JSON files written).
        """
        metadata = load_benchmark_metadata(self.benchmark_dir)
        load_benchmark_summary(self.benchmark_dir)
        tasks = load_task_results(self.benchmark_dir)

        logger.info(
            "LightweightTraceExporter: %d tasks from %s",
            len(tasks),
            self.benchmark_dir.name,
        )

        filtered = self._filter_tasks(tasks)
        logger.info("Filtered to %d tasks (status=%s)", len(filtered), self.status_filter)

        if not filtered:
            logger.warning("No tasks match the filter criteria")
            return []

        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "episodes").mkdir(exist_ok=True)
        if self.copy_screenshots:
            (self.output_dir / "screenshots").mkdir(exist_ok=True)

        episodes: list[dict[str, Any]] = []
        stats = ExportStats(total_tasks=len(tasks))

        for i, task in enumerate(filtered):
            try:
                episode = self._convert_task(task, i, metadata)
                episodes.append(episode)

                episode_path = self.output_dir / "episodes" / f"{episode['episode_id']}.json"
                with open(episode_path, "w") as f:
                    json.dump(episode, f, indent=2)

                if self.copy_screenshots:
                    self._copy_screenshots(task, episode["episode_id"])

                stats.exported_tasks += 1
                stats.total_steps += len(episode.get("steps", []))
            except Exception as e:
                error_msg = f"Failed to export task {task.get('task_id', 'unknown')}: {e}"
                logger.error(error_msg)
                stats.errors.append(error_msg)
                stats.skipped_tasks += 1

        self._write_manifest(episodes, metadata, stats)
        self._write_jsonl(episodes)

        logger.info(
            "Lightweight export complete: %d/%d tasks, %d steps",
            stats.exported_tasks,
            stats.total_tasks,
            stats.total_steps,
        )
        return episodes

    def _filter_tasks(self, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self.status_filter == "all":
            return tasks
        filtered = []
        for task in tasks:
            success = task.get("execution", {}).get("success", False)
            if self.status_filter == "passed" and success:
                filtered.append(task)
            elif self.status_filter == "failed" and not success:
                filtered.append(task)
        return filtered

    def _convert_task(
        self,
        task: dict[str, Any],
        index: int,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        definition = task.get("definition", {})
        execution = task.get("execution", {})
        screenshots = task.get("screenshots", [])
        execution_steps = execution.get("steps", [])

        task_id = task.get("task_id", f"task_{index:03d}")
        episode_id = f"waa_{task_id}"

        steps = []
        for step_idx, step_data in enumerate(execution_steps):
            action_data = step_data.get("action", {})

            screenshot_rel = None
            if step_idx < len(screenshots):
                screenshot_rel = f"screenshots/{episode_id}/step_{step_idx:03d}.png"
            elif step_data.get("screenshot_path"):
                screenshot_rel = step_data["screenshot_path"]

            # Normalize coordinates to [0,1] if they look like pixel values
            x = action_data.get("x")
            y = action_data.get("y")
            if x is not None and y is not None:
                if not (0 <= x <= 1 and 0 <= y <= 1):
                    x = x / self.viewport_size[0]
                    y = y / self.viewport_size[1]

            steps.append({
                "step_index": step_idx,
                "screenshot": screenshot_rel,
                "action": {
                    "type": action_data.get("type", "click"),
                    "x": x,
                    "y": y,
                    "text": action_data.get("text"),
                    "key": action_data.get("key"),
                    "modifiers": action_data.get("modifiers"),
                    "scroll_direction": action_data.get("scroll_direction"),
                    "target_node_id": action_data.get("target_node_id"),
                },
                "reasoning": step_data.get("reasoning"),
                "timestamp": step_data.get("timestamp"),
            })

        return {
            "episode_id": episode_id,
            "task_id": task_id,
            "instruction": definition.get("instruction", ""),
            "success": execution.get("success", False),
            "score": execution.get("score", 0.0),
            "domain": definition.get("domain"),
            "agent_model": metadata.get("model_id"),
            "num_steps": len(steps),
            "steps": steps,
            "metadata": {
                "benchmark_name": metadata.get("benchmark_name", "waa"),
                "run_name": metadata.get("run_name"),
                "total_time_seconds": execution.get("total_time_seconds"),
            },
        }

    def _copy_screenshots(self, task: dict[str, Any], episode_id: str) -> None:
        screenshots = task.get("screenshots", [])
        if not screenshots:
            return
        dest_dir = self.output_dir / "screenshots" / episode_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        for i, rel_path in enumerate(screenshots):
            src = self.benchmark_dir / rel_path
            if src.exists():
                shutil.copy2(src, dest_dir / f"step_{i:03d}.png")

    def _write_manifest(
        self,
        episodes: list[dict[str, Any]],
        metadata: dict[str, Any],
        stats: ExportStats,
    ) -> None:
        manifest = {
            "export_timestamp": datetime.now(timezone.utc).isoformat(),
            "format": "lightweight-v1",
            "source_benchmark": metadata.get("benchmark_name", "waa"),
            "source_run": metadata.get("run_name"),
            "source_model": metadata.get("model_id"),
            "status_filter": self.status_filter,
            "statistics": {
                "total_tasks": stats.total_tasks,
                "exported_tasks": stats.exported_tasks,
                "skipped_tasks": stats.skipped_tasks,
                "total_steps": stats.total_steps,
                "errors": len(stats.errors),
            },
            "episodes": [
                {
                    "episode_id": ep["episode_id"],
                    "task_id": ep["task_id"],
                    "instruction": ep["instruction"],
                    "num_steps": ep["num_steps"],
                    "success": ep["success"],
                    "domain": ep.get("domain"),
                }
                for ep in episodes
            ],
        }
        with open(self.output_dir / "manifest.json", "w") as f:
            json.dump(manifest, f, indent=2)

    def _write_jsonl(self, episodes: list[dict[str, Any]]) -> None:
        jsonl_path = self.output_dir / "training_samples.jsonl"
        with open(jsonl_path, "w") as f:
            for ep in episodes:
                for step in ep.get("steps", []):
                    sample = {
                        "episode_id": ep["episode_id"],
                        "task_id": ep["task_id"],
                        "instruction": ep["instruction"],
                        "step_index": step["step_index"],
                        "screenshot": step["screenshot"],
                        "action": step["action"],
                        "reasoning": step.get("reasoning"),
                        "success": ep["success"],
                        "domain": ep.get("domain"),
                    }
                    f.write(json.dumps(sample) + "\n")


def export_traces_lightweight(
    benchmark_dir: str | Path,
    output_dir: str | Path,
    status_filter: StatusFilter = "passed",
    copy_screenshots: bool = True,
    viewport_size: tuple[int, int] = (1920, 1200),
) -> list[dict[str, Any]]:
    """Export benchmark traces as plain JSON (no openadapt-ml dependency).

    Args:
        benchmark_dir: Path to benchmark results directory.
        output_dir: Output directory for exported data.
        status_filter: Filter by task status ("passed", "failed", "all").
        copy_screenshots: Whether to copy screenshots to output directory.
        viewport_size: Default viewport size for coordinate normalization.

    Returns:
        List of episode dicts.
    """
    exporter = LightweightTraceExporter(
        benchmark_dir=Path(benchmark_dir),
        output_dir=Path(output_dir),
        status_filter=status_filter,
        copy_screenshots=copy_screenshots,
        viewport_size=viewport_size,
    )
    return exporter.export()

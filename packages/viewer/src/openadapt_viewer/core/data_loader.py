"""Data loading utilities for openadapt-viewer.

This module provides functions for loading benchmark and training data
from various file formats (JSON, directories with images, etc.).
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from openadapt_viewer.core.types import (
    BenchmarkRun,
    BenchmarkTask,
    ExecutionStep,
    TaskExecution,
)


class DataLoader:
    """Utility class for loading viewer data from files."""

    @staticmethod
    def load_json(path: Path | str) -> dict[str, Any]:
        """Load a JSON file and return its contents.

        Args:
            path: Path to the JSON file

        Returns:
            Parsed JSON data as a dictionary

        Raises:
            FileNotFoundError: If the file doesn't exist
            json.JSONDecodeError: If the file contains invalid JSON
        """
        path = Path(path)
        with open(path) as f:
            return json.load(f)

    @staticmethod
    def parse_datetime(value: Any) -> datetime | None:
        """Parse a datetime value from various formats.

        Args:
            value: A string, float (timestamp), or datetime object

        Returns:
            Parsed datetime or None if parsing fails
        """
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, (int, float)):
            # .astimezone() attaches the local zone without moving the wall
            # clock: the rendered time is the one the run happened at on this
            # machine, exactly as before, but now unambiguous.
            return datetime.fromtimestamp(value).astimezone()
        if isinstance(value, str):
            # Try ISO format first. An offset-bearing string ("...Z", "...+02:00")
            # is already aware and is left alone; a naive one is anchored to the
            # local zone, again without moving the wall clock.
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                pass
            else:
                return parsed if parsed.tzinfo else parsed.astimezone()
            # Try common formats
            for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"]:
                try:
                    return datetime.strptime(value, fmt).astimezone()
                except ValueError:
                    continue
        return None

    @classmethod
    def load_benchmark_run(cls, run_dir: Path | str) -> BenchmarkRun:
        """Load a complete benchmark run from a directory.

        Expected directory structure:
            run_dir/
            ├── metadata.json       # Run metadata
            ├── summary.json        # Aggregate metrics (optional)
            └── tasks/
                ├── task_001/
                │   ├── task.json       # Task definition
                │   ├── execution.json  # Execution trace
                │   └── screenshots/    # Step screenshots
                └── task_002/
                    └── ...

        Args:
            run_dir: Path to the benchmark run directory

        Returns:
            BenchmarkRun object with all data loaded
        """
        run_dir = Path(run_dir)

        # Load metadata
        metadata_path = run_dir / "metadata.json"
        if metadata_path.exists():
            metadata = cls.load_json(metadata_path)
        else:
            metadata = {}

        # Create BenchmarkRun with metadata
        run = BenchmarkRun(
            run_id=metadata.get("run_id", run_dir.name),
            benchmark_name=metadata.get("benchmark_name", "unknown"),
            model_id=metadata.get("model_id", "unknown"),
            start_time=cls.parse_datetime(metadata.get("start_time")),
            end_time=cls.parse_datetime(metadata.get("end_time")),
            config=metadata.get("config", {}),
        )

        # Load tasks and executions
        tasks_dir = run_dir / "tasks"
        if tasks_dir.exists():
            for task_dir in sorted(tasks_dir.iterdir()):
                if not task_dir.is_dir():
                    continue

                task, execution = cls._load_task_from_dir(task_dir)
                if task:
                    run.tasks.append(task)
                if execution:
                    run.executions.append(execution)

        return run

    @classmethod
    def _load_task_from_dir(
        cls, task_dir: Path
    ) -> tuple[BenchmarkTask | None, TaskExecution | None]:
        """Load a single task and its execution from a directory.

        Args:
            task_dir: Path to the task directory

        Returns:
            Tuple of (BenchmarkTask, TaskExecution), either may be None
        """
        task = None
        execution = None

        # Load task definition
        task_json = task_dir / "task.json"
        if task_json.exists():
            task_data = cls.load_json(task_json)
            task = BenchmarkTask(
                task_id=task_data.get("task_id", task_dir.name),
                instruction=task_data.get("instruction", ""),
                domain=task_data.get("domain"),
                difficulty=task_data.get("difficulty"),
                time_limit=task_data.get("time_limit"),
                metadata=task_data.get("metadata", {}),
            )

        # Load execution trace
        exec_json = task_dir / "execution.json"
        if exec_json.exists():
            exec_data = cls.load_json(exec_json)
            execution = cls._parse_execution(exec_data, task_dir)

        return task, execution

    @classmethod
    def _parse_execution(cls, exec_data: dict[str, Any], task_dir: Path) -> TaskExecution:
        """Parse execution data into a TaskExecution object.

        Args:
            exec_data: Raw execution data from JSON
            task_dir: Directory containing screenshots

        Returns:
            Parsed TaskExecution object
        """
        steps = []
        screenshots_dir = task_dir / "screenshots"

        for i, step_data in enumerate(exec_data.get("steps", [])):
            # Try to find screenshot for this step
            screenshot_path = None
            if screenshots_dir.exists():
                for ext in [".png", ".jpg", ".jpeg"]:
                    candidate = screenshots_dir / f"step_{i:03d}{ext}"
                    if candidate.exists():
                        screenshot_path = str(candidate.relative_to(task_dir.parent.parent))
                        break

            step = ExecutionStep(
                step_number=i,
                timestamp=cls.parse_datetime(step_data.get("timestamp")),
                screenshot_path=screenshot_path or step_data.get("screenshot_path"),
                action_type=step_data.get("action_type", "unknown"),
                action_details=step_data.get("action_details", {}),
                reasoning=step_data.get("reasoning"),
                raw_output=step_data.get("raw_output"),
            )
            steps.append(step)

        return TaskExecution(
            task_id=exec_data.get("task_id", "unknown"),
            start_time=cls.parse_datetime(exec_data.get("start_time")),
            end_time=cls.parse_datetime(exec_data.get("end_time")),
            steps=steps,
            success=exec_data.get("success", False),
            error=exec_data.get("error"),
        )

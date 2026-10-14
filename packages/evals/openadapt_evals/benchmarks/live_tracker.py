"""Live evaluation progress tracker for benchmark viewer.

This module provides a tracker that writes real-time evaluation progress
to a JSON file that the viewer can poll via /api/benchmark-live.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from openadapt_evals.adapters import (
    BenchmarkAction,
    BenchmarkObservation,
    BenchmarkResult,
    BenchmarkTask,
)


@dataclass
class LiveStepData:
    """Data for a single step in live evaluation."""

    step_idx: int
    action: dict[str, Any]
    reasoning: str | None = None
    screenshot_url: str | None = None


@dataclass
class LiveTaskData:
    """Data for current task being evaluated."""

    task_id: str
    instruction: str
    domain: str
    steps: list[LiveStepData]
    result: dict[str, Any] | None = None


class LiveEvaluationTracker:
    """Tracks live evaluation progress and writes to benchmark_live.json.

    This class is designed to be used alongside ExecutionTraceCollector
    to provide real-time progress updates to the viewer.

    Args:
        output_file: Path to output JSON file (default: benchmark_live.json).
        total_tasks: Total number of tasks to evaluate.
    """

    def __init__(
        self,
        output_file: str | Path = "benchmark_live.json",
        total_tasks: int = 0,
    ):
        self.output_file = Path(output_file)
        self.total_tasks = total_tasks
        self.tasks_completed = 0
        self.current_task: LiveTaskData | None = None

        # Initialize with idle state
        self._write_state({"status": "idle"})

    def start_task(self, task: BenchmarkTask) -> None:
        """Start tracking a new task.

        Args:
            task: The benchmark task being evaluated.
        """
        self.current_task = LiveTaskData(
            task_id=task.task_id,
            instruction=task.instruction,
            domain=task.domain or "unknown",
            steps=[],
            result=None,
        )

        self._write_state({
            "status": "running",
            "total_tasks": self.total_tasks,
            "tasks_completed": self.tasks_completed,
            "current_task": asdict(self.current_task),
        })

    def record_step(
        self,
        step_idx: int,
        observation: BenchmarkObservation,
        action: BenchmarkAction,
        reasoning: str | None = None,
    ) -> None:
        """Record a step in the current task.

        Args:
            step_idx: Index of this step.
            observation: Observation at this step.
            action: Action taken at this step.
            reasoning: Optional reasoning/thought from agent.
        """
        if self.current_task is None:
            raise RuntimeError("No task started. Call start_task() first.")

        # Serialize action
        action_data = {
            "type": action.type,
            "x": action.x,
            "y": action.y,
            "target_node_id": action.target_node_id,
            "text": action.text,
            "key": action.key,
        }

        # Create step data
        step = LiveStepData(
            step_idx=step_idx,
            action=action_data,
            reasoning=reasoning,
            screenshot_url=None,  # Could be populated if we serve screenshots
        )

        self.current_task.steps.append(step)

        # Write updated state
        self._write_state({
            "status": "running",
            "total_tasks": self.total_tasks,
            "tasks_completed": self.tasks_completed,
            "current_task": asdict(self.current_task),
        })

    def finish_task(self, result: BenchmarkResult) -> None:
        """Finish tracking the current task.

        Args:
            result: The evaluation result for the task.
        """
        if self.current_task is None:
            raise RuntimeError("No task started. Call start_task() first.")

        # Add result to current task
        self.current_task.result = {
            "success": result.success,
            "score": result.score,
            "num_steps": result.num_steps,
            "total_time_seconds": result.total_time_seconds,
        }

        # Increment completed count
        self.tasks_completed += 1

        # Write updated state
        self._write_state({
            "status": "running",
            "total_tasks": self.total_tasks,
            "tasks_completed": self.tasks_completed,
            "current_task": asdict(self.current_task),
        })

        # Clear current task
        self.current_task = None

    def finish(self) -> None:
        """Mark evaluation as complete."""
        self._write_state({
            "status": "complete",
            "total_tasks": self.total_tasks,
            "tasks_completed": self.tasks_completed,
        })

    def _write_state(self, state: dict[str, Any]) -> None:
        """Write current state to JSON file.

        Args:
            state: State dictionary to write.
        """
        with open(self.output_file, "w") as f:
            json.dump(state, f, indent=2)

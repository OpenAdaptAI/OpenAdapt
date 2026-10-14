"""Pydantic models for openadapt-viewer data structures.

These models define the data structures used throughout the viewer package.
They provide type safety, validation, and clear documentation of data formats.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ExecutionStep(BaseModel):
    """A single step in a task execution.

    Represents one action taken by the agent during task execution,
    including the screenshot, action details, and any model reasoning.
    """

    step_number: int = Field(description="Step index (0-based)")
    timestamp: datetime | None = Field(default=None, description="When this step occurred")
    screenshot_path: str | None = Field(
        default=None, description="Relative path to screenshot image"
    )
    action_type: str = Field(description="Type of action (e.g., 'click', 'type', 'scroll')")
    action_details: dict[str, Any] = Field(
        default_factory=dict, description="Action-specific parameters (coordinates, text, etc.)"
    )
    reasoning: str | None = Field(
        default=None, description="Model's reasoning/chain-of-thought for this action"
    )
    raw_output: str | None = Field(
        default=None, description="Raw model output before parsing"
    )


class TaskExecution(BaseModel):
    """Execution trace for a single benchmark task.

    Contains the full history of steps taken to complete (or fail) a task.
    """

    task_id: str = Field(description="Unique identifier for the task")
    start_time: datetime | None = Field(default=None, description="When execution started")
    end_time: datetime | None = Field(default=None, description="When execution ended")
    steps: list[ExecutionStep] = Field(default_factory=list, description="Ordered list of steps")
    success: bool = Field(default=False, description="Whether the task was completed successfully")
    error: str | None = Field(default=None, description="Error message if task failed")


class BenchmarkTask(BaseModel):
    """A single benchmark task definition.

    Represents the task specification, not the execution results.
    """

    task_id: str = Field(description="Unique identifier for the task")
    instruction: str = Field(description="Natural language instruction for the task")
    domain: str | None = Field(
        default=None, description="Task domain (e.g., 'office', 'browser', 'system')"
    )
    difficulty: str | None = Field(
        default=None, description="Difficulty level (e.g., 'easy', 'medium', 'hard')"
    )
    time_limit: int | None = Field(
        default=None, description="Maximum time allowed in seconds"
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Additional task-specific metadata"
    )


class BenchmarkRun(BaseModel):
    """A complete benchmark run with multiple tasks.

    Represents the results of running an agent on a benchmark suite.
    """

    run_id: str = Field(description="Unique identifier for this run")
    benchmark_name: str = Field(description="Name of the benchmark (e.g., 'WAA', 'WebArena')")
    model_id: str = Field(description="Identifier of the model being evaluated")
    start_time: datetime | None = Field(default=None, description="When the run started")
    end_time: datetime | None = Field(default=None, description="When the run ended")
    tasks: list[BenchmarkTask] = Field(default_factory=list, description="Task definitions")
    executions: list[TaskExecution] = Field(
        default_factory=list, description="Execution traces for each task"
    )
    config: dict[str, Any] = Field(
        default_factory=dict, description="Configuration used for this run"
    )

    @property
    def success_rate(self) -> float:
        """Calculate overall success rate."""
        if not self.executions:
            return 0.0
        successes = sum(1 for e in self.executions if e.success)
        return successes / len(self.executions)

    @property
    def total_tasks(self) -> int:
        """Total number of tasks executed."""
        return len(self.executions)

    @property
    def passed_tasks(self) -> int:
        """Number of tasks that passed."""
        return sum(1 for e in self.executions if e.success)

    @property
    def failed_tasks(self) -> int:
        """Number of tasks that failed."""
        return sum(1 for e in self.executions if not e.success)

    def get_domain_stats(self) -> dict[str, dict[str, int]]:
        """Get pass/fail counts by domain."""
        stats: dict[str, dict[str, int]] = {}
        task_map = {t.task_id: t for t in self.tasks}

        for execution in self.executions:
            task = task_map.get(execution.task_id)
            domain = task.domain if task and task.domain else "unknown"

            if domain not in stats:
                stats[domain] = {"passed": 0, "failed": 0, "total": 0}

            stats[domain]["total"] += 1
            if execution.success:
                stats[domain]["passed"] += 1
            else:
                stats[domain]["failed"] += 1

        return stats

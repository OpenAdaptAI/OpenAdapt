"""Data collection for benchmark viewer integration.

This module handles saving execution traces during benchmark runs for later
replay in the benchmark viewer. It creates a structured directory layout with
screenshots, metadata, and execution traces.

Directory structure:
    benchmark_results/
    ├── waa_eval_YYYYMMDD_HHMMSS/
    │   ├── metadata.json
    │   ├── tasks/
    │   │   ├── task_001/
    │   │   │   ├── task.json
    │   │   │   ├── screenshots/
    │   │   │   │   ├── step_000.png
    │   │   │   │   ├── step_001.png
    │   │   │   │   └── ...
    │   │   │   └── execution.json
    │   │   └── task_002/
    │   │       └── ...
    │   └── summary.json

Example:
    from openadapt_evals.benchmarks.data_collection import ExecutionTraceCollector

    collector = ExecutionTraceCollector(
        benchmark_name="waa",
        run_name="waa_eval_20241214",
        model_id="qwen3vl-2b-epoch5"
    )

    # During evaluation
    collector.start_task(task)
    for step_idx, (obs, action) in enumerate(trajectory):
        collector.record_step(step_idx, obs, action, reasoning="...")
    collector.finish_task(result)
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from openadapt_evals.adapters import (
    BenchmarkAction,
    BenchmarkObservation,
    BenchmarkResult,
    BenchmarkTask,
)

logger = logging.getLogger(__name__)


@dataclass
class LogEntry:
    """Single log entry during execution.

    Attributes:
        timestamp: Timestamp of the log entry (seconds since task start).
        level: Log level (INFO, WARNING, ERROR, SUCCESS).
        message: Log message.
    """

    timestamp: float
    level: str
    message: str


class TaskLogHandler(logging.Handler):
    """Custom log handler that captures logs for the current task."""

    def __init__(self):
        super().__init__()
        self.logs: list[LogEntry] = []
        self.task_start_time: float | None = None

    def emit(self, record: logging.LogRecord) -> None:
        """Capture a log record."""
        if self.task_start_time is None:
            return

        # Calculate relative timestamp
        timestamp = record.created - self.task_start_time

        # Map logging levels to our format
        level_map = {
            logging.DEBUG: "INFO",
            logging.INFO: "INFO",
            logging.WARNING: "WARNING",
            logging.ERROR: "ERROR",
            logging.CRITICAL: "ERROR",
        }
        level = level_map.get(record.levelno, "INFO")

        # Check for SUCCESS marker in message
        if "[SUCCESS]" in record.getMessage():
            level = "SUCCESS"

        log_entry = LogEntry(
            timestamp=timestamp,
            level=level,
            message=record.getMessage(),
        )
        self.logs.append(log_entry)

    def start_task(self) -> None:
        """Start capturing logs for a new task."""
        self.logs = []
        self.task_start_time = datetime.now().timestamp()

    def finish_task(self) -> list[dict[str, Any]]:
        """Finish task and return captured logs."""
        log_dicts = [asdict(log) for log in self.logs]
        self.logs = []
        self.task_start_time = None
        return log_dicts


@dataclass
class ExecutionStep:
    """Single step in execution trace.

    Attributes:
        step_idx: Step index in the trajectory.
        screenshot_path: Relative path to screenshot image.
        action: Action taken at this step.
        reasoning: Optional reasoning/thought from the agent.
        timestamp: Timestamp when step was recorded.
    """

    step_idx: int
    screenshot_path: str | None
    action: dict[str, Any]  # Serialized BenchmarkAction
    reasoning: str | None = None
    timestamp: float | None = None
    agent_logs: dict[str, Any] | None = None


class ExecutionTraceCollector:
    """Collects execution traces during benchmark runs.

    This class handles:
    - Creating the directory structure for a benchmark run
    - Saving screenshots at each step
    - Recording actions and reasoning
    - Saving task results and metadata
    - Capturing detailed execution logs

    Args:
        benchmark_name: Name of the benchmark (e.g., "waa", "webarena").
        run_name: Unique name for this evaluation run (e.g., "waa_eval_20241214").
        model_id: Identifier for the model being evaluated.
        output_dir: Base directory for benchmark results (default: "./benchmark_results").
        capture_logs: Whether to capture execution logs (default: True).
    """

    def __init__(
        self,
        benchmark_name: str,
        run_name: str | None = None,
        model_id: str = "unknown",
        output_dir: str | Path = "benchmark_results",
        capture_logs: bool = True,
    ):
        self.benchmark_name = benchmark_name
        self.model_id = model_id
        self.capture_logs = capture_logs

        # Auto-generate run_name if not provided
        if run_name is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            run_name = f"{benchmark_name}_eval_{timestamp}"
        self.run_name = run_name

        # Set up directory structure
        self.output_dir = Path(output_dir)
        self.run_dir = self.output_dir / run_name
        self.tasks_dir = self.run_dir / "tasks"

        # Current task tracking
        self._current_task: BenchmarkTask | None = None
        self._current_task_dir: Path | None = None
        self._current_screenshots_dir: Path | None = None
        self._current_steps: list[ExecutionStep] = []

        # Log handler for capturing execution logs
        self._log_handler: TaskLogHandler | None = None
        if self.capture_logs:
            self._log_handler = TaskLogHandler()
            # Attach to root logger to capture all logs
            logging.getLogger().addHandler(self._log_handler)

        # Initialize run
        self._initialize_run()

    def _initialize_run(self) -> None:
        """Initialize the benchmark run directory and metadata."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.tasks_dir.mkdir(exist_ok=True)

        # Save run metadata
        metadata = {
            "benchmark_name": self.benchmark_name,
            "run_name": self.run_name,
            "model_id": self.model_id,
            "created_at": datetime.now().isoformat(),
        }

        metadata_path = self.run_dir / "metadata.json"
        with open(metadata_path, "w") as f:
            json.dump(metadata, f, indent=2)

        logger.info(f"Initialized benchmark run at: {self.run_dir}")

    def start_task(self, task: BenchmarkTask) -> None:
        """Start collecting data for a new task.

        Args:
            task: The benchmark task being executed.
        """
        if self._current_task is not None:
            logger.warning(
                f"Starting new task {task.task_id} without finishing {self._current_task.task_id}"
            )

        self._current_task = task
        self._current_steps = []

        # Start log capture
        if self._log_handler is not None:
            self._log_handler.start_task()

        # Create task directory
        task_dir_name = self._sanitize_task_id(task.task_id)
        self._current_task_dir = self.tasks_dir / task_dir_name
        self._current_task_dir.mkdir(parents=True, exist_ok=True)

        # Create screenshots directory
        self._current_screenshots_dir = self._current_task_dir / "screenshots"
        self._current_screenshots_dir.mkdir(exist_ok=True)

        # Save task definition
        task_data = {
            "task_id": task.task_id,
            "instruction": task.instruction,
            "domain": task.domain,
            "initial_state_ref": task.initial_state_ref,
            "time_limit_steps": task.time_limit_steps,
            "raw_config": task.raw_config,
            "evaluation_spec": task.evaluation_spec,
        }

        task_path = self._current_task_dir / "task.json"
        with open(task_path, "w") as f:
            json.dump(task_data, f, indent=2)

        logger.info(f"Started collecting data for task: {task.task_id}")

    def record_step(
        self,
        step_idx: int,
        observation: BenchmarkObservation,
        action: BenchmarkAction,
        reasoning: str | None = None,
        agent_logs: dict[str, Any] | None = None,
    ) -> None:
        """Record a single step in the execution trace.

        Args:
            step_idx: Index of this step in the trajectory.
            observation: Observation at this step.
            action: Action taken at this step.
            reasoning: Optional reasoning/thought from the agent.
            agent_logs: Optional dict of agent metadata (LLM response,
                parse strategy, timing, token usage, etc.).
        """
        if self._current_task is None:
            raise RuntimeError("No task started. Call start_task() first.")

        # Save screenshot if available
        screenshot_path = None
        if observation.screenshot is not None:
            screenshot_path = self._save_screenshot(step_idx, observation.screenshot)
        elif observation.screenshot_path is not None:
            # Copy existing screenshot
            screenshot_path = self._copy_screenshot(step_idx, observation.screenshot_path)

        # Create execution step record
        step = ExecutionStep(
            step_idx=step_idx,
            screenshot_path=screenshot_path,
            action=self._serialize_action(action),
            reasoning=reasoning,
            timestamp=datetime.now().timestamp(),
            agent_logs=agent_logs,
        )

        self._current_steps.append(step)

    def finish_task(self, result: BenchmarkResult) -> None:
        """Finish collecting data for the current task and save execution trace.

        Args:
            result: The evaluation result for the task.
        """
        if self._current_task is None:
            raise RuntimeError("No task started. Call start_task() first.")

        # Get captured logs
        logs = []
        if self._log_handler is not None:
            logs = self._log_handler.finish_task()

        # Save execution trace
        execution_data = {
            "task_id": result.task_id,
            "model_id": self.model_id,
            "success": result.success,
            "score": result.score,
            "num_steps": result.num_steps,
            "total_time_seconds": result.total_time_seconds,
            "error": result.error,
            "reason": result.reason,
            "error_type": result.error_type,
            "steps": [asdict(step) for step in self._current_steps],
            "logs": logs,
        }

        execution_path = self._current_task_dir / "execution.json"
        with open(execution_path, "w") as f:
            json.dump(execution_data, f, indent=2)

        logger.info(
            f"Saved execution trace for task {result.task_id}: "
            f"{'SUCCESS' if result.success else 'FAIL'} ({result.num_steps} steps, {len(logs)} log entries)"
        )

        # Clear current task
        self._current_task = None
        self._current_task_dir = None
        self._current_screenshots_dir = None
        self._current_steps = []

    def save_summary(self, all_results: list[BenchmarkResult]) -> None:
        """Save summary of all task results.

        Args:
            all_results: List of all BenchmarkResult objects from the run.
        """
        # Keep one aggregation contract across CLI, traces, and integrations.
        # Import locally to avoid a module cycle during runner initialization.
        from openadapt_evals.adapters import normalize_benchmark_result
        from openadapt_evals.benchmarks.runner import compute_metrics

        all_results = [
            normalize_benchmark_result(result, context=f"trace result {index}")
            for index, result in enumerate(all_results)
        ]

        metrics = compute_metrics(all_results)
        summary = {
            "benchmark_name": self.benchmark_name,
            "run_name": self.run_name,
            "model_id": self.model_id,
            **metrics,
            # Existing trace consumers use these names. Evaluator and
            # infrastructure outages do not enter the outcome denominator.
            "num_success": metrics["success_count"],
            "num_success_excluding_infra": metrics["success_count"],
            "tasks": [
                {
                    "task_id": r.task_id,
                    "success": r.success,
                    "score": r.score,
                    "num_steps": r.num_steps,
                    "error": r.error,
                    "reason": r.reason,
                    "error_type": r.error_type,
                }
                for r in all_results
            ],
        }

        summary_path = self.run_dir / "summary.json"
        with open(summary_path, "w") as f:
            json.dump(summary, f, indent=2)

        logger.info(
            f"Saved summary: {summary['num_success']}/"
            f"{summary['num_outcome_tasks']} outcomes succeeded "
            f"({summary['success_rate']:.1%}); {summary['error_count']} errors "
            f"across {summary['num_tasks']} attempts"
        )

    def _save_screenshot(self, step_idx: int, screenshot_bytes: bytes) -> str:
        """Save screenshot bytes to file.

        Args:
            step_idx: Step index for naming the file.
            screenshot_bytes: PNG image bytes.

        Returns:
            Relative path to the saved screenshot.
        """
        if self._current_screenshots_dir is None:
            raise RuntimeError("No task started")

        filename = f"step_{step_idx:03d}.png"
        screenshot_path = self._current_screenshots_dir / filename

        with open(screenshot_path, "wb") as f:
            f.write(screenshot_bytes)

        # Return relative path from task directory
        return f"screenshots/{filename}"

    def _copy_screenshot(self, step_idx: int, source_path: str) -> str:
        """Copy screenshot from existing path.

        Args:
            step_idx: Step index for naming the file.
            source_path: Path to existing screenshot.

        Returns:
            Relative path to the copied screenshot.
        """
        if self._current_screenshots_dir is None:
            raise RuntimeError("No task started")

        filename = f"step_{step_idx:03d}.png"
        dest_path = self._current_screenshots_dir / filename

        # Copy file
        import shutil
        shutil.copy2(source_path, dest_path)

        return f"screenshots/{filename}"

    def _serialize_action(self, action: BenchmarkAction) -> dict[str, Any]:
        """Serialize BenchmarkAction to dict.

        Args:
            action: Action to serialize.

        Returns:
            Dictionary representation of the action.
        """
        return {
            "type": action.type,
            "x": action.x,
            "y": action.y,
            "target_node_id": action.target_node_id,
            "target_bbox": action.target_bbox,
            "target_role": action.target_role,
            "target_name": action.target_name,
            "text": action.text,
            "key": action.key,
            "modifiers": action.modifiers,
            "scroll_direction": action.scroll_direction,
            "scroll_amount": action.scroll_amount,
            "end_x": action.end_x,
            "end_y": action.end_y,
            "answer": action.answer,
            "raw_action": action.raw_action,
        }

    def _sanitize_task_id(self, task_id: str) -> str:
        """Sanitize task ID for use as directory name.

        Args:
            task_id: Original task ID.

        Returns:
            Sanitized task ID safe for filesystem.
        """
        # Replace unsafe characters with underscores
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in task_id)
        return safe_id


def save_execution_trace(
    task: BenchmarkTask,
    result: BenchmarkResult,
    trajectory: list[tuple[BenchmarkObservation, BenchmarkAction]],
    benchmark_name: str,
    model_id: str = "unknown",
    output_dir: str | Path = "benchmark_results",
    run_name: str | None = None,
    reasoning_map: dict[int, str] | None = None,
) -> Path:
    """Convenience function to save a complete execution trace.

    This is a simpler alternative to using ExecutionTraceCollector directly
    when you have the complete trajectory available.

    Args:
        task: The benchmark task.
        result: The evaluation result.
        trajectory: List of (observation, action) pairs.
        benchmark_name: Name of the benchmark.
        model_id: Identifier for the model.
        output_dir: Base directory for results.
        run_name: Optional run name (auto-generated if None).
        reasoning_map: Optional map of step_idx -> reasoning text.

    Returns:
        Path to the task directory.

    Example:
        save_execution_trace(
            task=task,
            result=result,
            trajectory=trajectory,
            benchmark_name="waa",
            model_id="qwen3vl-2b-epoch5",
            reasoning_map={0: "I should click the button", 1: "Now type the text"}
        )
    """
    collector = ExecutionTraceCollector(
        benchmark_name=benchmark_name,
        run_name=run_name,
        model_id=model_id,
        output_dir=output_dir,
    )

    collector.start_task(task)

    for step_idx, (obs, action) in enumerate(trajectory):
        reasoning = reasoning_map.get(step_idx) if reasoning_map else None
        collector.record_step(step_idx, obs, action, reasoning)

    collector.finish_task(result)

    return collector._current_task_dir or collector.tasks_dir

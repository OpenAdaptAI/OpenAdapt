"""Base classes for benchmark adapters.

This module provides the core abstractions for integrating GUI agent benchmarks
into the evaluation framework. It supports both interactive environments (WAA, OSWorld)
and static trajectory datasets (Mind2Web).

Example:
    from openadapt_evals.adapters import BenchmarkAdapter, WAAAdapter

    adapter = WAAAdapter(waa_repo_path="/path/to/WAA")
    tasks = adapter.list_tasks()
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from numbers import Real
from typing import TYPE_CHECKING, Any, Iterator

# Canonical Benchmark* task/observation/action types now live in the shared
# schema package (openadapt-types) so openadapt-ml and openadapt-evals do not
# import each other. Re-exported here for backward compatibility with existing
# imports, e.g. ``from openadapt_evals.adapters.base import BenchmarkAction``.
from openadapt_types import (
    BenchmarkAction,
    BenchmarkObservation,
    BenchmarkTask,
)

if TYPE_CHECKING:
    pass


class EvaluationUnavailableError(RuntimeError):
    """A task could not be scored, as distinct from having scored badly.

    Adapters signal this condition on a :class:`BenchmarkResult` via
    ``error_type in {"infrastructure", "evaluation"}``. Any API that flattens a
    result down to a bare float must raise this instead of returning ``0.0``:
    an unreachable VM reported as a legitimate 0% is the single worst defect an
    evaluation harness can have, because the number looks measured.
    """

    def __init__(self, message: str, error_type: str = "infrastructure") -> None:
        super().__init__(message)
        self.error_type = error_type


@dataclass
class BenchmarkResult:
    """Result of a single task evaluation.

    Attributes:
        task_id: ID of the evaluated task.
        success: Whether the task was completed successfully.
        score: Score between 0.0 and 1.0.
        steps: List of (observation, action) pairs from the trajectory.
        num_steps: Number of steps taken.
        error: Error message if task failed due to error.
        reason: Explanation of success/failure.
        total_time_seconds: Total time taken for the task.
    """

    task_id: str
    success: bool
    score: float  # 0.0 to 1.0

    # Trajectory
    steps: list[tuple[BenchmarkObservation, BenchmarkAction]] = field(
        default_factory=list
    )
    num_steps: int = 0

    # Diagnostics
    error: str | None = None
    reason: str | None = None  # Why success/fail
    error_type: str | None = None  # "infrastructure", "agent", "evaluation", or None

    # Timing
    total_time_seconds: float = 0.0


BENCHMARK_ERROR_TYPES = frozenset({None, "agent", "infrastructure", "evaluation"})
UNSCORED_BENCHMARK_ERROR_TYPES = frozenset({"infrastructure", "evaluation"})


def normalize_benchmark_result(
    result: object,
    *,
    expected_task_id: str | None = None,
    context: str = "benchmark result",
) -> BenchmarkResult:
    """Return a valid result or an explicit unscored evaluation failure.

    Adapters are plugin boundaries. A malformed adapter result must not reach a
    done gate, a report, or aggregate metrics as a measured outcome. Partial
    scores are valid, so coherence only fixes the unambiguous endpoint cases:
    success cannot have a zero score and failure cannot have a full score.
    """
    issues: list[str] = []

    if not isinstance(result, BenchmarkResult):
        issues.append(f"expected BenchmarkResult, got {type(result).__name__}")
        candidate: BenchmarkResult | None = None
    else:
        candidate = result

    task_id = expected_task_id
    if candidate is not None:
        if not isinstance(candidate.task_id, str) or not candidate.task_id:
            issues.append("task_id must be a non-empty string")
        elif expected_task_id is not None and candidate.task_id != expected_task_id:
            issues.append(
                f"task_id {candidate.task_id!r} does not match {expected_task_id!r}"
            )
        elif task_id is None:
            task_id = candidate.task_id

        if type(candidate.success) is not bool:
            issues.append("success must be a bool")

        score_valid = (
            not isinstance(candidate.score, bool)
            and isinstance(candidate.score, Real)
            and math.isfinite(float(candidate.score))
            and 0.0 <= float(candidate.score) <= 1.0
        )
        if not score_valid:
            issues.append("score must be a finite number in [0, 1]")

        error_type_valid = (
            candidate.error_type is None
            or (
                isinstance(candidate.error_type, str)
                and candidate.error_type in BENCHMARK_ERROR_TYPES
            )
        )
        if not error_type_valid:
            issues.append(f"unknown error_type {candidate.error_type!r}")

        if (
            isinstance(candidate.num_steps, bool)
            or not isinstance(candidate.num_steps, int)
            or candidate.num_steps < 0
        ):
            issues.append("num_steps must be a non-negative integer")

        if (
            isinstance(candidate.total_time_seconds, bool)
            or not isinstance(candidate.total_time_seconds, Real)
            or not math.isfinite(float(candidate.total_time_seconds))
            or float(candidate.total_time_seconds) < 0.0
        ):
            issues.append("total_time_seconds must be a finite non-negative number")

        if candidate.error is not None and not isinstance(candidate.error, str):
            issues.append("error must be a string or null")
        if candidate.reason is not None and not isinstance(candidate.reason, str):
            issues.append("reason must be a string or null")

        if (
            type(candidate.success) is bool
            and score_valid
            and error_type_valid
        ):
            score = float(candidate.score)
            if candidate.error_type is None and candidate.success and score == 0.0:
                issues.append("a successful result cannot have score=0.0")
            elif (
                candidate.error_type is None
                and not candidate.success
                and score == 1.0
            ):
                issues.append("a failed result cannot have score=1.0")

    if not issues and candidate is not None:
        if candidate.error_type is not None and (
            candidate.success or float(candidate.score) != 0.0
        ):
            return replace(candidate, success=False, score=0.0)
        return candidate

    safe_task_id = task_id if isinstance(task_id, str) and task_id else "unknown"
    message = f"Malformed {context}: {'; '.join(issues)}"
    return BenchmarkResult(
        task_id=safe_task_id,
        success=False,
        score=0.0,
        error=message,
        reason=message,
        error_type="evaluation",
    )


def normalize_benchmark_result_artifact(
    record: object,
    *,
    expected_task_id: str | None = None,
    context: str = "benchmark result artifact",
) -> BenchmarkResult:
    """Parse one persisted result row through the runtime result contract.

    JSON decoders preserve strings such as ``"false"``. Constructing reports
    directly from their Python truthiness turns those strings into successful
    outcomes. This parser keeps raw artifact types intact until
    :func:`normalize_benchmark_result` validates them.
    """
    if not isinstance(record, Mapping):
        return normalize_benchmark_result(
            record,
            expected_task_id=expected_task_id,
            context=context,
        )

    raw_steps = record.get("num_steps", record.get("steps", 0))
    num_steps = len(raw_steps) if isinstance(raw_steps, list) else raw_steps
    candidate = BenchmarkResult(
        task_id=record.get("task_id", expected_task_id),  # type: ignore[arg-type]
        success=record.get("success"),  # type: ignore[arg-type]
        score=record.get("score"),  # type: ignore[arg-type]
        num_steps=num_steps,  # type: ignore[arg-type]
        error=record.get("error"),  # type: ignore[arg-type]
        reason=record.get("reason"),  # type: ignore[arg-type]
        error_type=record.get("error_type"),  # type: ignore[arg-type]
        total_time_seconds=record.get(
            "total_time_seconds",
            record.get("elapsed_seconds", 0.0),
        ),  # type: ignore[arg-type]
    )
    return normalize_benchmark_result(
        candidate,
        expected_task_id=expected_task_id,
        context=context,
    )


def benchmark_result_is_scored(result: BenchmarkResult) -> bool:
    """Return whether a result belongs in measured-outcome denominators."""
    return result.error_type not in UNSCORED_BENCHMARK_ERROR_TYPES


@dataclass
class UIElement:
    """Normalized UI element for cross-platform use.

    Provides a common representation for UI elements across platforms
    (Windows UIA, macOS AXTree, web DOM).

    Attributes:
        node_id: Unique identifier for the element.
        role: Element role (button, textfield, link, etc.).
        name: Accessible name/label.
        bbox: Bounding box (normalized [0,1] or pixels).
        text: Text content.
        value: Current value (for inputs).
        children: Child elements.
        attributes: Additional platform-specific attributes.
    """

    node_id: str
    role: str  # "button", "textfield", "link", etc.
    name: str | None = None  # Accessible name/label
    bbox: tuple[float, float, float, float] | None = None  # (x1, y1, x2, y2)
    text: str | None = None  # Text content
    value: str | None = None  # Current value (for inputs)
    children: list[UIElement] | None = None
    attributes: dict[str, Any] | None = None  # Platform-specific


class BenchmarkAdapter(ABC):
    """Abstract interface for benchmark integration.

    Subclasses implement this interface to integrate specific benchmarks
    (WAA, OSWorld, WebArena, etc.) with the evaluation framework.

    Two types of adapters:
    - Interactive: Run environment, step through tasks (WAA, OSWorld)
    - Static: Load trajectories for offline training/eval (Mind2Web)
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Benchmark name (e.g., 'waa', 'osworld', 'webarena')."""
        pass

    @property
    @abstractmethod
    def benchmark_type(self) -> str:
        """Benchmark type: 'interactive' or 'static'."""
        pass

    @property
    def supports_parallel(self) -> bool:
        """Whether the adapter supports parallel task execution."""
        return False

    @abstractmethod
    def list_tasks(self, domain: str | None = None) -> list[BenchmarkTask]:
        """List available tasks, optionally filtered by domain.

        Args:
            domain: Optional domain filter (e.g., "browser", "office").

        Returns:
            List of BenchmarkTask objects.
        """
        pass

    @abstractmethod
    def load_task(self, task_id: str) -> BenchmarkTask:
        """Load a specific task by ID.

        Args:
            task_id: Task identifier.

        Returns:
            BenchmarkTask object.

        Raises:
            KeyError: If task_id not found.
        """
        pass

    @abstractmethod
    def reset(self, task: BenchmarkTask) -> BenchmarkObservation:
        """Reset environment to task's initial state.

        Args:
            task: Task to initialize.

        Returns:
            Initial observation.
        """
        pass

    @abstractmethod
    def step(
        self, action: BenchmarkAction
    ) -> tuple[BenchmarkObservation, bool, dict[str, Any]]:
        """Execute action and return new observation.

        Args:
            action: Action to execute.

        Returns:
            Tuple of (observation, done, info).
        """
        pass

    @abstractmethod
    def evaluate(self, task: BenchmarkTask) -> BenchmarkResult:
        """Run benchmark's native evaluation on current state.

        Args:
            task: Task to evaluate.

        Returns:
            BenchmarkResult with success/score.
        """
        pass

    def close(self) -> None:
        """Clean up resources (VMs, browser, etc.)."""
        pass

    def __enter__(self) -> BenchmarkAdapter:
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        self.close()


class StaticDatasetAdapter(BenchmarkAdapter):
    """Base for static trajectory datasets (Mind2Web, demos).

    Static adapters load pre-recorded trajectories for offline training
    or evaluation, rather than running an interactive environment.
    """

    @property
    def benchmark_type(self) -> str:
        """Static datasets are not interactive."""
        return "static"

    @abstractmethod
    def load_trajectories(
        self, split: str = "test"
    ) -> Iterator[tuple[BenchmarkTask, list[tuple[BenchmarkObservation, BenchmarkAction]]]]:
        """Iterate over expert trajectories.

        Args:
            split: Dataset split ("train", "val", "test").

        Yields:
            Tuples of (task, trajectory) where trajectory is a list of
            (observation, action) pairs.
        """
        pass

    def reset(self, task: BenchmarkTask) -> BenchmarkObservation:
        """Not supported for static datasets."""
        raise NotImplementedError(
            "Static datasets don't support interactive reset. "
            "Use load_trajectories() instead."
        )

    def step(
        self, action: BenchmarkAction
    ) -> tuple[BenchmarkObservation, bool, dict[str, Any]]:
        """Not supported for static datasets."""
        raise NotImplementedError(
            "Static datasets don't support interactive stepping. "
            "Use load_trajectories() instead."
        )

    def evaluate(self, task: BenchmarkTask) -> BenchmarkResult:
        """Not supported for static datasets."""
        raise NotImplementedError(
            "Static datasets don't support execution-based evaluation. "
            "Use offline metrics instead."
        )

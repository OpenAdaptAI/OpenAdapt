"""Trace analyzer for OpenAdapt evaluation runs.

Loads JSONL trace files and/or screenshot directories produced by
:class:`~openadapt_evals.training.trajectory_logger.PlannerTrajectoryLogger`,
``scripts/run_full_eval.py``, or the structured benchmark viewer format, and
exposes aggregate statistics, failure classification, step timelines, and
run-to-run comparison.

Usage::

    from openadapt_evals.analysis import TraceAnalyzer

    analyzer = TraceAnalyzer("benchmark_results/full_eval_20260320.jsonl")
    print(analyzer.summary())

    failures = analyzer.failure_modes()
    for fm in failures:
        print(f"{fm['mode']}: {fm['count']} episodes")

    other = TraceAnalyzer("benchmark_results/full_eval_20260321.jsonl")
    diff = analyzer.compare(other)
    print(f"Improved: {len(diff['improved'])}, Regressed: {len(diff['regressed'])}")
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from openadapt_evals.adapters.base import (
    UNSCORED_BENCHMARK_ERROR_TYPES,
    normalize_benchmark_result_artifact,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cost estimation constants (USD per million tokens, March 2026)
# ---------------------------------------------------------------------------
_COST_TABLE: dict[str, dict[str, float]] = {
    "claude-sonnet-4": {"input": 3.00, "output": 15.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "gpt-4.1": {"input": 2.00, "output": 8.00},
    "gpt-5.4": {"input": 5.00, "output": 20.00},
}
# Average tokens per step (including screenshot as base64 in prompt)
_AVG_INPUT_TOKENS_PER_STEP = 5000
_AVG_OUTPUT_TOKENS_PER_STEP = 300


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class Episode:
    """Parsed episode data from any supported trace format."""

    __slots__ = (
        "episode_id",
        "task_id",
        "task_instruction",
        "score",
        "success",
        "num_steps",
        "elapsed_seconds",
        "error",
        "error_type",
        "started_at",
        "finished_at",
        "milestones_passed",
        "milestones_total",
        "steps",
        "model",
        "raw",
    )

    def __init__(self, **kwargs: Any) -> None:
        self.episode_id: str = kwargs.get("episode_id", "")
        self.task_id: str = kwargs.get("task_id", "")
        self.task_instruction: str = kwargs.get("task_instruction", "")
        self.score: float = kwargs.get("score", 0.0)
        self.success: bool = kwargs.get("success", False)
        self.num_steps: int = kwargs.get("num_steps", 0)
        self.elapsed_seconds: float = kwargs.get("elapsed_seconds", 0.0)
        self.error: str | None = kwargs.get("error")
        self.error_type: str | None = kwargs.get("error_type")
        self.started_at: str | None = kwargs.get("started_at")
        self.finished_at: str | None = kwargs.get("finished_at")
        self.milestones_passed: int | None = kwargs.get("milestones_passed")
        self.milestones_total: int | None = kwargs.get("milestones_total")
        self.steps: list[StepRecord] = kwargs.get("steps", [])
        self.model: str | None = kwargs.get("model")
        self.raw: dict[str, Any] = kwargs.get("raw", {})


class StepRecord:
    """Parsed step data from a trajectory JSONL record."""

    __slots__ = (
        "step_index",
        "screenshot_path",
        "action_type",
        "target",
        "instruction",
        "reasoning",
        "decision",
        "action_history",
        "raw",
    )

    def __init__(self, **kwargs: Any) -> None:
        self.step_index: int = kwargs.get("step_index", 0)
        self.screenshot_path: str | None = kwargs.get("screenshot_path")
        self.action_type: str | None = kwargs.get("action_type")
        self.target: str | None = kwargs.get("target")
        self.instruction: str | None = kwargs.get("instruction")
        self.reasoning: str | None = kwargs.get("reasoning")
        self.decision: str | None = kwargs.get("decision")
        self.action_history: list[str] = kwargs.get("action_history", [])
        self.raw: dict[str, Any] = kwargs.get("raw", {})


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

_FORMAT_FULL_EVAL_JSONL = "full_eval_jsonl"
_FORMAT_TRAJECTORY_DIR = "trajectory_dir"
_FORMAT_BENCHMARK_DIR = "benchmark_dir"
_FORMAT_MIXED_DIR = "mixed_dir"


def _detect_format(path: Path) -> str:
    """Auto-detect the trace format of *path*.

    Returns one of the ``_FORMAT_*`` constants.
    """
    if path.is_file() and path.suffix == ".jsonl":
        return _FORMAT_FULL_EVAL_JSONL

    if path.is_dir():
        if (path / "trajectories.jsonl").exists():
            return _FORMAT_TRAJECTORY_DIR
        if (path / "metadata.json").exists() and (path / "tasks").is_dir():
            return _FORMAT_BENCHMARK_DIR
        # Fallback: directory with PNGs or JSONL files
        return _FORMAT_MIXED_DIR

    # Treat as JSONL file even without extension
    return _FORMAT_FULL_EVAL_JSONL


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def _load_full_eval_jsonl(path: Path) -> tuple[list[Episode], dict[str, Any]]:
    """Load episodes from ``run_full_eval.py`` JSONL output.

    Returns (episodes, run_metadata).
    """
    episodes: list[Episode] = []
    run_meta: dict[str, Any] = {}

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        if record.get("_meta"):
            run_meta = record
            continue

        task_id = record.get("task_id")
        if not task_id:
            continue

        result = normalize_benchmark_result_artifact(
            record,
            expected_task_id=task_id,
            context=f"full-eval artifact for {task_id}",
        )
        episodes.append(
            Episode(
                episode_id=task_id,
                task_id=task_id,
                score=result.score,
                success=result.success,
                num_steps=result.num_steps,
                elapsed_seconds=result.total_time_seconds,
                error=result.error,
                error_type=result.error_type,
                started_at=record.get("started_at"),
                finished_at=record.get("finished_at"),
                milestones_passed=record.get("milestones_passed"),
                milestones_total=record.get("milestones_total"),
                model=run_meta.get("planner_model"),
                raw=record,
            )
        )

    return episodes, run_meta


def _load_trajectory_dir(path: Path) -> tuple[list[Episode], dict[str, Any]]:
    """Load episodes from a PlannerTrajectoryLogger output directory.

    Groups JSONL records by episode_id.
    """
    jsonl_path = path / "trajectories.jsonl"
    if not jsonl_path.exists():
        return [], {}

    # Group records by episode
    episode_records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        eid = record.get("episode_id", "unknown")
        episode_records[eid].append(record)

    episodes: list[Episode] = []
    for eid, records in episode_records.items():
        records.sort(key=lambda r: r.get("step_index", 0))
        steps: list[StepRecord] = []
        task_instruction = ""

        for rec in records:
            planner_out = rec.get("planner_output", {})
            if isinstance(planner_out, str):
                try:
                    planner_out = json.loads(planner_out)
                except json.JSONDecodeError:
                    planner_out = {}

            # Resolve screenshot path relative to the trace dir
            screenshot_path = rec.get("screenshot_path")
            if screenshot_path:
                abs_screenshot = path / screenshot_path
                if abs_screenshot.exists():
                    screenshot_path = str(abs_screenshot)

            steps.append(
                StepRecord(
                    step_index=rec.get("step_index", 0),
                    screenshot_path=screenshot_path,
                    action_type=planner_out.get("action_type"),
                    target=planner_out.get("target_description"),
                    instruction=planner_out.get("instruction"),
                    reasoning=planner_out.get("reasoning"),
                    decision=planner_out.get("decision"),
                    action_history=rec.get("action_history", []),
                    raw=rec,
                )
            )
            if not task_instruction:
                task_instruction = rec.get("task_instruction", "")

        reward = records[0].get("episode_reward")
        reward_is_number = (
            not isinstance(reward, bool)
            and isinstance(reward, (int, float))
            and 0.0 <= float(reward) <= 1.0
        )
        result_record = {
            "task_id": eid,
            "score": reward,
            "success": reward_is_number and float(reward) >= 1.0,
            "num_steps": len(steps),
            "error_type": None if reward is not None else "evaluation",
            "reason": None if reward is not None else "episode reward is missing",
        }
        result = normalize_benchmark_result_artifact(
            result_record,
            expected_task_id=eid,
            context=f"trajectory artifact for {eid}",
        )

        episodes.append(
            Episode(
                episode_id=eid,
                task_id=eid,
                task_instruction=task_instruction,
                score=result.score,
                success=result.success,
                num_steps=result.num_steps,
                error=result.error,
                error_type=result.error_type,
                steps=steps,
            )
        )

    return episodes, {}


def _load_benchmark_dir(path: Path) -> tuple[list[Episode], dict[str, Any]]:
    """Load episodes from the structured benchmark viewer directory."""
    metadata: dict[str, Any] = {}
    metadata_path = path / "metadata.json"
    if metadata_path.exists():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

    episodes: list[Episode] = []
    tasks_dir = path / "tasks"
    if not tasks_dir.is_dir():
        return episodes, metadata

    for task_dir in sorted(tasks_dir.iterdir()):
        if not task_dir.is_dir():
            continue

        task_id = task_dir.name
        definition: dict[str, Any] = {}
        execution_artifact: object = {}

        task_json = task_dir / "task.json"
        if task_json.exists():
            definition = json.loads(task_json.read_text(encoding="utf-8"))

        exec_json = task_dir / "execution.json"
        if exec_json.exists():
            execution_artifact = json.loads(exec_json.read_text(encoding="utf-8"))
        execution = (
            execution_artifact if isinstance(execution_artifact, dict) else {}
        )

        # Collect screenshot paths
        screenshots_dir = task_dir / "screenshots"
        screenshot_files = sorted(screenshots_dir.glob("*.png")) if screenshots_dir.is_dir() else []

        # Build steps
        exec_steps = execution.get("steps", [])
        steps: list[StepRecord] = []
        for i, step_data in enumerate(exec_steps):
            action = step_data.get("action", {})
            screenshot_path = (
                str(screenshot_files[i]) if i < len(screenshot_files) else None
            )
            steps.append(
                StepRecord(
                    step_index=i,
                    screenshot_path=screenshot_path,
                    action_type=action.get("type"),
                    target=action.get("target_name") or action.get("target_description"),
                    reasoning=step_data.get("reasoning"),
                    raw=step_data,
                )
            )

        result = normalize_benchmark_result_artifact(
            (
                {**execution, "task_id": execution.get("task_id", task_id)}
                if isinstance(execution_artifact, dict)
                else execution_artifact
            ),
            expected_task_id=task_id,
            context=f"benchmark artifact for {task_id}",
        )
        episodes.append(
            Episode(
                episode_id=f"waa_{task_id}",
                task_id=task_id,
                task_instruction=definition.get("instruction", ""),
                score=result.score,
                success=result.success,
                num_steps=result.num_steps,
                elapsed_seconds=result.total_time_seconds,
                error=result.error,
                error_type=result.error_type,
                steps=steps,
                model=metadata.get("model_id"),
                raw={"definition": definition, "execution": execution},
            )
        )

    return episodes, metadata


def _load_mixed_dir(path: Path) -> tuple[list[Episode], dict[str, Any]]:
    """Load from a directory that may contain a mix of PNGs and JSONL files."""
    # Look for any JSONL file
    jsonl_files = list(path.glob("*.jsonl"))
    if jsonl_files:
        # Use the first JSONL file as a full-eval format
        all_episodes: list[Episode] = []
        run_meta: dict[str, Any] = {}
        for jf in jsonl_files:
            eps, meta = _load_full_eval_jsonl(jf)
            all_episodes.extend(eps)
            if meta:
                run_meta = meta
        return all_episodes, run_meta

    # Fallback: just PNGs (create a single episode from screenshots)
    pngs = sorted(path.glob("*.png"))
    if not pngs:
        pngs = sorted(path.glob("*/*.png"))

    if pngs:
        steps = []
        for i, png in enumerate(pngs):
            steps.append(
                StepRecord(
                    step_index=i,
                    screenshot_path=str(png),
                )
            )
        ep = Episode(
            episode_id=path.name,
            task_id=path.name,
            num_steps=len(steps),
            error="No benchmark result artifact was present",
            error_type="evaluation",
            steps=steps,
        )
        return [ep], {}

    return [], {}


# ---------------------------------------------------------------------------
# Failure mode classification
# ---------------------------------------------------------------------------

_FAILURE_MODE_LOOP = "loop_detected"
_FAILURE_MODE_TIMEOUT = "timeout"
_FAILURE_MODE_SERVER_ERROR = "server_error"
_FAILURE_MODE_AGENT_ERROR = "agent_error"
_FAILURE_MODE_PLANNER_WRONG = "planner_wrong_target"
_FAILURE_MODE_GROUNDER_MISS = "grounder_miss"
_FAILURE_MODE_TASK_INCOMPLETE = "task_incomplete"
_FAILURE_MODE_UNKNOWN = "unknown_failure"

_SERVER_ERROR_PATTERNS = re.compile(
    r"(connection|timeout|unreachable|500|502|503|504|refused|broken.pipe|reset.by.peer)",
    re.IGNORECASE,
)


def _classify_failure(episode: Episode, max_steps: int = 15) -> str | None:
    """Classify a single failed episode into a failure mode.

    Returns None for successful episodes.
    """
    if episode.success and episode.score >= 1.0:
        return None

    # Partial completion
    if episode.score > 0 and episode.score < 1.0:
        return _FAILURE_MODE_TASK_INCOMPLETE

    # Infrastructure / server errors
    if episode.error_type == "infrastructure":
        return _FAILURE_MODE_SERVER_ERROR
    if episode.error and _SERVER_ERROR_PATTERNS.search(str(episode.error)):
        return _FAILURE_MODE_SERVER_ERROR

    # Agent-level errors
    if episode.error_type == "agent":
        return _FAILURE_MODE_AGENT_ERROR

    # Timeout (hit max steps without completing)
    if episode.num_steps >= max_steps:
        return _FAILURE_MODE_TIMEOUT

    # Loop detection: 3+ consecutive identical actions
    if episode.steps and len(episode.steps) >= 3:
        for i in range(len(episode.steps) - 2):
            s1, s2, s3 = episode.steps[i], episode.steps[i + 1], episode.steps[i + 2]
            if (
                s1.action_type
                and s1.action_type == s2.action_type == s3.action_type
                and s1.target
                and s1.target == s2.target == s3.target
            ):
                return _FAILURE_MODE_LOOP

    # If we have steps with planner output, check for wrong target indicators
    if episode.steps:
        for step in episode.steps:
            if step.decision and "fail" in step.decision.lower():
                return _FAILURE_MODE_PLANNER_WRONG

    return _FAILURE_MODE_UNKNOWN


# ---------------------------------------------------------------------------
# Main analyzer class
# ---------------------------------------------------------------------------


class TraceAnalyzer:
    """Analyze traces from OpenAdapt evaluation runs.

    Supports multiple input formats:

    - A ``.jsonl`` file from ``run_full_eval.py``
    - A directory from :class:`PlannerTrajectoryLogger` (contains ``trajectories.jsonl``)
    - A structured benchmark directory (``metadata.json`` + ``tasks/``)
    - A mixed directory with JSONL files and/or PNGs

    Args:
        path: Path to trace file or directory.
        max_steps: Maximum steps per episode (used for timeout classification).

    Example::

        analyzer = TraceAnalyzer("benchmark_results/full_eval_20260320.jsonl")
        print(analyzer.summary())
    """

    def __init__(self, path: str | Path, max_steps: int = 15) -> None:
        self._path = Path(path)
        self._max_steps = max_steps
        self._format = _detect_format(self._path)
        self._episodes: list[Episode] = []
        self._run_meta: dict[str, Any] = {}

        self._load()

    def _load(self) -> None:
        """Load episodes from the detected format."""
        loaders = {
            _FORMAT_FULL_EVAL_JSONL: _load_full_eval_jsonl,
            _FORMAT_TRAJECTORY_DIR: _load_trajectory_dir,
            _FORMAT_BENCHMARK_DIR: _load_benchmark_dir,
            _FORMAT_MIXED_DIR: _load_mixed_dir,
        }
        loader = loaders.get(self._format, _load_mixed_dir)
        self._episodes, self._run_meta = loader(self._path)
        logger.info(
            "Loaded %d episodes from %s (format=%s)",
            len(self._episodes),
            self._path,
            self._format,
        )

    @property
    def episodes(self) -> list[Episode]:
        """Return the list of parsed episodes."""
        return self._episodes

    @property
    def run_metadata(self) -> dict[str, Any]:
        """Return run-level metadata (model, config, etc.)."""
        return self._run_meta

    @property
    def path(self) -> Path:
        """Return the source path."""
        return self._path

    @property
    def format(self) -> str:
        """Return the detected format string."""
        return self._format

    def summary(self) -> dict[str, Any]:
        """Compute aggregate statistics across all episodes.

        Returns:
            Dictionary with keys: total_episodes, total_steps, success_rate,
            avg_score, avg_steps_per_episode, avg_time_per_episode,
            total_time, cost_estimate_usd, episodes_by_status, model.
        """
        if not self._episodes:
            return {
                "total_episodes": 0,
                "outcome_episodes": 0,
                "unscored_episodes": 0,
                "total_steps": 0,
                "success_rate": 0.0,
                "avg_score": 0.0,
                "avg_steps_per_episode": 0.0,
                "avg_time_per_episode": 0.0,
                "total_time": 0.0,
                "cost_estimate_usd": 0.0,
                "episodes_by_status": {},
                "model": None,
            }

        total = len(self._episodes)
        outcomes = [
            ep
            for ep in self._episodes
            if ep.error_type not in UNSCORED_BENCHMARK_ERROR_TYPES
        ]
        outcome_total = len(outcomes)
        successes = sum(1 for ep in outcomes if ep.success)
        total_steps = sum(ep.num_steps for ep in self._episodes)
        total_time = sum(ep.elapsed_seconds for ep in self._episodes)
        scores = [ep.score for ep in outcomes]

        # Status breakdown
        status_counts: dict[str, int] = Counter()
        for ep in self._episodes:
            if ep.success:
                status_counts["passed"] += 1
            elif ep.error_type == "infrastructure":
                status_counts["infra_error"] += 1
            elif ep.error_type == "evaluation":
                status_counts["evaluation_error"] += 1
            elif ep.error:
                status_counts["error"] += 1
            else:
                status_counts["failed"] += 1

        # Cost estimate
        model = self._run_meta.get("planner_model") or (
            self._episodes[0].model if self._episodes else None
        )
        cost = self._estimate_cost(total_steps, model)

        return {
            "total_episodes": total,
            "outcome_episodes": outcome_total,
            "unscored_episodes": total - outcome_total,
            "total_steps": total_steps,
            "success_rate": (
                round(successes / outcome_total, 4) if outcome_total else 0.0
            ),
            "avg_score": (
                round(sum(scores) / outcome_total, 4) if outcome_total else 0.0
            ),
            "avg_steps_per_episode": round(total_steps / total, 2) if total else 0.0,
            "avg_time_per_episode": round(total_time / total, 2) if total else 0.0,
            "total_time": round(total_time, 2),
            "cost_estimate_usd": round(cost, 4),
            "episodes_by_status": dict(status_counts),
            "model": model,
        }

    def failure_modes(self) -> list[dict[str, Any]]:
        """Classify failed episodes into failure modes.

        Returns:
            List of dicts, each with keys: mode, count, episode_ids, percentage.
            Sorted by count descending.
        """
        mode_episodes: dict[str, list[str]] = defaultdict(list)

        for ep in self._episodes:
            mode = _classify_failure(ep, max_steps=self._max_steps)
            if mode:
                mode_episodes[mode].append(ep.episode_id)

        total_failed = sum(len(eids) for eids in mode_episodes.values())
        result = []
        for mode, eids in mode_episodes.items():
            result.append(
                {
                    "mode": mode,
                    "count": len(eids),
                    "episode_ids": eids,
                    "percentage": round(
                        len(eids) / total_failed * 100, 1
                    )
                    if total_failed
                    else 0.0,
                }
            )

        result.sort(key=lambda x: x["count"], reverse=True)
        return result

    def step_timeline(self, episode_id: str | None = None) -> list[dict[str, Any]]:
        """Get step-by-step data for a specific episode or all episodes.

        Args:
            episode_id: If provided, return steps for this episode only.
                If None, return steps from all episodes (prefixed with episode_id).

        Returns:
            List of step dicts with keys: episode_id, step_index, action_type,
            target, instruction, reasoning, decision, screenshot_path.
        """
        episodes = self._episodes
        if episode_id:
            episodes = [ep for ep in self._episodes if ep.episode_id == episode_id]

        result: list[dict[str, Any]] = []
        for ep in episodes:
            for step in ep.steps:
                result.append(
                    {
                        "episode_id": ep.episode_id,
                        "step_index": step.step_index,
                        "action_type": step.action_type,
                        "target": step.target,
                        "instruction": step.instruction,
                        "reasoning": step.reasoning,
                        "decision": step.decision,
                        "screenshot_path": step.screenshot_path,
                    }
                )

        return result

    def action_distribution(self) -> dict[str, int]:
        """Count action types across all steps.

        Returns:
            Dictionary mapping action_type -> count.
        """
        counts: Counter[str] = Counter()
        for ep in self._episodes:
            for step in ep.steps:
                action = step.action_type or "unknown"
                counts[action] += 1
        return dict(counts.most_common())

    def compare(self, other: TraceAnalyzer) -> dict[str, Any]:
        """Compare this run against another run.

        Matches episodes by task_id and classifies changes as improved,
        regressed, or unchanged.

        Args:
            other: Another TraceAnalyzer to compare against.

        Returns:
            Dictionary with keys: improved, regressed, unchanged, new_tasks,
            removed_tasks, summary_diff.
        """
        self_by_task = {ep.task_id: ep for ep in self._episodes}
        other_by_task = {ep.task_id: ep for ep in other._episodes}

        all_tasks = set(self_by_task.keys()) | set(other_by_task.keys())

        improved: list[dict[str, Any]] = []
        regressed: list[dict[str, Any]] = []
        unchanged: list[dict[str, Any]] = []
        new_tasks: list[str] = []
        removed_tasks: list[str] = []

        for task_id in sorted(all_tasks):
            self_ep = self_by_task.get(task_id)
            other_ep = other_by_task.get(task_id)

            if self_ep and not other_ep:
                removed_tasks.append(task_id)
                continue
            if other_ep and not self_ep:
                new_tasks.append(task_id)
                continue

            assert self_ep is not None and other_ep is not None

            diff_entry = {
                "task_id": task_id,
                "old_score": self_ep.score,
                "new_score": other_ep.score,
                "old_steps": self_ep.num_steps,
                "new_steps": other_ep.num_steps,
                "old_time": self_ep.elapsed_seconds,
                "new_time": other_ep.elapsed_seconds,
                "score_delta": round(other_ep.score - self_ep.score, 4),
            }

            if other_ep.score > self_ep.score:
                improved.append(diff_entry)
            elif other_ep.score < self_ep.score:
                regressed.append(diff_entry)
            else:
                unchanged.append(diff_entry)

        self_summary = self.summary()
        other_summary = other.summary()

        return {
            "improved": improved,
            "regressed": regressed,
            "unchanged": unchanged,
            "new_tasks": new_tasks,
            "removed_tasks": removed_tasks,
            "summary_diff": {
                "old": self_summary,
                "new": other_summary,
                "success_rate_delta": round(
                    other_summary["success_rate"] - self_summary["success_rate"], 4
                ),
                "avg_score_delta": round(
                    other_summary["avg_score"] - self_summary["avg_score"], 4
                ),
            },
        }

    def generate_report(
        self,
        output_path: str | Path,
        compare_with: TraceAnalyzer | None = None,
    ) -> Path:
        """Generate an HTML report to *output_path*.

        Args:
            output_path: Where to write the HTML file.
            compare_with: Optional second analyzer for comparison report.

        Returns:
            Path to the generated report.
        """
        from openadapt_evals.analysis.report_generator import generate_report

        return generate_report(
            analyzer=self,
            output_path=Path(output_path),
            compare_with=compare_with,
        )

    def _estimate_cost(self, total_steps: int, model: str | None) -> float:
        """Estimate API cost in USD for the given number of steps."""
        if not model or total_steps == 0:
            return 0.0

        # Find best matching cost entry
        costs = None
        for key, val in _COST_TABLE.items():
            if key in model or model in key:
                costs = val
                break

        if costs is None:
            # Default estimate
            costs = {"input": 3.00, "output": 15.00}

        input_cost = (
            total_steps
            * _AVG_INPUT_TOKENS_PER_STEP
            * costs["input"]
            / 1_000_000
        )
        output_cost = (
            total_steps
            * _AVG_OUTPUT_TOKENS_PER_STEP
            * costs["output"]
            / 1_000_000
        )
        return input_cost + output_cost

    def __repr__(self) -> str:
        return (
            f"TraceAnalyzer(path={self._path!r}, "
            f"episodes={len(self._episodes)}, "
            f"format={self._format!r})"
        )

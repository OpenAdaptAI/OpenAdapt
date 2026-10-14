"""Evaluation runner for benchmarks.

This module provides functions to run agents on benchmarks and collect results.

Example:
    from openadapt_evals.benchmarks import WAAMockAdapter, SmartMockAgent, evaluate_agent_on_benchmark

    adapter = WAAMockAdapter()
    agent = SmartMockAgent()
    results = evaluate_agent_on_benchmark(agent, adapter, max_steps=50)

    print(f"Success rate: {sum(r.success for r in results) / len(results):.1%}")
"""

from __future__ import annotations

import copy
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from openadapt_evals.adapters import (
    BenchmarkAction,
    BenchmarkAdapter,
    BenchmarkObservation,
    BenchmarkResult,
    BenchmarkTask,
    EvaluationUnavailableError,
    normalize_benchmark_result,
)
from openadapt_evals.agents import BenchmarkAgent
from openadapt_evals.errors import RolloutEvaluationError, RolloutInfrastructureError
from openadapt_evals.telemetry import (
    track_action_executed,
    track_agent_run,
    track_agent_run_completed,
)

if TYPE_CHECKING:
    from openadapt_evals.benchmarks.data_collection import ExecutionTraceCollector
    from openadapt_evals.benchmarks.live_tracker import LiveEvaluationTracker

logger = logging.getLogger(__name__)

_ERROR_TYPES = frozenset({"agent", "infrastructure", "evaluation"})


def _exception_error_type(error: Exception, fallback: str) -> str:
    """Return one allowed error type for an escaped exception."""
    claimed = getattr(error, "error_type", None)
    if claimed is None:
        claimed = fallback
    if isinstance(claimed, str) and claimed in _ERROR_TYPES:
        return claimed
    logger.warning(
        "Exception claimed unknown error_type=%r; classifying it as evaluation",
        claimed,
    )
    return "evaluation"


@dataclass
class EvaluationConfig:
    """Configuration for benchmark evaluation.

    Attributes:
        max_steps: Maximum steps per task.
        parallel: Number of parallel workers (if supported).
        save_trajectories: Whether to save full trajectories in results.
        verbose: Whether to print progress.
        on_step: Optional callback called after each step.
        on_task_complete: Optional callback called after each task.
        save_execution_traces: Whether to save execution traces for viewer.
        model_id: Model identifier for execution traces.
        output_dir: Output directory for benchmark results.
        run_name: Name for this evaluation run.
        enable_live_tracking: Whether to enable live evaluation progress tracking.
        live_tracking_file: Path to live tracking JSON file.
        done_gate: Whether to verify task completion before accepting agent's "done".
        done_gate_max_overrides: Max times to override a premature "done" (default 3).
        done_gate_threshold: Minimum score to accept "done" (default 1.0).
    """

    max_steps: int = 50
    parallel: int = 1
    save_trajectories: bool = True
    verbose: bool = True
    on_step: Callable[[BenchmarkObservation, BenchmarkAction, int], None] | None = None
    on_task_complete: Callable[[BenchmarkResult], None] | None = None
    save_execution_traces: bool = True
    model_id: str = "unknown"
    output_dir: str = "benchmark_results"
    run_name: str | None = None
    enable_live_tracking: bool = True
    live_tracking_file: str = "benchmark_live.json"
    done_gate: bool = False
    done_gate_max_overrides: int = 3
    done_gate_threshold: float = 1.0


def evaluate_agent_on_benchmark(
    agent: BenchmarkAgent,
    adapter: BenchmarkAdapter,
    task_ids: list[str] | None = None,
    max_steps: int = 50,
    parallel: int = 1,
    config: EvaluationConfig | None = None,
) -> list[BenchmarkResult]:
    """Run agent on benchmark tasks and collect results.

    Args:
        agent: Agent to evaluate.
        adapter: Benchmark adapter.
        task_ids: Specific tasks to run (None = all tasks).
        max_steps: Maximum steps per task (overridden by config if provided).
        parallel: Number of parallel workers (overridden by config if provided).
        config: Full evaluation configuration.

    Returns:
        List of BenchmarkResult for each task.
    """
    if config is None:
        config = EvaluationConfig(max_steps=max_steps, parallel=parallel)

    # Load tasks
    if task_ids is not None:
        tasks = [adapter.load_task(tid) for tid in task_ids]
    else:
        tasks = adapter.list_tasks()

    if config.verbose:
        logger.info(f"Evaluating {len(tasks)} tasks on {adapter.name}")
    track_agent_run(
        phase="start",
        adapter=adapter.name,
        agent_class=type(agent).__name__,
        num_tasks=len(tasks),
        max_steps=config.max_steps,
        parallel=config.parallel,
        run_name=config.run_name or "unspecified",
    )

    # Initialize execution trace collector if enabled
    trace_collector: ExecutionTraceCollector | None = None
    if config.save_execution_traces:
        from openadapt_evals.benchmarks.data_collection import ExecutionTraceCollector

        trace_collector = ExecutionTraceCollector(
            benchmark_name=adapter.name,
            run_name=config.run_name,
            model_id=config.model_id,
            output_dir=config.output_dir,
        )
        if config.verbose:
            logger.info(f"Saving execution traces to: {trace_collector.run_dir}")

    # Initialize live evaluation tracker if enabled
    live_tracker: LiveEvaluationTracker | None = None
    if config.enable_live_tracking:
        from openadapt_evals.benchmarks.live_tracker import LiveEvaluationTracker

        live_tracker = LiveEvaluationTracker(
            output_file=config.live_tracking_file,
            total_tasks=len(tasks),
        )
        if config.verbose:
            logger.info(f"Live tracking enabled: {config.live_tracking_file}")

    # Run evaluation
    if config.parallel > 1 and adapter.supports_parallel:
        results = _evaluate_parallel(agent, adapter, tasks, config, trace_collector, live_tracker)
    else:
        results = _evaluate_sequential(agent, adapter, tasks, config, trace_collector, live_tracker)

    results = [
        normalize_benchmark_result(result, context=f"returned result {index}")
        for index, result in enumerate(results)
    ]

    # Save summary if trace collection is enabled
    if trace_collector is not None:
        trace_collector.save_summary(results)

    # Mark live tracking as complete
    if live_tracker is not None:
        live_tracker.finish()

    metrics = compute_metrics(results)
    success_count = metrics["success_count"]
    avg_steps = metrics["avg_steps"]

    # Exclude evaluator and infrastructure outages from the outcome rate.
    # Agent errors remain failed outcomes and also remain visible as errors.
    if config.verbose:
        logger.info(
            f"Evaluation complete: {success_count}/{metrics['num_outcome_tasks']} "
            f"outcomes ({metrics['success_rate']:.1%}) success; "
            f"{metrics['error_count']} errors across {metrics['num_tasks']} attempts; "
            f"{avg_steps:.1f} avg steps"
        )

    track_agent_run_completed(
        adapter=adapter.name,
        agent_class=type(agent).__name__,
        num_tasks=metrics["num_tasks"],
        attempt_count=metrics["num_tasks"],
        outcome_count=metrics["num_outcome_tasks"],
        error_count=metrics["error_count"],
        success_count=success_count,
        avg_steps=round(avg_steps, 2),
        run_name=config.run_name or "unspecified",
    )

    return results


def _evaluate_sequential(
    agent: BenchmarkAgent,
    adapter: BenchmarkAdapter,
    tasks: list[BenchmarkTask],
    config: EvaluationConfig,
    trace_collector: ExecutionTraceCollector | None = None,
    live_tracker: LiveEvaluationTracker | None = None,
) -> list[BenchmarkResult]:
    """Run evaluation sequentially.

    Args:
        agent: Agent to evaluate.
        adapter: Benchmark adapter.
        tasks: Tasks to evaluate.
        config: Evaluation configuration.
        trace_collector: Optional trace collector for saving execution data.
        live_tracker: Optional live evaluation tracker.

    Returns:
        List of results.
    """
    results = []
    for i, task in enumerate(tasks):
        if config.verbose:
            logger.info(f"Task {i + 1}/{len(tasks)}: {task.task_id}")

        try:
            result = _run_single_task(
                agent, adapter, task, config, trace_collector, live_tracker
            )
        except Exception as e:
            logger.error(f"Task {task.task_id} failed with error: {e}")
            result = _failed_task_result(task, e)
        results.append(result)

        if config.on_task_complete:
            config.on_task_complete(result)

    return results


def _evaluate_parallel(
    agent: BenchmarkAgent,
    adapter: BenchmarkAdapter,
    tasks: list[BenchmarkTask],
    config: EvaluationConfig,
    trace_collector: ExecutionTraceCollector | None = None,
    live_tracker: LiveEvaluationTracker | None = None,
) -> list[BenchmarkResult]:
    """Run evaluation in parallel.

    Note: This requires the adapter to support parallel execution
    (e.g., via multiple VM instances).

    Args:
        agent: Agent to evaluate.
        adapter: Benchmark adapter.
        tasks: Tasks to evaluate.
        config: Evaluation configuration.
        trace_collector: Optional trace collector for saving execution data.
        live_tracker: Optional live evaluation tracker.

    Returns:
        List of results.
    """
    results = []

    with ThreadPoolExecutor(max_workers=config.parallel) as executor:
        # Submit all tasks
        future_to_task = {
            executor.submit(_run_single_task, agent, adapter, task, config, trace_collector, live_tracker): task
            for task in tasks
        }

        # Collect results as they complete
        for future in as_completed(future_to_task):
            task = future_to_task[future]
            try:
                result = future.result()
                results.append(result)

                if config.on_task_complete:
                    config.on_task_complete(result)

                if config.verbose:
                    status = "SUCCESS" if result.success else "FAIL"
                    logger.info(f"Task {task.task_id}: {status}")

            except Exception as e:
                logger.error(f"Task {task.task_id} failed with error: {e}")
                results.append(_failed_task_result(task, e))

    return results


def _failed_task_result(task: BenchmarkTask, error: Exception) -> BenchmarkResult:
    """Convert an escaped worker error into an explicit unmeasured attempt."""
    return BenchmarkResult(
        task_id=task.task_id,
        success=False,
        score=0.0,
        error=str(error),
        reason=str(error),
        error_type=_exception_error_type(error, "infrastructure"),
    )


def _run_single_task(
    agent: BenchmarkAgent,
    adapter: BenchmarkAdapter,
    task: BenchmarkTask,
    config: EvaluationConfig,
    trace_collector: ExecutionTraceCollector | None = None,
    live_tracker: LiveEvaluationTracker | None = None,
) -> BenchmarkResult:
    """Run a single task and return result.

    Args:
        agent: Agent to evaluate.
        adapter: Benchmark adapter.
        task: Task to run.
        config: Evaluation configuration.
        trace_collector: Optional trace collector for saving execution data.
        live_tracker: Optional live evaluation tracker.

    Returns:
        BenchmarkResult.
    """
    start_time = time.perf_counter()
    history: list[tuple[BenchmarkObservation, BenchmarkAction]] = []
    failure_error_type = "infrastructure"

    # Start trace collection if enabled
    if trace_collector is not None:
        trace_collector.start_task(task)

    # Start live tracking if enabled
    if live_tracker is not None:
        live_tracker.start_task(task)

    try:
        # Reset agent and environment
        logger.info(f"Resetting environment for task {task.task_id}")
        agent.reset()
        obs = adapter.reset(task)
        logger.info("Environment reset complete, starting task execution")

        done = False
        steps = 0
        action = None
        done_gate_overrides = 0
        max_steps = task.time_limit_steps or config.max_steps

        while not done and steps < max_steps:
            logger.info(f"Step {steps}: Getting action from agent")

            # Get action from agent
            try:
                failure_error_type = "agent"
                think_start = time.perf_counter()
                action = agent.act(obs, task, history if config.save_trajectories else None)
                think_end = time.perf_counter()
                logger.info(f"Step {steps}: Agent chose action: {action.type}")
            except Exception as e:
                logger.error(f"Step {steps}: Failed to get action from agent: {e}")
                raise

            # Extract reasoning if available from PolicyAgent
            reasoning = None
            if hasattr(action, "raw_action") and action.raw_action:
                if isinstance(action.raw_action, dict):
                    reasoning = action.raw_action.get("thought")
                    if reasoning:
                        logger.info(f"Step {steps}: Agent reasoning: {reasoning[:100]}...")

            # Extract agent logs (from ApiAgent._last_step_logs)
            agent_logs = getattr(agent, "_last_step_logs", None)
            if agent_logs:
                # Add timing data
                agent_logs["agent_think_ms"] = round((think_end - think_start) * 1000)

            # Record step in trace collector
            if trace_collector is not None:
                trace_collector.record_step(steps, obs, action, reasoning, agent_logs=agent_logs)

            # Record step in live tracker
            if live_tracker is not None:
                live_tracker.record_step(steps, obs, action, reasoning)

            # Record step in history
            if config.save_trajectories:
                history.append((obs, action))

            if config.on_step:
                config.on_step(obs, action, steps)

            # Check for terminal action
            if action.type in ("done", "error"):
                if action.type == "error":
                    logger.error(f"Step {steps}: Agent error: {action.raw_action}")
                    done = True
                    break

                # Agent says "done" — apply done-gate if enabled
                logger.info(f"Step {steps}: Agent signaled task completion")

                if (
                    config.done_gate
                    and done_gate_overrides < config.done_gate_max_overrides
                ):
                    logger.info(
                        f"Step {steps}: Done-gate active — evaluating task "
                        f"(override {done_gate_overrides + 1}/{config.done_gate_max_overrides})"
                    )
                    try:
                        failure_error_type = "evaluation"
                        gate_result = normalize_benchmark_result(
                            adapter.evaluate(task),
                            expected_task_id=task.task_id,
                            context="done-gate result",
                        )
                    except Exception as e:
                        if getattr(e, "error_type", None) is not None:
                            raise
                        raise RolloutEvaluationError(
                            f"Done-gate evaluation failed: {e}"
                        ) from e

                    if gate_result.error_type is not None:
                        message = (
                            gate_result.reason
                            or gate_result.error
                            or "done-gate evaluation did not produce a measured result"
                        )
                        raise EvaluationUnavailableError(
                            str(message), error_type=gate_result.error_type
                        )

                    gate_score = gate_result.score

                    if gate_score >= config.done_gate_threshold:
                        logger.info(
                            f"Step {steps}: Done-gate PASSED "
                            f"(score={gate_score:.2f} >= {config.done_gate_threshold:.2f})"
                        )
                        done = True
                        break

                    # Override the premature "done"
                    done_gate_overrides += 1
                    logger.warning(
                        f"Step {steps}: Done-gate REJECTED premature 'done' "
                        f"(score={gate_score:.2f} < {config.done_gate_threshold:.2f}, "
                        f"override {done_gate_overrides}/{config.done_gate_max_overrides})"
                    )

                    # Modify the task instruction to tell the agent to continue.
                    # Strip any previous done-gate message before appending the new one.
                    _DONE_GATE_MARKER = "\n\n[SYSTEM: The task is NOT yet complete"
                    continuation_msg = (
                        "\n\n[SYSTEM: The task is NOT yet complete based on automated "
                        "evaluation (score: {score:.0%}). Your previous 'done' signal "
                        "was overridden ({n}/{max}). Please examine the current screen "
                        "carefully and continue working on the task. Do NOT declare "
                        "'done' unless the task is truly finished.]"
                    ).format(
                        score=gate_score,
                        n=done_gate_overrides,
                        max=config.done_gate_max_overrides,
                    )

                    # Create a modified task with continuation message
                    task = copy.copy(task)
                    # Remove previous done-gate message if present
                    marker_idx = task.instruction.find(_DONE_GATE_MARKER)
                    if marker_idx >= 0:
                        task.instruction = task.instruction[:marker_idx]
                    task.instruction = task.instruction + continuation_msg

                    # Refresh only through an observation-only adapter method.
                    # Never synthesize input to request a screenshot.
                    failure_error_type = "infrastructure"
                    observe = getattr(adapter, "observe", None)
                    if not callable(observe):
                        raise RolloutInfrastructureError(
                            "Done-gate rejected completion, but the adapter cannot "
                            "provide a fresh observation"
                        )
                    try:
                        refreshed_obs = observe()
                    except Exception as e:
                        raise RolloutInfrastructureError(
                            "Done-gate rejected completion, but the fresh observation "
                            f"failed: {e}"
                        ) from e
                    if not isinstance(refreshed_obs, BenchmarkObservation):
                        raise RolloutInfrastructureError(
                            "Done-gate rejected completion, but the adapter returned "
                            "an invalid fresh observation"
                        )
                    obs = refreshed_obs

                    steps += 1
                    continue
                else:
                    if config.done_gate and done_gate_overrides >= config.done_gate_max_overrides:
                        logger.warning(
                            f"Step {steps}: Done-gate max overrides reached "
                            f"({config.done_gate_max_overrides}). Accepting 'done'."
                        )
                    done = True
                    break

            # Execute action
            try:
                failure_error_type = "infrastructure"
                logger.info(f"Step {steps}: Executing action in environment")
                exec_start = time.perf_counter()
                obs, done, info = adapter.step(action)
                exec_end = time.perf_counter()
                track_action_executed(
                    task_id=task.task_id,
                    step_index=steps,
                    action_type=action.type,
                    adapter=adapter.name,
                    agent_class=type(agent).__name__,
                )
                if agent_logs:
                    agent_logs["env_execute_ms"] = round((exec_end - exec_start) * 1000)
                if done:
                    logger.info(f"Step {steps}: Environment signaled task completion")
            except Exception as e:
                logger.error(f"Step {steps}: Failed to execute action: {e}")
                raise

            steps += 1

        if steps >= max_steps:
            logger.warning(f"Task reached maximum steps ({max_steps})")

        # An evaluator observes target state, not whether the agent itself
        # completed this attempt safely. A pre-existing target state can score
        # as successful after an agent error. Do not call the evaluator for a
        # terminal error because evaluator unavailability must not hide a valid
        # agent failure from the benchmark denominator.
        if action is not None and action.type == "error":
            raw_action = action.raw_action or {}
            claimed_error_type = raw_action.get("error_type")
            if claimed_error_type not in (None, "agent"):
                logger.warning(
                    "Agent terminal error claimed error_type=%r; classifying the "
                    "attempt as an agent outcome",
                    claimed_error_type,
                )
            error_reason = (
                raw_action.get("reason")
                or raw_action.get("error")
                or raw_action.get("parse_error")
                or raw_action.get("fail_reason")
                or "agent reported a terminal error"
            )
            result = BenchmarkResult(
                task_id=task.task_id,
                success=False,
                score=0.0,
                error_type="agent",
                error=str(error_reason),
                reason=str(error_reason),
            )
        else:
            logger.info("Evaluating task result")
            failure_error_type = "evaluation"
            try:
                result = normalize_benchmark_result(
                    adapter.evaluate(task),
                    expected_task_id=task.task_id,
                    context="final evaluator result",
                )
            except Exception as e:
                if getattr(e, "error_type", None) is not None:
                    raise
                raise RolloutEvaluationError(f"Task evaluation failed: {e}") from e

        # Update result with trajectory info
        result.steps = history if config.save_trajectories else []
        result.num_steps = steps
        result.total_time_seconds = time.perf_counter() - start_time
        result = normalize_benchmark_result(
            result,
            expected_task_id=task.task_id,
            context="final task result",
        )

        # Log final result
        if result.success:
            logger.info(f"[SUCCESS] Task {task.task_id} completed successfully (score: {result.score:.2f})")
        else:
            logger.error(f"Task {task.task_id} failed (score: {result.score:.2f})")
            if result.error:
                logger.error(f"Error reason: {result.error}")

        # Finish trace collection if enabled
        if trace_collector is not None:
            trace_collector.finish_task(result)

        # Finish live tracking if enabled
        if live_tracker is not None:
            live_tracker.finish_task(result)

        return result

    except Exception as e:
        logger.error(f"Error running task {task.task_id}: {e}")
        error_type = _exception_error_type(e, failure_error_type)
        result = BenchmarkResult(
            task_id=task.task_id,
            success=False,
            score=0.0,
            steps=history if config.save_trajectories else [],
            num_steps=len(history),
            error=str(e),
            reason=str(e),
            error_type=error_type,
            total_time_seconds=time.perf_counter() - start_time,
        )

        # Finish trace collection even on error
        if trace_collector is not None:
            trace_collector.finish_task(result)

        return normalize_benchmark_result(
            result,
            expected_task_id=task.task_id,
            context="escaped task result",
        )


def compute_metrics(results: list[BenchmarkResult]) -> dict:
    """Compute outcomes without scoring unavailable evaluation attempts.

    Args:
        results: List of BenchmarkResult from evaluation.

    Returns:
        Dict with aggregate metrics.
    """
    if not results:
        return {
            "num_tasks": 0,
            "num_attempts": 0,
            "num_outcome_tasks": 0,
            "error_count": 0,
            "success_rate": 0.0,
            "avg_score": 0.0,
            "avg_steps": 0.0,
            "avg_time_seconds": 0.0,
            "num_infrastructure_failures": 0,
            "num_evaluation_failures": 0,
            "num_agent_failures": 0,
            "num_tasks_excluding_infra": 0,
            "success_rate_excluding_infra": 0.0,
            "success_count": 0,
            "fail_count": 0,
        }

    results = [
        normalize_benchmark_result(result, context=f"aggregate result {index}")
        for index, result in enumerate(results)
    ]
    num_tasks = len(results)
    unavailable_types = {"infrastructure", "evaluation"}
    outcomes = [
        result for result in results if result.error_type not in unavailable_types
    ]
    success_count = sum(1 for result in outcomes if result.success)
    total_score = sum(result.score for result in outcomes)
    total_steps = sum(r.num_steps for r in results)
    total_time = sum(r.total_time_seconds for r in results)
    infra_failures = [r for r in results if r.error_type == "infrastructure"]
    evaluation_failures = [r for r in results if r.error_type == "evaluation"]
    agent_failures = [r for r in results if r.error_type == "agent"]
    outcome_count = len(outcomes)
    outcome_fail_count = outcome_count - success_count
    error_count = sum(1 for result in results if result.error_type is not None)

    return {
        "num_tasks": num_tasks,
        "num_attempts": num_tasks,
        "num_outcome_tasks": outcome_count,
        "error_count": error_count,
        "success_rate": success_count / outcome_count if outcomes else 0.0,
        "avg_score": total_score / outcome_count if outcomes else 0.0,
        "avg_steps": total_steps / num_tasks,
        "avg_time_seconds": total_time / num_tasks,
        "success_count": success_count,
        "fail_count": outcome_fail_count,
        "num_infrastructure_failures": len(infra_failures),
        "num_evaluation_failures": len(evaluation_failures),
        "num_agent_failures": len(agent_failures),
        # Compatibility aliases. Evaluator and infrastructure outages are
        # excluded. Agent errors remain failed benchmark outcomes.
        "num_tasks_excluding_infra": outcome_count,
        "success_rate_excluding_infra": (
            success_count / outcome_count if outcomes else 0.0
        ),
    }


def compute_domain_metrics(
    results: list[BenchmarkResult], tasks: list[BenchmarkTask]
) -> dict[str, dict]:
    """Compute per-domain metrics.

    Args:
        results: List of BenchmarkResult.
        tasks: List of BenchmarkTask (to get domain info).

    Returns:
        Dict mapping domain to metrics dict.
    """
    # Build task_id -> domain mapping
    task_domains = {t.task_id: t.domain for t in tasks}

    # Group results by domain
    domain_results: dict[str, list[BenchmarkResult]] = {}
    for result in results:
        domain = task_domains.get(result.task_id, "unknown")
        if domain not in domain_results:
            domain_results[domain] = []
        domain_results[domain].append(result)

    # Compute metrics per domain
    return {domain: compute_metrics(res) for domain, res in domain_results.items()}

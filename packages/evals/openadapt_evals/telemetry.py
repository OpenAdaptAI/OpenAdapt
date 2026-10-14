"""openadapt-evals telemetry wrapper.

Thin adapter over openadapt-telemetry PostHog usage events.
"""

from __future__ import annotations

from typing import Any

from openadapt_telemetry.posthog import capture_usage_event

_PACKAGE_NAME = "openadapt-evals"


def _compact(properties: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in properties.items() if value is not None}


def capture_event(event: str, properties: dict[str, Any] | None = None) -> bool:
    """Capture a raw usage event."""
    return capture_usage_event(
        event=event,
        properties=_compact(properties or {}),
        package_name=_PACKAGE_NAME,
    )


def track_agent_run(
    *,
    phase: str,
    adapter: str | None = None,
    agent_class: str | None = None,
    entrypoint: str | None = None,
    mode: str | None = None,
    num_tasks: int | None = None,
    max_steps: int | None = None,
    parallel: int | None = None,
    run_name: str | None = None,
) -> bool:
    properties = {
        "phase": phase,
        "adapter": adapter,
        "agent_class": agent_class,
        "entrypoint": entrypoint,
        "mode": mode,
        "num_tasks": num_tasks,
        "max_steps": max_steps,
        "parallel": parallel,
        "run_name": run_name,
    }
    return capture_event("agent_run", properties)


def track_agent_run_completed(
    *,
    adapter: str | None = None,
    agent_class: str | None = None,
    entrypoint: str | None = None,
    mode: str | None = None,
    num_tasks: int | None = None,
    attempt_count: int | None = None,
    outcome_count: int | None = None,
    error_count: int | None = None,
    success_count: int | None = None,
    avg_steps: float | None = None,
    return_code: int | None = None,
    duration_seconds: float | None = None,
    run_name: str | None = None,
) -> bool:
    properties = {
        "adapter": adapter,
        "agent_class": agent_class,
        "entrypoint": entrypoint,
        "mode": mode,
        "num_tasks": num_tasks,
        "attempt_count": attempt_count,
        "outcome_count": outcome_count,
        "error_count": error_count,
        "success_count": success_count,
        "avg_steps": avg_steps,
        "return_code": return_code,
        "duration_seconds": duration_seconds,
        "run_name": run_name,
    }
    return capture_event("agent_run_completed", properties)


def track_action_executed(
    *,
    task_id: str | None = None,
    step_index: int | None = None,
    action_type: str | None = None,
    adapter: str | None = None,
    agent_class: str | None = None,
) -> bool:
    properties = {
        "task_id": task_id,
        "step_index": step_index,
        "action_type": action_type,
        "adapter": adapter,
        "agent_class": agent_class,
    }
    return capture_event("action_executed", properties)


def track_training_run(
    *,
    phase: str,
    model_name: str | None = None,
    num_steps: int | None = None,
    num_rollouts_per_step: int | None = None,
    task_count: int | None = None,
    constrained_decoding: bool | None = None,
    vision_loss_mode: str | None = None,
    reward_mean: float | None = None,
    loss: float | None = None,
    duration_seconds: float | None = None,
) -> bool:
    return capture_event("training_run", {
        "phase": phase, "model_name": model_name,
        "num_steps": num_steps, "num_rollouts_per_step": num_rollouts_per_step,
        "task_count": task_count, "constrained_decoding": constrained_decoding,
        "vision_loss_mode": vision_loss_mode, "reward_mean": reward_mean,
        "loss": loss, "duration_seconds": duration_seconds,
    })


def track_training_step(
    *,
    step: int | None = None,
    task_id: str | None = None,
    reward_mean: float | None = None,
    loss: float | None = None,
    step_time: float | None = None,
) -> bool:
    return capture_event("training_step_completed", {
        "step": step, "task_id": task_id,
        "reward_mean": reward_mean, "loss": loss,
        "step_time": step_time,
    })


def track_rollout_collected(
    *,
    task_id: str | None = None,
    num_steps: int | None = None,
    reward: float | None = None,
) -> bool:
    return capture_event("rollout_collected", {
        "task_id": task_id, "num_steps": num_steps, "reward": reward,
    })


def track_checkpoint_saved(*, step: int | None = None) -> bool:
    return capture_event("checkpoint_saved", {"step": step})


def track_demo_execution(
    *,
    phase: str,
    task_id: str | None = None,
    num_steps: int | None = None,
    score: float | None = None,
    duration_seconds: float | None = None,
    tier1_count: int | None = None,
    tier2_count: int | None = None,
) -> bool:
    return capture_event("demo_execution", {
        "phase": phase, "task_id": task_id,
        "num_steps": num_steps, "score": score,
        "duration_seconds": duration_seconds,
        "tier1_count": tier1_count, "tier2_count": tier2_count,
    })


def track_correction(
    *,
    phase: str,
    task_id: str | None = None,
    entry_id: str | None = None,
) -> bool:
    return capture_event(f"correction_{phase}", {
        "task_id": task_id, "entry_id": entry_id,
    })


def track_demo_recorded(
    *,
    task_id: str | None = None,
    mode: str | None = None,
    steps: int | None = None,
    output_dir: str | None = None,
    phase: str | None = None,
) -> bool:
    properties = {
        "task_id": task_id,
        "mode": mode,
        "steps": steps,
        "output_dir": output_dir,
        "phase": phase,
    }
    return capture_event("demo_recorded", properties)

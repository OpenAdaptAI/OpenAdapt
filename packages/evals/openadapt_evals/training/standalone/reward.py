"""Reward: group-relative advantages + VLM milestone evaluation. No openadapt-ml imports."""

from __future__ import annotations

import io
import logging
import math
from numbers import Real
from typing import Any

from PIL import Image

from openadapt_evals.errors import RolloutEvaluationError, RolloutLossError

logger = logging.getLogger(__name__)


def compute_group_advantages(rewards: list[float]) -> list[float]:
    """GRPO group-relative advantages: (r - mean) / (std + eps)."""
    n = len(rewards)
    if n == 0:
        raise RolloutLossError("Cannot compute advantages without rewards")
    measured_rewards: list[float] = []
    for index, reward in enumerate(rewards):
        if isinstance(reward, bool) or not isinstance(reward, Real):
            raise RolloutLossError(f"Reward {index} must be numeric")
        measured = float(reward)
        if not math.isfinite(measured) or not 0.0 <= measured <= 1.0:
            raise RolloutLossError(
                f"Reward {index} must be finite and within [0, 1]"
            )
        measured_rewards.append(measured)

    mean = sum(measured_rewards) / n
    variance = sum((reward - mean) ** 2 for reward in measured_rewards) / n
    std = variance**0.5
    if std < 1e-8:
        return [0.0] * n
    advantages = [
        (reward - mean) / (std + 1e-8) for reward in measured_rewards
    ]
    if any(not math.isfinite(advantage) for advantage in advantages):
        raise RolloutLossError("Group advantage calculation produced a non-finite value")
    return advantages


def evaluate_milestones_screenshot(
    task_config: Any, screenshot: bytes, *, model: str = "gpt-4.1-mini",
) -> float:
    """Measure screenshot milestones or raise when no score was produced."""
    milestones = list(getattr(task_config, "milestones", []) or [])
    if not milestones:
        raise RolloutEvaluationError("No screenshot milestone contract is configured")
    unsupported = [
        milestone.name
        for milestone in milestones
        if milestone.check.check != "screenshot"
    ]
    if unsupported:
        raise RolloutEvaluationError(
            "Screenshot reward cannot measure required milestones: "
            + ", ".join(repr(name) for name in unsupported)
        )
    if not isinstance(screenshot, bytes) or not screenshot:
        raise RolloutEvaluationError("Milestone evaluation requires screenshot bytes")
    try:
        with Image.open(io.BytesIO(screenshot)) as image:
            image.load()
    except Exception as exc:
        raise RolloutEvaluationError(
            "Milestone evaluation requires a decodable screenshot"
        ) from exc
    from openadapt_evals.vlm_evaluator import vlm_judge

    passed = 0
    for milestone in milestones:
        description = milestone.check.description
        if not description:
            raise RolloutEvaluationError(
                f"Screenshot milestone {milestone.name!r} has no description"
            )
        try:
            success, confidence = vlm_judge(screenshot, description, model=model)
        except Exception as exc:
            raise RolloutEvaluationError(
                f"Screenshot milestone {milestone.name!r} could not be evaluated"
            ) from exc
        if not isinstance(success, bool):
            raise RolloutEvaluationError(
                f"Screenshot milestone {milestone.name!r} returned no YES/NO verdict"
            )
        if isinstance(confidence, bool) or not isinstance(confidence, Real):
            raise RolloutEvaluationError(
                f"Screenshot milestone {milestone.name!r} returned invalid confidence"
            )
        measured_confidence = float(confidence)
        if not math.isfinite(measured_confidence) or not 0.0 <= measured_confidence <= 1.0:
            raise RolloutEvaluationError(
                f"Screenshot milestone {milestone.name!r} returned invalid confidence"
            )
        passed += int(success)
    reward = passed / len(milestones)
    if not math.isfinite(reward) or not 0.0 <= reward <= 1.0:
        raise RolloutEvaluationError("Milestone reward was not a finite unit value")
    return reward

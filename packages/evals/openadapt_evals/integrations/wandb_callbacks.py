"""W&B callback functions for the standalone GRPO trainer.

Lazily imports wandb so the module loads even without wandb installed.
"""

from __future__ import annotations

import io
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _get_wandb():
    """Return the wandb module if available and a run is active, else None."""
    try:
        import wandb
    except ImportError:
        logger.warning("wandb is not installed -- skipping callback")
        return None
    if wandb.run is None:
        logger.warning("No active wandb run -- call wandb.init() before training")
        return None
    return wandb


def wandb_model_loaded(model: Any, processor: Any) -> None:
    """Log model metadata to ``wandb.config``."""
    wandb = _get_wandb()
    if wandb is None:
        return

    param_count = sum(p.numel() for p in model.parameters())
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)

    wandb.config.update({
        "model_name": getattr(model, "name_or_path", getattr(model.config, "_name_or_path", "unknown")),
        "param_count": param_count,
        "trainable_params": trainable,
        "lora_config": str(getattr(model, "peft_config", None)),
    }, allow_val_change=True)


def wandb_rollout_logger(rollout: Any, index: int) -> None:
    """Log per-rollout metrics and optional first/last screenshots."""
    wandb = _get_wandb()
    if wandb is None:
        return

    log_dict: dict[str, Any] = {
        "rollout/reward": rollout.reward,
        "rollout/num_steps": len(rollout.steps),
        "rollout/task_id": rollout.task_id,
        "rollout/index": index,
    }

    if rollout.steps:
        from PIL import Image

        first_png = rollout.steps[0].screenshot
        if first_png:
            log_dict["rollout/screenshot_first"] = wandb.Image(
                Image.open(io.BytesIO(first_png)), caption=f"step 0 | {rollout.task_id}"
            )
        last_png = rollout.steps[-1].screenshot
        if last_png and len(rollout.steps) > 1:
            log_dict["rollout/screenshot_last"] = wandb.Image(
                Image.open(io.BytesIO(last_png)),
                caption=f"step {len(rollout.steps) - 1} | {rollout.task_id}",
            )

    wandb.log(log_dict)


def wandb_step_logger(step: int, rollouts: list[Any], metrics: dict[str, Any]) -> None:
    """Log per-training-step metrics and a reward histogram."""
    wandb = _get_wandb()
    if wandb is None:
        return

    rewards = [r.reward for r in rollouts]

    log_dict: dict[str, Any] = {
        "step": step,
        "train/reward_mean": metrics.get("reward_mean", sum(rewards) / max(len(rewards), 1)),
        "train/num_rollouts": len(rollouts),
    }

    if "loss" in metrics:
        log_dict["train/loss"] = metrics["loss"]
    if "step_time" in metrics:
        log_dict["train/step_time"] = metrics["step_time"]

    for key, val in metrics.items():
        if key not in {"reward_mean", "loss", "step_time"} and isinstance(val, (int, float)):
            log_dict[f"train/{key}"] = val

    if rewards:
        log_dict["train/reward_hist"] = wandb.Histogram(rewards)

    wandb.log(log_dict)

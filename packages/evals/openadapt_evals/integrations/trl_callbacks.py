"""TRL TrainerCallback implementations for telemetry, diagnostics, and Weave tracing.

Provides callbacks that integrate with TRL's GRPOTrainer to automatically
track training events via our telemetry system, emit rich diagnostic logs
matching the standalone trainer output, and optionally log to Weave.

Usage::

    from trl import GRPOConfig, GRPOTrainer
    from openadapt_evals.integrations.trl_callbacks import (
        DiagnosticsCallback,
        TelemetryCallback,
    )

    trainer = GRPOTrainer(
        model=model,
        args=config,
        callbacks=[
            TelemetryCallback(model_name="Qwen/Qwen2.5-VL-7B-Instruct"),
            DiagnosticsCallback(),
        ],
        ...
    )
    trainer.train()
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class TelemetryCallback:
    """TRL TrainerCallback that emits telemetry events for training lifecycle.

    Calls our telemetry functions at key training milestones:
    - ``on_train_begin`` -> ``track_training_run(phase="start", ...)``
    - ``on_step_end``    -> ``track_training_step(step=..., reward_mean=..., loss=...)``
    - ``on_save``        -> ``track_checkpoint_saved(step=...)``
    - ``on_train_end``   -> ``track_training_run(phase="completed", ...)``

    Inherits from ``transformers.TrainerCallback`` at runtime so it can be
    passed directly to TRL's ``GRPOTrainer(callbacks=[...])``.

    All telemetry calls are wrapped in try/except so failures never interrupt
    training.
    """

    def __init__(
        self,
        model_name: str | None = None,
        task_count: int | None = None,
        constrained_decoding: bool = False,
        vision_loss_mode: str | None = None,
    ) -> None:
        self.model_name = model_name
        self.task_count = task_count
        self.constrained_decoding = constrained_decoding
        self.vision_loss_mode = vision_loss_mode
        self._train_start_time: float | None = None

    def on_train_begin(
        self,
        args: Any,
        state: Any,
        control: Any,
        **kwargs: Any,
    ) -> None:
        """Called at the start of training."""
        self._train_start_time = time.time()
        try:
            from openadapt_evals.telemetry import track_training_run

            num_steps = getattr(args, "max_steps", None)
            num_rollouts = getattr(args, "num_generations", None)
            track_training_run(
                phase="start",
                model_name=self.model_name,
                num_steps=num_steps,
                num_rollouts_per_step=num_rollouts,
                task_count=self.task_count,
                constrained_decoding=self.constrained_decoding,
                vision_loss_mode=self.vision_loss_mode,
            )
            logger.debug("Telemetry: training_run start emitted")
        except Exception as exc:
            logger.debug("Telemetry on_train_begin failed: %s", exc)

    def on_step_end(
        self,
        args: Any,
        state: Any,
        control: Any,
        **kwargs: Any,
    ) -> None:
        """Called at the end of each training step."""
        try:
            from openadapt_evals.telemetry import track_training_step

            # TRL logs metrics in state.log_history
            reward_mean = None
            loss = None
            if state.log_history:
                last_log = state.log_history[-1]
                reward_mean = last_log.get("reward", last_log.get("reward_mean"))
                loss = last_log.get("loss")

            track_training_step(
                step=state.global_step,
                reward_mean=reward_mean,
                loss=loss,
            )
        except Exception as exc:
            logger.debug("Telemetry on_step_end failed: %s", exc)

    def on_save(
        self,
        args: Any,
        state: Any,
        control: Any,
        **kwargs: Any,
    ) -> None:
        """Called when a checkpoint is saved."""
        try:
            from openadapt_evals.telemetry import track_checkpoint_saved

            track_checkpoint_saved(step=state.global_step)
            logger.debug("Telemetry: checkpoint_saved at step %d", state.global_step)
        except Exception as exc:
            logger.debug("Telemetry on_save failed: %s", exc)

    def on_train_end(
        self,
        args: Any,
        state: Any,
        control: Any,
        **kwargs: Any,
    ) -> None:
        """Called at the end of training."""
        try:
            from openadapt_evals.telemetry import track_training_run

            duration = None
            if self._train_start_time is not None:
                duration = time.time() - self._train_start_time

            # Extract final reward from last log entry
            reward_mean = None
            loss = None
            if state.log_history:
                last_log = state.log_history[-1]
                reward_mean = last_log.get("reward", last_log.get("reward_mean"))
                loss = last_log.get("loss")

            track_training_run(
                phase="completed",
                model_name=self.model_name,
                num_steps=state.global_step,
                duration_seconds=duration,
                reward_mean=reward_mean,
                loss=loss,
            )
            logger.debug("Telemetry: training_run completed emitted")
        except Exception as exc:
            logger.debug("Telemetry on_train_end failed: %s", exc)


class DiagnosticsCallback:
    """Rich training diagnostics matching standalone trainer output.

    Emits per-step log lines with loss, |loss|, grad_norm, and reward in the
    same format as the standalone GRPOTrainer. This makes it easy for operators
    to monitor TRL-based training runs with the same tooling (grep, dashboards)
    used for the standalone path.

    All values are read from TRL's ``state.log_history``. If a metric is
    missing, it defaults to 0.0.
    """

    def on_step_end(
        self,
        args: Any,
        state: Any,
        control: Any,
        **kwargs: Any,
    ) -> None:
        """Log diagnostic metrics at the end of each training step."""
        if not state.log_history:
            return
        latest = state.log_history[-1]
        loss = latest.get("loss", 0.0)
        grad_norm = latest.get("grad_norm", 0.0)
        reward = latest.get("reward", latest.get("reward_mean", 0.0))
        logger.info(
            "Step %d: loss=%+.2e |loss|=%.2e grad_norm=%.4f reward=%.4f",
            state.global_step,
            loss,
            abs(loss),
            grad_norm,
            reward,
        )


# Register as a TrainerCallback subclass at import time so TRL recognizes it.
# If transformers is installed, wrap with proper inheritance.
# We can't patch __bases__ after the fact (Python doesn't allow it when
# deallocators differ), so we create a subclass instead.
try:
    from transformers import TrainerCallback as _TrainerCallback

    class _TelemetryCallbackWithBase(_TrainerCallback, TelemetryCallback):
        """TelemetryCallback with proper TrainerCallback inheritance."""
        pass

    class _DiagnosticsCallbackWithBase(_TrainerCallback, DiagnosticsCallback):
        """DiagnosticsCallback with proper TrainerCallback inheritance."""
        pass

    # Replace the module-level names so imports get the subclasses
    TelemetryCallback = _TelemetryCallbackWithBase  # type: ignore[misc]
    DiagnosticsCallback = _DiagnosticsCallbackWithBase  # type: ignore[misc]
except ImportError:
    pass  # Callbacks work as duck-typed without inheritance

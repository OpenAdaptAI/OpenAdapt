"""Standalone GRPO trainer: rollout collection + training loop.

REINFORCE with group-relative advantages. Direct HTTP to WAA,
standard HF+PEFT, standalone loss math. NO openadapt-ml imports.

Usage:
    python -m openadapt_evals.training.standalone.trainer \\
        --task-dir example_tasks --server-url http://localhost:5001 \\
        --model Qwen/Qwen3.5-9B --num-steps 10 --output checkpoints/
"""

from __future__ import annotations

import argparse
import io
import logging
import math
import re
import time
from numbers import Real
from pathlib import Path
from typing import Any

from PIL import Image

from openadapt_evals.errors import RolloutInfrastructureError, RolloutLossError
from openadapt_evals.telemetry import (
    track_checkpoint_saved,
    track_rollout_collected,
    track_training_run,
    track_training_step,
)
from openadapt_evals.training.standalone.config import TrainingConfig
from openadapt_evals.training.standalone.model_loader import load_model_and_processor
from openadapt_evals.training.standalone.prompt import (
    build_agent_messages,
    format_action_as_text,
    parse_vlm_output_to_action,
)
from openadapt_evals.training.standalone.reward import (
    compute_group_advantages,
    evaluate_milestones_screenshot,
)
from openadapt_evals.training.standalone.waa_direct import Rollout, RolloutStep, WAADirect

logger = logging.getLogger(__name__)


def policy_gradient_loss(current_logps, old_logps, advantages, epsilon=0.2):
    """PPO-clipped policy gradient. Single-epoch: reduces to REINFORCE."""
    import torch

    if not isinstance(epsilon, Real) or not math.isfinite(float(epsilon)):
        raise RolloutLossError("Policy loss epsilon must be finite")
    if not 0.0 <= float(epsilon) < 1.0:
        raise RolloutLossError("Policy loss epsilon must be within [0, 1)")
    for name, value in (
        ("current log probabilities", current_logps),
        ("old log probabilities", old_logps),
        ("advantages", advantages),
    ):
        try:
            finite = bool(torch.isfinite(value).all().item())
        except Exception as exc:
            raise RolloutLossError(f"Policy loss {name} are not numeric tensors") from exc
        if not finite:
            raise RolloutLossError(f"Policy loss {name} must be finite")

    ratio = torch.exp(current_logps - old_logps)
    if not bool(torch.isfinite(ratio).all().item()):
        raise RolloutLossError("Policy loss ratio must be finite")
    clipped = torch.clamp(ratio, 1 - epsilon, 1 + epsilon)
    loss = -torch.min(ratio * advantages, clipped * advantages).mean()
    if not bool(torch.isfinite(loss).all().item()):
        raise RolloutLossError("Policy loss must be finite")
    return loss


class GRPOTrainer:
    """Standalone GRPO trainer with direct WAA HTTP integration."""

    def __init__(
        self,
        config: TrainingConfig,
        *,
        on_model_loaded: Any | None = None,
        on_before_collect: Any | None = None,
        on_rollout_complete: Any | None = None,
        on_step_complete: Any | None = None,
    ) -> None:
        """Initialize the trainer.

        Args:
            config: Training configuration.
            on_model_loaded: ``(model, processor) -> None``
                Called after model and processor are loaded but before
                training starts.  Use for custom setup like enabling
                gradient checkpointing on specific submodules or
                attaching hooks.
            on_before_collect: ``(task_id: str, env: WAADirect) -> None``
                Called before each rollout group collection.  Use for
                WAA health checks, tunnel verification, or task-specific
                setup.
            on_rollout_complete: ``(rollout: Rollout, index: int) -> None``
                Called after each individual rollout.  Use for capturing
                screenshots, thought traces, or per-rollout W&B logging.
            on_step_complete: ``(step: int, rollouts: list[Rollout], metrics: dict) -> None``
                Called after each training step with all rollouts and
                computed metrics (reward_mean, loss, etc.).  Use for
                W&B step logging, early stopping, or custom eval.
        """
        self._config = config
        self._model: Any = None
        self._processor: Any = None
        self._optimizer: Any = None
        self._env: WAADirect | None = None
        self._task_configs: dict[str, Any] = {}

        # Callback hooks (all optional, default None = no-op)
        self._on_model_loaded = on_model_loaded
        self._on_before_collect = on_before_collect
        self._on_rollout_complete = on_rollout_complete
        self._on_step_complete = on_step_complete

    # --- Constrained decoding -------------------------------------------

    # Regex matching the required Thought/Action format from SYSTEM_PROMPT.
    # The model reasons freely, then MUST output exactly one valid action.
    #
    # Format:  Thought: <reasoning>\nAction: <action>
    #
    # IMPORTANT: All repetitions use unbounded quantifiers (+, *) instead
    # of bounded ({1,N}).  Bounded quantifiers create counting DFA states
    # that explode combinatorially — {1,500} alone creates 1,500 states
    # cross-producted with every action alternative.  Unbounded repetitions
    # are single-state self-loops that Outlines handles efficiently.
    # max_new_tokens provides the actual length limit.
    _ACTION_RE = (
        r"CLICK\(x=0\.\d+,\s*y=0\.\d+\)"
        r'|TYPE\(text="[^"]*"\)'
        r"|WAIT\(\)"
        r"|DONE\(\)"
    )
    _ACTION_REGEX = (
        r"Thought: [^\n]+\nAction: (" + _ACTION_RE + r")"
    )
    # Cached outlines Generator (created once, reused for all generate calls)
    # None = not yet attempted, False = failed, Generator = success
    _outlines_generator: Any = None

    def _get_outlines_generator(self) -> Any | None:
        """Build an Outlines Generator for constrained generation.

        Outlines v1.2 uses its own Generator API — NOT model.generate()
        with a logits_processor kwarg.  The Generator wraps the model and
        handles tokenization, generation, and decoding internally.

        Returns the Generator, or None if creation fails.
        """
        if self._outlines_generator is False:
            return None
        if self._outlines_generator is not None:
            return self._outlines_generator

        try:
            import outlines

            wrapped_model = outlines.from_transformers(
                self._model, self._processor,
            )
            constraint = outlines.regex(self._ACTION_REGEX)
            generator = outlines.Generator(wrapped_model, constraint)

            self._outlines_generator = generator
            logger.info(
                "Outlines constrained decoding enabled "
                "(model=%s, regex compiled successfully)",
                type(wrapped_model).__name__,
            )
            return generator
        except ImportError:
            logger.error(
                "constrained_decoding=True but 'outlines' is not installed. "
                "Install with: uv sync --extra training"
            )
            self._outlines_generator = False
            return None
        except Exception as exc:
            logger.error(
                "Outlines Generator creation failed: %s. "
                "Falling back to unconstrained generation. "
                "Try: uv pip install -U outlines",
                exc,
            )
            self._outlines_generator = False
            return None

    # --- Task loading -----------------------------------------------------

    def _load_task_configs(self) -> None:
        """Load TaskConfig YAMLs from task_dir."""
        if not self._config.task_dir:
            return
        from openadapt_evals.task_config import TaskConfig
        task_dir = Path(self._config.task_dir)
        if not task_dir.exists():
            logger.warning("Task dir not found: %s", task_dir)
            return
        auto_populate = not self._config.task_ids
        for tc in TaskConfig.from_dir(str(task_dir)):
            self._task_configs[tc.id] = tc
            if auto_populate:
                self._config.task_ids.append(tc.id)
        logger.info("Loaded %d task configs from %s", len(self._task_configs), task_dir)

    def _collect_rollout(self, task_id: str, instruction: str) -> Rollout:
        """Collect one rollout: screenshot -> generate -> execute loop."""
        import torch
        assert self._env is not None
        rollout = Rollout(task_id=task_id, instruction=instruction)
        recent: list[bytes] = []

        for step_idx in range(self._config.max_steps_per_episode):
            # screenshot() already has built-in retry (3 attempts by default)
            try:
                screenshot = self._env.screenshot()
            except Exception as e:
                raise RolloutInfrastructureError(
                    f"Screenshot failed at step {step_idx} after retries"
                ) from e
            recent.append(screenshot)
            if self._env.is_stuck(recent, window=self._config.stuck_window):
                logger.info("Stuck at step %d", step_idx)
                break

            try:
                image = Image.open(io.BytesIO(screenshot))
            except (SyntaxError, OSError) as img_err:
                logger.warning(
                    "Corrupt screenshot at step %d, retrying: %s",
                    step_idx, img_err,
                )
                time.sleep(2)
                try:
                    screenshot = self._env.screenshot()
                    image = Image.open(io.BytesIO(screenshot))
                except Exception as exc:
                    raise RolloutInfrastructureError(
                        f"Screenshot remained unreadable at step {step_idx}"
                    ) from exc
            if image.mode != "RGB":
                image = image.convert("RGB")
                # .convert() drops .format; restore it for outlines.Image
                image.format = "PNG"
            messages = build_agent_messages(instruction, include_image=True)
            if hasattr(self._processor, "apply_chat_template"):
                text_input = self._processor.apply_chat_template(
                    messages, tokenize=False, add_generation_prompt=True)
            else:
                text_input = messages[-1]["content"]

            # --- Generation: constrained (Outlines) or unconstrained (HF) ---
            outlines_gen = (
                self._get_outlines_generator()
                if self._config.constrained_decoding
                else None
            )
            if outlines_gen is not None:
                # Outlines v1.2 Generator API for multimodal models.
                # TransformersMultiModal.format_input dispatches on type:
                #   list  → [prompt_text, Image(pil), ...]
                #   Chat  → Chat([Message(...)])
                # A dict is NOT accepted (raises TypeError).
                import outlines
                model_input = [text_input, outlines.Image(image)]
                decoded = outlines_gen(
                    model_input,
                    max_new_tokens=self._config.max_new_tokens,
                    temperature=self._config.temperature,
                )
                gen_len = len(self._processor.tokenizer.encode(
                    decoded, add_special_tokens=False,
                )) if decoded else 0
            else:
                # Standard HF generate (no constrained decoding)
                inputs = self._processor(
                    text=[text_input], images=[image], return_tensors="pt",
                )
                inputs = {k: v.to(self._model.device) for k, v in inputs.items()}
                with torch.no_grad():
                    outputs = self._model.generate(
                        **inputs,
                        max_new_tokens=self._config.max_new_tokens,
                        temperature=self._config.temperature,
                        do_sample=True,
                    )
                decoded = self._processor.decode(
                    outputs[0][inputs["input_ids"].shape[1]:],
                    skip_special_tokens=True,
                )
                gen_len = outputs[0].shape[0] - inputs["input_ids"].shape[1]
            action = parse_vlm_output_to_action(decoded, screen_size=self._config.screen_size)

            if gen_len >= self._config.max_new_tokens - 1:
                # Output hit the token ceiling. If we also failed to parse a
                # meaningful action, the truncation is the likely cause.
                if action.type == "done" and not re.search(r"\bDONE\s*\(\s*\)", decoded, re.IGNORECASE):
                    logger.warning(
                        "Output truncated at max_new_tokens=%d without "
                        "parseable action. Consider increasing max_new_tokens "
                        "(current GPU VRAM may limit this — see config.py "
                        "for recommendations).",
                        self._config.max_new_tokens,
                    )
                else:
                    logger.warning(
                        "Hit max_new_tokens=%d — output may be truncated "
                        "(action parsed OK this time).",
                        self._config.max_new_tokens,
                    )
            rollout.steps.append(RolloutStep(screenshot=screenshot, action=action, raw_text=decoded))
            if action.type == "done":
                break
            self._env.execute_action(action)
            time.sleep(0.5)

        # Fresh screenshot for evaluation
        tc = self._task_configs.get(task_id)
        if not tc:
            raise RolloutInfrastructureError(f"Task configuration {task_id!r} is missing")
        rollout.reward = evaluate_milestones_screenshot(
            tc, self._env.screenshot(), model=self._config.eval_model
        )
        return rollout

    def _collect_group(self, task_id: str) -> list[Rollout]:
        """Collect N rollouts for one GRPO gradient step."""
        assert self._env is not None

        if self._on_before_collect is not None:
            self._on_before_collect(task_id, self._env)

        # Pre-rollout health check: verify WAA is responsive before committing
        # to a full group of rollouts (avoids wasting time on a dead server).
        probe = self._env.probe()
        if not probe.get("screenshot_ok"):
            raise RolloutInfrastructureError(
                f"Pre-rollout health check failed for task {task_id}: {probe}"
            )

        tc = self._task_configs.get(task_id)
        instruction = getattr(tc, "name", "") or task_id if tc else task_id
        if tc and self._env:
            raw_config = getattr(tc, "raw_config", {})
            if raw_config:
                self._env.setup_task(raw_config)

        rollouts = []
        for i in range(self._config.num_rollouts_per_step):
            logger.info("Rollout %d/%d for %s", i + 1, self._config.num_rollouts_per_step, task_id)
            r = self._collect_rollout(task_id, instruction)
            rollouts.append(r)
            logger.info("Rollout %d: %d steps, reward=%.2f", i + 1, len(r.steps), r.reward)
            try:
                track_rollout_collected(task_id=task_id, num_steps=len(r.steps), reward=r.reward)
            except Exception:
                pass
            if self._on_rollout_complete is not None:
                self._on_rollout_complete(r, i)
        return rollouts

    def _compute_rollout_loss(self, rollout: Rollout, advantage: float, scale: float) -> float:
        """Compute GRPO loss for one rollout. Per-step backward to avoid OOM."""
        if isinstance(advantage, bool) or not isinstance(advantage, Real):
            raise RolloutLossError("Rollout advantage must be numeric")
        if not math.isfinite(float(advantage)):
            raise RolloutLossError("Rollout advantage must be finite")
        if isinstance(scale, bool) or not isinstance(scale, Real):
            raise RolloutLossError("Rollout loss scale must be numeric")
        if not math.isfinite(float(scale)) or float(scale) <= 0.0:
            raise RolloutLossError("Rollout loss scale must be finite and positive")
        if not rollout.steps:
            raise RolloutLossError("Cannot compute loss for an empty rollout")
        if any(not step.screenshot for step in rollout.steps):
            raise RolloutLossError("Every rollout step requires screenshot evidence")

        images = []
        for index, step in enumerate(rollout.steps):
            try:
                image = Image.open(io.BytesIO(step.screenshot))
                image.load()
            except Exception as exc:
                raise RolloutLossError(
                    f"Rollout step {index} has an unreadable screenshot"
                ) from exc
            images.append(image)

        import torch
        device = next(self._model.parameters()).device
        total, n = 0.0, len(rollout.steps)

        for index, (step, image) in enumerate(zip(rollout.steps, images)):
            if image.mode != "RGB":
                image = image.convert("RGB")
                image.format = "PNG"
            messages = build_agent_messages(rollout.instruction, include_image=True)
            action_text = step.raw_text or format_action_as_text(step.action, self._config.screen_size)

            if hasattr(self._processor, "apply_chat_template"):
                prompt_text = self._processor.apply_chat_template(
                    messages, tokenize=False, add_generation_prompt=True)
            else:
                prompt_text = messages[-1]["content"]

            # --- Vision-safe loss computation ---
            #
            # Process the FULL text (prompt + action) through the processor
            # as a single unit.  This ensures the model's vision merge
            # operates on consistent input.
            #
            # WHY: The old approach processed prompt alone, then manually
            # concatenated action_ids onto input_ids.  This created a
            # frankenstein input where pixel_values were sized for the
            # prompt but input_ids included action tokens.  Qwen3's vision
            # merge changed internal sequence length, causing attention
            # mask mismatches (crash on step 5 intermittently).
            #
            # NOW: processor(prompt + action, image) produces consistent
            # input_ids + pixel_values + attention_mask.  The model's
            # forward pass handles vision merge correctly.

            vision_loss_mode = getattr(self._config, "vision_loss_mode", "exclude")
            _VISION_KEYS = {"pixel_values", "pixel_values_videos",
                            "image_grid_thw", "video_grid_thw"}

            inner_tok = getattr(self._processor, "tokenizer", self._processor)
            action_ids = inner_tok(action_text, add_special_tokens=False, return_tensors="pt")["input_ids"]
            n_action = action_ids.shape[1]
            if n_action <= 0:
                raise RolloutLossError(f"Rollout step {index} has no action tokens")

            full_text = prompt_text + action_text
            full_inputs = self._processor(
                text=[full_text], images=[image], return_tensors="pt",
            )

            if vision_loss_mode == "exclude":
                excluded = _VISION_KEYS & set(full_inputs.keys())
                if excluded and not getattr(self, "_vision_exclude_warned", False):
                    logger.warning(
                        "vision_loss_mode='exclude': stripping vision tensors %s",
                        sorted(excluded),
                    )
                    self._vision_exclude_warned = True
                full_inputs = {k: v for k, v in full_inputs.items()
                               if k not in _VISION_KEYS}
            elif vision_loss_mode == "checkpoint":
                if not getattr(self, "_vision_checkpoint_warned", False):
                    logger.info("vision_loss_mode='checkpoint': gradient checkpointing on vision encoder.")
                    self._vision_checkpoint_warned = True
                    if hasattr(self._model, "visual") and hasattr(self._model.visual, "gradient_checkpointing_enable"):
                        self._model.visual.gradient_checkpointing_enable()
                    elif hasattr(self._model, "vision_tower"):
                        self._model.vision_tower.gradient_checkpointing_enable()
            # "include" mode: keep all tensors as-is

            full_inputs = {k: v.to(device) for k, v in full_inputs.items()}
            outputs = self._model(**full_inputs)
            if not bool(torch.isfinite(outputs.logits).all().item()):
                raise RolloutLossError(
                    f"Rollout step {index} produced non-finite model logits"
                )

            # Action logits are the last n_action positions in the output
            seq_len = outputs.logits.shape[1]
            al = outputs.logits[:, seq_len - n_action - 1: seq_len - 1, :]

            lp = torch.nn.functional.log_softmax(al, dim=-1)
            action_token_ids = action_ids.to(device)
            tlp = lp.gather(2, action_token_ids.unsqueeze(-1)).squeeze(-1)
            slp = tlp.sum()
            adv = torch.tensor(advantage, device=device, dtype=slp.dtype)
            loss = policy_gradient_loss(slp.unsqueeze(0), slp.detach().unsqueeze(0), adv.unsqueeze(0))
            scaled_loss = loss * scale / n
            if not bool(torch.isfinite(scaled_loss).all().item()):
                raise RolloutLossError(
                    f"Rollout step {index} produced a non-finite scaled loss"
                )
            scaled_loss.backward()
            loss_value = float(loss.detach().item())
            if not math.isfinite(loss_value):
                raise RolloutLossError(
                    f"Rollout step {index} produced a non-finite loss"
                )
            total += loss_value
        average_loss = total / n
        if not math.isfinite(average_loss):
            raise RolloutLossError("Rollout average loss must be finite")
        return average_loss

    def _training_step(self, rollouts: list[Rollout]) -> dict[str, float]:
        """Single GRPO gradient step."""
        if not rollouts:
            raise RolloutLossError("Cannot train from an empty rollout group")

        rewards = [r.reward for r in rollouts]
        advantages = compute_group_advantages(rewards)
        reward_mean = sum(float(reward) for reward in rewards) / len(rewards)
        if not math.isfinite(reward_mean):
            raise RolloutLossError("Mean reward must be finite")
        valid = [(r, a) for r, a in zip(rollouts, advantages) if abs(a) >= 1e-8]
        if not valid:
            return {"reward_mean": reward_mean, "loss": 0.0, "skipped": True}

        self._optimizer.zero_grad()
        n = len(valid)
        losses = []
        for r, a in valid:
            loss = self._compute_rollout_loss(r, a, 1.0 / n)
            if isinstance(loss, bool) or not isinstance(loss, Real):
                raise RolloutLossError("Rollout loss must be numeric")
            if not math.isfinite(float(loss)):
                raise RolloutLossError("Rollout loss must be finite")
            losses.append(loss)

        avg_loss = sum(losses) / n
        abs_loss = sum(abs(loss) for loss in losses) / n
        if not math.isfinite(avg_loss) or not math.isfinite(abs_loss):
            raise RolloutLossError("Training loss metrics must be finite")

        import torch

        trainable_parameters = [
            parameter
            for parameter in self._model.parameters()
            if parameter.requires_grad
        ]
        if not trainable_parameters:
            raise RolloutLossError("The model has no trainable parameters")
        gradient_parameters = [
            parameter for parameter in trainable_parameters if parameter.grad is not None
        ]
        if not gradient_parameters:
            raise RolloutLossError("Training produced no gradients")
        for parameter in gradient_parameters:
            if not bool(torch.isfinite(parameter.grad).all().item()):
                raise RolloutLossError("Training produced a non-finite gradient")
        max_grad_norm = self._config.max_grad_norm
        if (
            isinstance(max_grad_norm, bool)
            or not isinstance(max_grad_norm, Real)
            or not math.isfinite(float(max_grad_norm))
            or float(max_grad_norm) <= 0.0
        ):
            raise RolloutLossError("Maximum gradient norm must be finite and positive")
        try:
            grad_norm = torch.nn.utils.clip_grad_norm_(
                trainable_parameters,
                max_norm=max_grad_norm,
                error_if_nonfinite=True,
            )
        except Exception as exc:
            raise RolloutLossError(
                "Gradient norm could not be measured as finite"
            ) from exc
        gn = grad_norm.item() if hasattr(grad_norm, "item") else float(grad_norm)
        if not math.isfinite(gn):
            raise RolloutLossError("Gradient norm must be finite")
        if gn > 10 * self._config.max_grad_norm:
            logger.warning(
                "grad_norm=%.1f is %.0fx the clip threshold (%.1f). "
                "Gradients are dominated by clipping, not learning signal. "
                "Consider lowering learning_rate (current: %.1e).",
                gn, gn / max_grad_norm,
                max_grad_norm, self._config.learning_rate,
            )
        self._optimizer.step()

        return {
            "reward_mean": reward_mean,
            "loss": avg_loss,
            "loss_abs": abs_loss,
            "grad_norm": grad_norm.item() if hasattr(grad_norm, "item") else float(grad_norm),
            "advantages": [a for _, a in valid],
            "skipped": False,
            "num_rollouts": len(rollouts),
            "num_gradient_terms": n,
        }

    def _save_checkpoint(self, step: int) -> str:
        ckpt = Path(self._config.output_dir) / f"step_{step}"
        ckpt.mkdir(parents=True, exist_ok=True)
        self._model.save_pretrained(str(ckpt))
        logger.info("Saved checkpoint to %s", ckpt)
        return str(ckpt)

    def train(self) -> str:
        """Run GRPO training loop. Returns path to final checkpoint."""
        self._config.validate_for_training()

        import torch

        logger.info(
            "Using standalone GRPO trainer. This is the production training "
            "path for VLM agents with dynamic screenshots. TRL migration "
            "pending multimodal environment_factory support (TRL PR #5323)."
        )

        self._load_task_configs()
        if not self._config.task_ids:
            raise ValueError("No task IDs. Provide --task-dir with YAML configs or set task_ids.")

        logger.info("Starting standalone GRPO | model=%s tasks=%s steps=%d rollouts/step=%d max_tokens=%d",
                     self._config.model_name, self._config.task_ids,
                     self._config.num_training_steps, self._config.num_rollouts_per_step,
                     self._config.max_new_tokens)

        try:
            track_training_run(
                phase="start",
                model_name=self._config.model_name,
                num_steps=self._config.num_training_steps,
                num_rollouts_per_step=self._config.num_rollouts_per_step,
                task_count=len(self._config.task_ids),
                constrained_decoding=self._config.constrained_decoding,
                vision_loss_mode=getattr(self._config, "vision_loss_mode", None),
            )
        except Exception:
            pass

        self._model, self._processor = load_model_and_processor(
            self._config.model_name, load_in_4bit=self._config.load_in_4bit,
            lora_r=self._config.lora_r, lora_alpha=self._config.lora_alpha,
            lora_checkpoint=self._config.lora_checkpoint)

        if self._on_model_loaded is not None:
            self._on_model_loaded(self._model, self._processor)

        self._optimizer = torch.optim.AdamW(
            [p for p in self._model.parameters() if p.requires_grad], lr=self._config.learning_rate)
        self._env = WAADirect(server_url=self._config.server_url, screen_size=self._config.screen_size)
        if not self._env.health_check():
            raise ConnectionError(f"WAA server not reachable at {self._config.server_url}")

        Path(self._config.output_dir).mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        last_reward_mean: float | None = None
        optimizer_updates = 0
        for step in range(self._config.num_training_steps):
            ts = time.time()
            task_id = self._config.task_ids[step % len(self._config.task_ids)]
            self._model.eval()
            rollouts = self._collect_group(task_id)
            self._model.train()
            m = self._training_step(rollouts)
            if not m.get("skipped", False):
                optimizer_updates += 1
            m.update({"step": step, "task_id": task_id, "elapsed": time.time() - t0, "step_time": time.time() - ts})
            last_reward_mean = m.get("reward_mean")
            logger.info(
                "Step %d/%d: reward=%.2f loss=%+.2e |loss|=%.2e grad_norm=%.4f adv=%s time=%.1fs",
                step + 1, self._config.num_training_steps,
                m.get("reward_mean", 0),
                m.get("loss", 0),
                m.get("loss_abs", 0),
                m.get("grad_norm", 0),
                [f"{a:+.2f}" for a in m.get("advantages", [])],
                m["step_time"],
            )

            try:
                track_training_step(
                    step=step,
                    task_id=task_id,
                    reward_mean=m.get("reward_mean"),
                    loss=m.get("loss"),
                    step_time=m.get("step_time"),
                )
            except Exception:
                pass

            if self._on_step_complete is not None:
                self._on_step_complete(step, rollouts, m)

            if (
                optimizer_updates > 0
                and (step + 1) % self._config.save_every_steps == 0
            ):
                self._save_checkpoint(step + 1)
                try:
                    track_checkpoint_saved(step=step + 1)
                except Exception:
                    pass

        if optimizer_updates == 0:
            raise RolloutLossError(
                "Training completed no optimizer updates; refusing a trained checkpoint"
            )

        self._save_checkpoint(self._config.num_training_steps)
        try:
            track_checkpoint_saved(step=self._config.num_training_steps)
        except Exception:
            pass
        final = str(Path(self._config.output_dir) / f"step_{self._config.num_training_steps}")
        logger.info("Training complete. Final checkpoint: %s", final)

        try:
            track_training_run(
                phase="completed",
                model_name=self._config.model_name,
                num_steps=self._config.num_training_steps,
                duration_seconds=time.time() - t0,
                reward_mean=last_reward_mean,
            )
        except Exception:
            pass

        return final


def main() -> None:
    """CLI entry point."""
    p = argparse.ArgumentParser(description="Standalone GRPO trainer for WAA")
    p.add_argument("--task-dir", required=True, help="Directory of TaskConfig YAMLs")
    p.add_argument("--task-ids", nargs="+", default=None, help="Specific task IDs to train on (default: all from task-dir)")
    p.add_argument("--server-url", default="http://localhost:5001")
    p.add_argument("--model", default="Qwen/Qwen2.5-VL-7B-Instruct")
    p.add_argument("--lora-checkpoint", default=None)
    p.add_argument("--num-steps", type=int, default=10)
    p.add_argument("--num-rollouts", type=int, default=8)
    p.add_argument("--max-steps-per-episode", type=int, default=15)
    p.add_argument("--max-new-tokens", type=int, default=512)
    p.add_argument("--output", default="checkpoints/grpo")
    p.add_argument("--no-4bit", action="store_true")
    p.add_argument("--eval-model", default="gpt-4.1-mini")
    a = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    config = TrainingConfig(
        model_name=a.model, load_in_4bit=not a.no_4bit, lora_checkpoint=a.lora_checkpoint,
        server_url=a.server_url, task_dir=a.task_dir, num_training_steps=a.num_steps,
        num_rollouts_per_step=a.num_rollouts, max_steps_per_episode=a.max_steps_per_episode,
        max_new_tokens=a.max_new_tokens, output_dir=a.output, eval_model=a.eval_model)

    # Filter to specific tasks if requested
    if a.task_ids:
        config.task_ids = a.task_ids

    GRPOTrainer(config).train()


if __name__ == "__main__":
    main()

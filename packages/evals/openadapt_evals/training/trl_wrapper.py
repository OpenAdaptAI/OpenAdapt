"""TRL-backed GRPO trainer with clean config separation.

Our TrainingConfig handles OpenAdapt-specific concerns (WAA server,
task loading, constrained decoding, callbacks). TRL's GRPOConfig
handles training concerns (learning rate, loss type, batch size).
No duplication — each config owns its domain.

Usage:
    from trl import GRPOConfig
    from openadapt_evals.training.trl_wrapper import GRPOTrainer
    from openadapt_evals.training.standalone.config import TrainingConfig

    trainer = GRPOTrainer(
        TrainingConfig(
            task_dir="tasks/",
            server_url="http://localhost:5001",
            constrained_decoding=True,
        ),
        trl_config=GRPOConfig(
            output_dir="./checkpoints",
            loss_type="dapo",
            num_generations=4,
            learning_rate=5e-6,
            bf16=True,
        ),
        on_step_complete=my_logger,
    )
    trainer.train()
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class GRPOTrainer:
    """TRL-backed GRPO trainer.

    Args:
        config: Our TrainingConfig — WAA server, task_dir, model loading,
            constrained decoding. Handles everything OpenAdapt-specific.
        trl_config: TRL's GRPOConfig — learning rate, loss type, batch
            size, gradient accumulation, vLLM, W&B reporting. Passed
            directly to TRL with zero translation. Optional — sensible
            defaults are used if omitted.
        on_model_loaded: ``(model, processor) -> None``
        on_before_collect: ``(task_id, env) -> None``
        on_rollout_complete: ``(rollout, index) -> None``
        on_step_complete: ``(step, rollouts, metrics) -> None``
    """

    def __init__(
        self,
        config,
        *,
        trl_config=None,
        on_model_loaded=None,
        on_before_collect=None,
        on_rollout_complete=None,
        on_step_complete=None,
    ):
        self._config = config
        self._trl_config = trl_config
        self._on_model_loaded = on_model_loaded
        self._on_before_collect = on_before_collect
        self._on_rollout_complete = on_rollout_complete
        self._on_step_complete = on_step_complete

    def train(self) -> str:
        """Run GRPO training via TRL. Returns path to final checkpoint."""
        self._config.validate_for_training()

        from datasets import Dataset
        from trl import GRPOConfig
        from trl import GRPOTrainer as _TRLTrainer

        from openadapt_evals.task_config import TaskConfig
        from openadapt_evals.training.trl_rollout import make_waa_rollout_func

        # --- Tasks (from our config) ---
        task_configs = []
        if self._config.task_dir:
            task_configs = TaskConfig.from_dir(self._config.task_dir)

        # Filter by task_ids if specified — without this, ALL tasks from
        # task_dir end up in the TRL dataset regardless of what the user
        # requested. This was a critical bug: config had task_ids=["X"]
        # but TRL was running unrelated tasks.
        if getattr(self._config, "task_ids", None):
            allowed = set(self._config.task_ids)
            filtered = [tc for tc in task_configs if tc.id in allowed or tc.name in allowed]
            if filtered:
                task_configs = filtered
                logger.info(
                    "Filtered tasks by task_ids: %d/%d tasks selected",
                    len(filtered), len(task_configs) + len(filtered) - len(filtered),
                )
            else:
                raise ValueError(
                    "task_ids=%s matched no tasks from task_dir=%s. "
                    "Available: %s"
                    % (
                        self._config.task_ids,
                        self._config.task_dir,
                        [tc.id for tc in task_configs],
                    )
                )

        if not task_configs:
            raise ValueError("No tasks. Set task_dir in TrainingConfig.")

        dataset = Dataset.from_dict({
            "prompt": [tc.name for tc in task_configs],
            "task_id": [tc.id for tc in task_configs],
        })

        # --- Model (from our config) ---
        if getattr(self._config, "use_unsloth", False):
            try:
                from unsloth import FastVisionModel
            except ImportError:
                raise ImportError(
                    "use_unsloth=True but unsloth is not installed. "
                    "Install with: pip install openadapt-evals[unsloth]"
                ) from None
            logger.info("Loading with Unsloth: %s", self._config.model_name)
            model, processor = FastVisionModel.from_pretrained(
                self._config.model_name,
                load_in_4bit=self._config.load_in_4bit,
                fast_inference=True,
                gpu_memory_utilization=0.6,
            )
            model = FastVisionModel.get_peft_model(
                model, r=self._config.lora_r, lora_alpha=self._config.lora_alpha,
                target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                                "gate_proj", "up_proj", "down_proj"],
            )
        else:
            from openadapt_evals.training.standalone.model_loader import (
                load_model_and_processor,
            )
            model, processor = load_model_and_processor(
                self._config.model_name,
                load_in_4bit=self._config.load_in_4bit,
                lora_r=self._config.lora_r,
                lora_alpha=self._config.lora_alpha,
                lora_checkpoint=getattr(self._config, "lora_checkpoint", None),
            )

        if self._on_model_loaded:
            self._on_model_loaded(model, processor)

        # --- Patch model for TRL multimodal compatibility ---
        # TRL's GRPOTrainer calls model.forward(input_ids=...) and
        # model.generate(input_ids=...) without pixel_values. VLMs need
        # pixel_values. We patch the model's forward() directly on the
        # instance so it survives TRL/Accelerate unwrapping (which strips
        # wrapper classes). The cache_fn is passed to rollout_func.
        from openadapt_evals.training.vlm_wrapper import patch_model_for_trl
        cache_vision_fn = patch_model_for_trl(model)

        # --- Rollout function (from our config) ---
        from openadapt_evals.adapters.waa.live import WAALiveAdapter, WAALiveConfig
        adapter = WAALiveAdapter(WAALiveConfig(
            server_url=self._config.server_url,
            evaluate_url=getattr(self._config, "evaluate_url", None),
            # Training-appropriate timeouts: fail fast, don't block the
            # training loop. Benchmark defaults (180s, 3 retries) are for
            # one-shot evaluation where thoroughness matters. Training does
            # thousands of evaluations where speed matters.
            evaluate_timeout=15.0,
            evaluate_retries=1,
        ))
        rollout_func = make_waa_rollout_func(
            adapter=adapter,
            task_configs=task_configs,
            max_steps=self._config.max_steps_per_episode,
            constrained_decoding=getattr(self._config, "constrained_decoding", False),
            max_new_tokens=self._config.max_new_tokens,
            temperature=self._config.temperature,
            on_before_collect=self._on_before_collect,
            on_rollout_complete=self._on_rollout_complete,
            cache_vision_fn=cache_vision_fn,
        )

        # --- Reward ---
        def env_reward_fn(completions, **kwargs):
            return kwargs.get("env_reward", [0.0] * len(completions))

        # --- Callbacks ---
        callbacks = []

        try:
            from openadapt_evals.integrations.trl_callbacks import (
                DiagnosticsCallback,
                TelemetryCallback,
            )
            callbacks.append(TelemetryCallback())
            callbacks.append(DiagnosticsCallback())
        except ImportError:
            pass

        # on_before_collect and on_rollout_complete are passed directly to
        # make_waa_rollout_func (above) because TRL has no pre-rollout
        # callback. Only on_step_complete maps to TRL's on_step_end.
        if self._on_step_complete:
            try:
                from transformers import TrainerCallback

                class HookBridge(TrainerCallback):
                    def __init__(self, on_step_complete):
                        self._on_step_complete = on_step_complete

                    def on_step_end(self, args, state, control, **kwargs):
                        if self._on_step_complete:
                            self._on_step_complete(
                                state.global_step, [],
                                kwargs.get("metrics", {}),
                            )

                callbacks.append(HookBridge(self._on_step_complete))
            except ImportError:
                pass

        # --- Weave tracing ---
        weave_project = getattr(self._config, "weave_project", "")
        if weave_project:
            try:
                from openadapt_evals.integrations.weave_integration import weave_init
                weave_init(weave_project)
            except Exception:
                pass

        # --- TRL config: use provided or build sensible defaults ---
        # TRL constraints:
        #   - generation_batch_size must be divisible by num_generations
        #   - per_device_train_batch_size must be <= len(dataset)
        #
        # For RL with few tasks: set batch_size=1 (one unique prompt per
        # step) and generation_batch_size=num_generations (satisfies the
        # divisibility requirement). This produces exactly num_generations
        # rollouts per step — matching the standalone trainer.
        #
        # Previous approach (batch_size=num_gen, padded dataset) caused
        # 4× over-generation: 4 identical prompts × 4 generations = 16
        # rollouts when only 4 were needed.
        num_gen = self._config.num_rollouts_per_step

        if self._trl_config is not None:
            trl_config = self._trl_config
        else:
            trl_config = GRPOConfig(
                output_dir=self._config.output_dir,
                num_generations=num_gen,
                max_completion_length=self._config.max_new_tokens,
                max_steps=self._config.num_training_steps,
                learning_rate=self._config.learning_rate,
                save_steps=self._config.save_every_steps,
                logging_steps=1,
                bf16=True,
                loss_type="grpo",
                num_train_epochs=1,
                per_device_train_batch_size=1,
                # generation_batch_size must be divisible by num_generations.
                # Setting it to num_generations satisfies the constraint
                # while keeping batch_size=1 (one unique prompt per step).
                generation_batch_size=num_gen,
            )

        # No dataset padding needed: with batch_size=1, even a single-task
        # dataset works. TRL iterates one prompt per step, each getting
        # num_generations rollouts via rollout_func.

        # --- Train ---
        trainer = _TRLTrainer(
            model=model,
            processing_class=processor,
            args=trl_config,
            train_dataset=dataset,
            reward_funcs=[env_reward_fn],
            rollout_func=rollout_func,
            callbacks=callbacks,
        )

        logger.info(
            "Starting TRL GRPO: model=%s tasks=%d output=%s loss=%s",
            self._config.model_name, len(task_configs),
            trl_config.output_dir, trl_config.loss_type,
        )

        trainer.train()
        trainer.save_model(trl_config.output_dir)

        logger.info("Training complete. Checkpoint: %s", trl_config.output_dir)
        return trl_config.output_dir

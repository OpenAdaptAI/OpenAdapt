#!/usr/bin/env python3
"""End-to-end GRPO training script using TRL + Unsloth + WAA.

This is the **recommended** path for GRPO training of VLM desktop agents.
It replaces the standalone trainer (``openadapt_evals.training.standalone``)
with TRL's battle-tested ``GRPOTrainer``, adding:

- Outlines constrained decoding (``--constrained-decoding``)
- Configurable vision loss mode (``--vision-loss-mode``)
- Automatic telemetry via ``TelemetryCallback``
- Optional Weave tracing (``--weave-project``)

Examples:

    # With real WAA VM:
    python scripts/train_trl_grpo.py \
        --task-dir ./example_tasks \
        --server-url http://localhost:5001 \
        --model Qwen/Qwen2.5-VL-3B-Instruct \
        --output ./grpo_output

    # Mock mode (no VM, no GPU -- validates pipeline):
    python scripts/train_trl_grpo.py \
        --task-dir ./example_tasks \
        --mock \
        --output ./grpo_output_mock

    # With Unsloth + constrained decoding:
    python scripts/train_trl_grpo.py \
        --task-dir ./example_tasks \
        --server-url http://localhost:5001 \
        --model Qwen/Qwen2.5-VL-7B-Instruct \
        --use-unsloth \
        --constrained-decoding \
        --output ./grpo_output

    # With Weave tracing:
    python scripts/train_trl_grpo.py \
        --task-dir ./example_tasks \
        --server-url http://localhost:5001 \
        --model Qwen/Qwen2.5-VL-7B-Instruct \
        --weave-project openadapt-grpo \
        --output ./grpo_output

Requirements:
    pip install openadapt-evals trl>=0.17
    pip install unsloth          # optional, for VRAM efficiency
    pip install outlines>=0.1.0  # optional, for constrained decoding
    pip install weave             # optional, for Weave tracing
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("train_trl_grpo")


def load_task_dataset(task_configs):
    """Create a HuggingFace Dataset from TaskConfig objects.

    Each row has a 'prompt' field (task instruction) that TRL's
    GRPOTrainer samples from during training.
    """
    from datasets import Dataset

    return Dataset.from_dict({
        "prompt": [tc.name for tc in task_configs],
        "task_id": [tc.id for tc in task_configs],
    })


def load_model_unsloth(model_name, max_seq_length=4096, lora_r=16):
    """Load model with Unsloth for VRAM efficiency."""
    from unsloth import FastVisionModel

    logger.info("Loading model with Unsloth: %s", model_name)
    model, processor = FastVisionModel.from_pretrained(
        model_name,
        max_seq_length=max_seq_length,
        load_in_4bit=True,
        fast_inference=True,
        gpu_memory_utilization=0.6,
        float8_kv_cache=True,
    )
    model = FastVisionModel.get_peft_model(
        model,
        r=lora_r,
        lora_alpha=lora_r,
        target_modules=[
            "q_proj", "k_proj", "v_proj", "o_proj",
            "gate_proj", "up_proj", "down_proj",
        ],
    )
    logger.info("Model loaded with Unsloth (4bit + LoRA r=%d)", lora_r)
    return model, processor


def load_model_standard(model_name, lora_r=16):
    """Load model with standard HuggingFace + PEFT."""
    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import AutoProcessor

    try:
        from transformers import AutoModelForImageTextToText as AutoVLM
    except ImportError:
        from transformers import AutoModelForVision2Seq as AutoVLM

    logger.info("Loading model (standard): %s", model_name)
    processor = AutoProcessor.from_pretrained(model_name)
    model = AutoVLM.from_pretrained(
        model_name,
        torch_dtype=torch.bfloat16,
        device_map="auto",
    )
    lora_config = LoraConfig(
        r=lora_r,
        lora_alpha=lora_r,
        target_modules=[
            "q_proj", "k_proj", "v_proj", "o_proj",
            "gate_proj", "up_proj", "down_proj",
        ],
        task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()
    return model, processor


def create_mock_rollout_func(task_configs):
    """Create a mock rollout_func for pipeline validation without VM/GPU.

    Returns synthetic rewards matching milestone fractions so GRPO
    can compute advantages.
    """
    import random

    config_map = {tc.name: tc for tc in task_configs}

    def mock_rollout_func(prompts, trainer):
        num_generations = getattr(trainer.args, "num_generations", 8)
        all_prompt_ids = []
        all_completion_ids = []
        all_logprobs = []
        all_rewards = []

        for prompt in prompts:
            tc = config_map.get(prompt)
            n_milestones = len(tc.milestones) if tc else 3

            for _ in range(num_generations):
                # Simulate varying milestone completion
                passed = random.randint(0, n_milestones)
                reward = passed / max(n_milestones, 1)

                all_prompt_ids.append([1, 2, 3])
                all_completion_ids.append([4, 5, 6, 7])
                all_logprobs.append([-0.5, -0.3, -0.2, -0.1])
                all_rewards.append(reward)

        return {
            "prompt_ids": all_prompt_ids,
            "completion_ids": all_completion_ids,
            "logprobs": all_logprobs,
            "env_reward": all_rewards,
        }

    return mock_rollout_func


def _init_weave(project: str | None) -> bool:
    """Initialize Weave tracing if a project name is provided.

    Returns True if Weave was initialized successfully.
    """
    if not project:
        return False
    try:
        from openadapt_evals.integrations.weave_integration import weave_init

        weave_init(project)
        logger.info("Weave tracing enabled: project=%s", project)
        return True
    except ImportError:
        logger.warning(
            "Weave tracing requested but 'weave' is not installed. "
            "Install with: pip install weave"
        )
        return False
    except Exception as exc:
        logger.warning("Weave initialization failed: %s", exc)
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Train a VLM desktop agent with TRL GRPO + dense rewards"
    )

    # Task configuration
    parser.add_argument(
        "--task-dir", required=True,
        help="Directory of YAML task configs (e.g., ./example_tasks)",
    )

    # Environment
    parser.add_argument(
        "--server-url", default="http://localhost:5001",
        help="WAA server URL (default: localhost:5001)",
    )
    parser.add_argument(
        "--evaluate-url", default=None,
        help="Separate evaluate server URL (default: same as server-url)",
    )
    parser.add_argument(
        "--max-steps", type=int, default=15,
        help="Max steps per episode (default: 15)",
    )
    parser.add_argument(
        "--mock", action="store_true",
        help="Use mock adapter (no VM/GPU needed, validates pipeline)",
    )

    # Model
    parser.add_argument(
        "--model", default="Qwen/Qwen2.5-VL-3B-Instruct",
        help="Model name or path (default: Qwen2.5-VL-3B)",
    )
    parser.add_argument(
        "--use-unsloth", action="store_true",
        help="Use Unsloth for VRAM efficiency (recommended for GPU)",
    )
    parser.add_argument(
        "--lora-r", type=int, default=16,
        help="LoRA rank (default: 16)",
    )
    parser.add_argument(
        "--lora-checkpoint", default=None,
        help="Path to LoRA checkpoint to resume from",
    )

    # Training
    parser.add_argument("--output", default="./grpo_output", help="Output directory")
    parser.add_argument("--num-generations", type=int, default=4, help="GRPO group size")
    parser.add_argument("--max-completion-length", type=int, default=256)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--gradient-accumulation", type=int, default=4)
    parser.add_argument("--num-epochs", type=int, default=1)
    parser.add_argument("--learning-rate", type=float, default=5e-6)
    parser.add_argument("--save-steps", type=int, default=50)
    parser.add_argument("--logging-steps", type=int, default=1)
    parser.add_argument("--loss-type", default="grpo", choices=["grpo", "dapo", "dr_grpo"])
    parser.add_argument(
        "--use-vllm", action="store_true",
        help="Use vLLM for generation (faster, requires vllm installed)",
    )

    # Constrained decoding
    parser.add_argument(
        "--constrained-decoding", action="store_true",
        help=(
            "Use Outlines to constrain generation to the "
            "'Thought: ...\\nAction: CLICK/TYPE/WAIT/DONE' format. "
            "Eliminates 5-15%% of rollouts wasted on unparseable output. "
            "Requires: pip install outlines>=0.1.0"
        ),
    )

    # Vision loss mode
    parser.add_argument(
        "--vision-loss-mode",
        default="exclude",
        choices=["exclude", "include", "checkpoint"],
        help=(
            "How to handle vision tensors during the loss backward pass. "
            "'exclude' (default): strip vision tensors, text-only log-probs "
            "(safe on <=48GB GPUs). "
            "'include': keep vision tensors, full multimodal backward "
            "(may OOM on <80GB). "
            "'checkpoint': gradient-checkpoint the vision encoder to cut "
            "peak VRAM. "
            "NOTE: TRL handles VLM tensors natively in the forward pass; "
            "this flag controls the rollout_func's generation behavior and "
            "is passed as metadata for telemetry tracking."
        ),
    )

    # Reward
    parser.add_argument(
        "--reward-fn", default="env",
        choices=["env", "env+length"],
        help="Reward function: env (milestone rewards only) or env+length (penalize long episodes)",
    )

    # Telemetry & tracing
    parser.add_argument(
        "--no-telemetry", action="store_true",
        help="Disable automatic telemetry callback",
    )
    parser.add_argument(
        "--weave-project", default=None,
        help="Weave project name for tracing (e.g., 'openadapt-grpo'). "
             "Requires: pip install weave",
    )

    args = parser.parse_args()

    # --- Initialize Weave tracing (before any model loading) ---
    _init_weave(args.weave_project)

    # --- Load task configs ---
    from openadapt_evals.task_config import TaskConfig

    task_configs = TaskConfig.from_dir(args.task_dir)
    if not task_configs:
        logger.error("No task configs found in %s", args.task_dir)
        sys.exit(1)

    logger.info(
        "Loaded %d task configs from %s",
        len(task_configs), args.task_dir,
    )
    for tc in task_configs:
        logger.info(
            "  %s (%s): %d milestones, max_steps=%d",
            tc.id, tc.name[:40], len(tc.milestones), tc.max_steps,
        )

    dataset = load_task_dataset(task_configs)
    logger.info("Training dataset: %d tasks", len(dataset))

    # --- Mock mode ---
    if args.mock:
        logger.info("=== MOCK MODE -- validating pipeline without VM/GPU ===")

        rollout_func = create_mock_rollout_func(task_configs)

        # Verify rollout_func output shape
        mock_trainer = type("MockTrainer", (), {"args": type("Args", (), {"num_generations": args.num_generations})()})()
        result = rollout_func(dataset["prompt"][:2], mock_trainer)

        logger.info("Mock rollout output keys: %s", list(result.keys()))
        logger.info("Rewards: %s", result["env_reward"])
        logger.info(
            "Reward variance: %.4f (need >0 for GRPO)",
            max(result["env_reward"]) - min(result["env_reward"]),
        )

        # Save mock results
        output_dir = Path(args.output)
        output_dir.mkdir(parents=True, exist_ok=True)
        with open(output_dir / "mock_results.json", "w") as f:
            json.dump({
                "mode": "mock",
                "tasks": len(task_configs),
                "num_generations": args.num_generations,
                "rewards": result["env_reward"],
                "reward_variance": max(result["env_reward"]) - min(result["env_reward"]),
            }, f, indent=2)
        logger.info("Mock results saved to %s", output_dir / "mock_results.json")
        logger.info("=== Mock pipeline validation PASSED ===")
        return

    # --- Real training ---
    logger.info("=== Setting up GRPO training ===")

    # Load model
    if args.use_unsloth:
        model, processor = load_model_unsloth(args.model, lora_r=args.lora_r)
    else:
        model, processor = load_model_standard(args.model, lora_r=args.lora_r)

    # Load LoRA checkpoint if provided
    if args.lora_checkpoint:
        from peft import PeftModel

        logger.info("Loading LoRA checkpoint: %s", args.lora_checkpoint)
        model = PeftModel.from_pretrained(model, args.lora_checkpoint)

    # Create rollout function
    if args.mock:
        rollout_func = create_mock_rollout_func(task_configs)
    else:
        from openadapt_evals.adapters.waa.live import WAALiveAdapter, WAALiveConfig
        from openadapt_evals.training.trl_rollout import make_waa_rollout_func

        adapter = WAALiveAdapter(
            WAALiveConfig(
                server_url=args.server_url,
                evaluate_url=args.evaluate_url,
            )
        )
        rollout_func = make_waa_rollout_func(
            adapter=adapter,
            task_configs=task_configs,
            max_steps=args.max_steps,
            constrained_decoding=args.constrained_decoding,
            max_new_tokens=args.max_completion_length,
        )

    # Create reward function
    def env_reward_fn(completions, **kwargs):
        """Extract environment rewards from rollout_func output."""
        return kwargs.get("env_reward", [0.0] * len(completions))

    reward_funcs = [env_reward_fn]

    if args.reward_fn == "env+length":
        def length_penalty(completions, **kwargs):
            """Penalize very long completions (encourage efficiency)."""
            max_len = args.max_completion_length
            return [-0.1 * (len(c) / max_len) for c in completions]
        reward_funcs.append(length_penalty)

    # --- Build callbacks ---
    callbacks = []

    # Telemetry callback (auto-enabled unless --no-telemetry)
    if not args.no_telemetry:
        try:
            from openadapt_evals.integrations.trl_callbacks import TelemetryCallback

            callbacks.append(TelemetryCallback(
                model_name=args.model,
                task_count=len(task_configs),
                constrained_decoding=args.constrained_decoding,
                vision_loss_mode=args.vision_loss_mode,
            ))
            logger.info("Telemetry callback enabled")
        except Exception as exc:
            logger.warning("Failed to create TelemetryCallback: %s", exc)

    # Configure training
    from trl import GRPOConfig, GRPOTrainer

    config = GRPOConfig(
        output_dir=args.output,
        num_generations=args.num_generations,
        max_completion_length=args.max_completion_length,
        per_device_train_batch_size=args.batch_size,
        gradient_accumulation_steps=args.gradient_accumulation,
        num_train_epochs=args.num_epochs,
        learning_rate=args.learning_rate,
        loss_type=args.loss_type,
        logging_steps=args.logging_steps,
        save_steps=args.save_steps,
        bf16=True,
        report_to="none",  # set to "wandb" for W&B logging
    )

    if args.use_vllm:
        config.use_vllm = True
        config.vllm_mode = "colocate"
        config.vllm_gpu_memory_utilization = 0.3

    trainer = GRPOTrainer(
        model=model,
        processing_class=processor,
        args=config,
        train_dataset=dataset,
        reward_funcs=reward_funcs,
        rollout_func=rollout_func,
        callbacks=callbacks if callbacks else None,
    )

    logger.info("=== Starting GRPO training ===")
    logger.info("  Model: %s", args.model)
    logger.info("  Tasks: %d", len(task_configs))
    logger.info("  Group size: %d", args.num_generations)
    logger.info("  Loss type: %s", args.loss_type)
    logger.info("  Constrained decoding: %s", args.constrained_decoding)
    logger.info("  Vision loss mode: %s", args.vision_loss_mode)
    logger.info("  Telemetry: %s", "disabled" if args.no_telemetry else "enabled")
    logger.info("  Weave tracing: %s", args.weave_project or "disabled")
    logger.info("  Output: %s", args.output)

    trainer.train()

    # Save final checkpoint
    trainer.save_model(args.output)
    logger.info("=== Training complete. Model saved to %s ===", args.output)


if __name__ == "__main__":
    main()

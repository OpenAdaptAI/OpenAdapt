#!/usr/bin/env python3
"""Fine-tune a student model on distilled trajectories from a frontier teacher.

Loads SFT training data produced by ``collect_distillation_data.py`` (JSONL +
screenshot PNGs) and fine-tunes a vision-language model with LoRA using TRL's
SFTTrainer (or Unsloth if available).

This is Phase 1 of the distillation pipeline described in
``private/research_superhuman_desktop_agent_2026_03_20.md`` Section 8.3.
After SFT, the resulting model can be further improved with GRPO RL training
via ``train_trl_grpo.py``.

Usage:
    # Fine-tune Qwen3.5-9B on collected trajectories
    python scripts/finetune_distilled.py \\
        --data-dir distillation_data/ \\
        --output-dir checkpoints/qwen35_distilled

    # Use a different base model
    python scripts/finetune_distilled.py \\
        --base-model Qwen/Qwen3-VL-7B \\
        --data-dir distillation_data/ \\
        --output-dir checkpoints/qwen3vl_distilled

    # Mock mode (validate pipeline without GPU)
    python scripts/finetune_distilled.py \\
        --data-dir distillation_data/ \\
        --mock

    # Custom LoRA params
    python scripts/finetune_distilled.py \\
        --data-dir distillation_data/ \\
        --lora-r 32 \\
        --lora-alpha 64 \\
        --epochs 5 \\
        --batch-size 2

Prerequisites:
    - Distillation data collected via ``collect_distillation_data.py``
    - GPU with sufficient VRAM (A10G 24GB for 9B model with LoRA)
    - ``pip install trl peft transformers accelerate bitsandbytes``
    - Optional: ``pip install unsloth`` for 2x faster training
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("finetune_distilled")


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_trajectories(data_dir: Path) -> list[dict[str, Any]]:
    """Load trajectory data from JSONL file.

    Reads the JSONL file produced by ``collect_distillation_data.py``
    and groups records by episode_id. Only returns episodes that have
    ``episode_reward > 0`` (successful trajectories).

    Args:
        data_dir: Directory containing ``trajectories.jsonl`` and
            episode screenshot subdirectories.

    Returns:
        List of episode dicts, each containing a list of steps.
    """
    jsonl_path = data_dir / "trajectories.jsonl"
    if not jsonl_path.exists():
        raise FileNotFoundError(f"No trajectories.jsonl found in {data_dir}")

    # Group records by episode
    episodes: dict[str, list[dict]] = {}
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Skipping invalid JSON at line %d", line_num)
                continue

            ep_id = record.get("episode_id")
            if not ep_id:
                continue

            if ep_id not in episodes:
                episodes[ep_id] = []
            episodes[ep_id].append(record)

    # Filter to successful episodes only
    successful = []
    for ep_id, steps in episodes.items():
        # Check if any step has episode_reward > 0
        rewards = [s.get("episode_reward", 0) for s in steps if "episode_reward" in s]
        if rewards and max(rewards) > 0:
            # Sort steps by step_index
            steps.sort(key=lambda s: s.get("step_index", 0))
            successful.append(
                {
                    "episode_id": ep_id,
                    "task_instruction": steps[0].get("task_instruction", ""),
                    "episode_reward": max(rewards),
                    "steps": steps,
                }
            )

    logger.info(
        "Loaded %d successful episodes (%d total steps) from %d total episodes",
        len(successful),
        sum(len(ep["steps"]) for ep in successful),
        len(episodes),
    )
    return successful


def format_training_example(
    step: dict[str, Any],
    task_instruction: str,
    data_dir: Path,
    include_image: bool = True,
) -> dict[str, Any]:
    """Format a single step as a chat/instruction training example.

    Produces a conversation in the standard chat format that VLM
    fine-tuning frameworks (TRL, Unsloth) expect:

    - System message: Desktop agent instructions
    - User message: Task + screenshot + action history
    - Assistant message: The teacher's action (target output)

    Args:
        step: Step record from trajectories.jsonl.
        task_instruction: Task instruction string.
        data_dir: Base directory for resolving screenshot paths.
        include_image: Whether to include image paths in the output.

    Returns:
        Dict with 'messages' key in chat format, plus optional 'images'.
    """
    system_msg = (
        "You are an expert desktop automation agent. Given a screenshot and "
        "task instruction, output the next action as a JSON object with "
        "fields: decision, action_type, x, y, text, target_description, reasoning."
    )

    # Build action history text
    action_history = step.get("action_history", [])
    history_text = "\n".join(
        f"  {i + 1}. {a}" for i, a in enumerate(action_history[-10:])
    )
    if not history_text:
        history_text = "  (none yet)"

    user_text = (
        f"Task: {task_instruction}\n\n"
        f"Previous actions:\n{history_text}\n\n"
        "Look at the screenshot and output the next action as JSON."
    )

    # Build the assistant response (teacher output)
    planner_output = step.get("planner_output", {})
    assistant_text = json.dumps(planner_output, ensure_ascii=False)

    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_text},
        {"role": "assistant", "content": assistant_text},
    ]

    result: dict[str, Any] = {"messages": messages}

    # Resolve screenshot path
    if include_image:
        screenshot_rel = step.get("screenshot_path")
        if screenshot_rel:
            screenshot_abs = data_dir / screenshot_rel
            if screenshot_abs.exists():
                result["images"] = [str(screenshot_abs)]
            else:
                logger.debug("Screenshot not found: %s", screenshot_abs)

    return result


# ---------------------------------------------------------------------------
# Mock mode: validate pipeline without GPU
# ---------------------------------------------------------------------------


def run_mock_validation(
    data_dir: Path,
    base_model: str,
    lora_r: int,
    lora_alpha: int,
    epochs: int,
    batch_size: int,
) -> int:
    """Validate the pipeline structure without loading models or GPU.

    Checks:
    1. Data directory exists and contains valid trajectories
    2. Training examples can be formatted correctly
    3. Configuration parameters are valid
    4. Required packages are importable

    Returns 0 on success, 1 on failure.
    """
    print("=" * 70)
    print("MOCK VALIDATION MODE")
    print("=" * 70)

    errors = []

    # 1. Load data
    print(f"\n[1/5] Loading trajectories from {data_dir}...")
    try:
        episodes = load_trajectories(data_dir)
        if not episodes:
            errors.append("No successful episodes found in data directory")
        else:
            total_steps = sum(len(ep["steps"]) for ep in episodes)
            print(f"  Found {len(episodes)} episodes, {total_steps} steps")
    except FileNotFoundError as e:
        errors.append(str(e))
        episodes = []

    # 2. Format training examples
    print("\n[2/5] Formatting training examples...")
    if episodes:
        sample_ep = episodes[0]
        sample_step = sample_ep["steps"][0]
        example = format_training_example(
            sample_step,
            sample_ep["task_instruction"],
            data_dir,
            include_image=True,
        )
        print(f"  Sample messages: {len(example['messages'])} turns")
        print(f"  System: {example['messages'][0]['content'][:80]}...")
        print(f"  User: {example['messages'][1]['content'][:80]}...")
        print(f"  Assistant: {example['messages'][2]['content'][:80]}...")
        if "images" in example:
            print(f"  Images: {example['images']}")
        else:
            print("  Images: (none found)")

    # 3. Validate config
    print("\n[3/5] Validating configuration...")
    print(f"  Base model:  {base_model}")
    print(f"  LoRA rank:   {lora_r}")
    print(f"  LoRA alpha:  {lora_alpha}")
    print(f"  Epochs:      {epochs}")
    print(f"  Batch size:  {batch_size}")
    if lora_alpha < lora_r:
        errors.append(
            f"LoRA alpha ({lora_alpha}) should typically be >= LoRA rank ({lora_r})"
        )

    # 4. Check imports
    print("\n[4/5] Checking required packages...")
    packages = {
        "transformers": "transformers",
        "peft": "peft",
        "trl": "trl",
        "torch": "torch",
        "PIL": "Pillow",
        "datasets": "datasets",
    }
    optional_packages = {
        "unsloth": "unsloth (optional, 2x faster training)",
        "bitsandbytes": "bitsandbytes (optional, for 4-bit quantization)",
    }

    for module, name in packages.items():
        try:
            __import__(module)
            print(f"  [OK]  {name}")
        except ImportError:
            print(f"  [!!]  {name} -- NOT INSTALLED")
            errors.append(f"Required package not installed: {name}")

    for module, name in optional_packages.items():
        try:
            __import__(module)
            print(f"  [OK]  {name}")
        except ImportError:
            print(f"  [--]  {name} -- not installed (optional)")

    # 5. Estimate training requirements
    print("\n[5/5] Estimating training requirements...")
    if episodes:
        total_steps = sum(len(ep["steps"]) for ep in episodes)
        # Rough estimates
        est_tokens = total_steps * 1000  # ~1K tokens per example
        est_time_per_epoch_a10g = total_steps * 2.5  # ~2.5s per step on A10G
        est_vram_gb = {
            "9B + LoRA (4-bit)": 12,
            "9B + LoRA (bf16)": 22,
            "32B + LoRA (4-bit)": 24,
            "32B + LoRA (bf16)": 48,
        }
        print(f"  Training examples:    {total_steps}")
        print(f"  Est. tokens:          {est_tokens:,}")
        print(f"  Est. time/epoch (A10G): {est_time_per_epoch_a10g / 60:.1f} min")
        print(f"  Est. total time:      {est_time_per_epoch_a10g * epochs / 60:.1f} min")
        print("  VRAM estimates:")
        for config, vram in est_vram_gb.items():
            print(f"    {config}: ~{vram}GB")

    # Summary
    print("\n" + "=" * 70)
    if errors:
        print("VALIDATION FAILED:")
        for e in errors:
            print(f"  - {e}")
        return 1
    else:
        print("VALIDATION PASSED -- pipeline is ready for GPU training")
        print("\nTo train, run without --mock:")
        print(
            f"  python scripts/finetune_distilled.py "
            f"--data-dir {data_dir} --base-model {base_model}"
        )
        return 0


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------


def build_dataset(
    episodes: list[dict[str, Any]],
    data_dir: Path,
) -> Any:
    """Build a HuggingFace Dataset from trajectory episodes.

    Each step in each episode becomes a training example in chat format.

    Args:
        episodes: List of episode dicts from ``load_trajectories()``.
        data_dir: Base directory for resolving screenshot paths.

    Returns:
        A ``datasets.Dataset`` with 'messages' and optionally 'images' columns.
    """
    from datasets import Dataset

    examples: list[dict[str, Any]] = []

    for episode in episodes:
        task_instruction = episode["task_instruction"]
        for step in episode["steps"]:
            example = format_training_example(
                step, task_instruction, data_dir, include_image=True
            )
            examples.append(example)

    if not examples:
        raise ValueError("No training examples produced from episodes")

    # Separate messages and images for dataset
    records = []
    for ex in examples:
        record: dict[str, Any] = {"messages": ex["messages"]}
        if "images" in ex and ex["images"]:
            record["images"] = ex["images"]
        else:
            record["images"] = []
        records.append(record)

    dataset = Dataset.from_list(records)
    logger.info("Built dataset with %d training examples", len(dataset))
    return dataset


def train(
    base_model: str,
    data_dir: Path,
    output_dir: Path,
    lora_r: int,
    lora_alpha: int,
    epochs: int,
    batch_size: int,
    learning_rate: float,
    max_seq_length: int,
    use_4bit: bool,
    gradient_accumulation_steps: int,
) -> None:
    """Run LoRA fine-tuning with TRL SFTTrainer.

    Attempts to use Unsloth for 2x speedup if available, otherwise
    falls back to standard TRL + PEFT.

    Args:
        base_model: HuggingFace model ID.
        data_dir: Directory containing distillation data.
        output_dir: Directory for saving checkpoints.
        lora_r: LoRA rank.
        lora_alpha: LoRA alpha scaling.
        epochs: Number of training epochs.
        batch_size: Per-device training batch size.
        learning_rate: Learning rate.
        max_seq_length: Maximum sequence length.
        use_4bit: Whether to use 4-bit quantization.
        gradient_accumulation_steps: Gradient accumulation steps.
    """

    # Load data
    episodes = load_trajectories(data_dir)
    if not episodes:
        logger.error("No successful episodes found. Run collect_distillation_data.py first.")
        sys.exit(1)

    dataset = build_dataset(episodes, data_dir)

    # Check for Unsloth
    use_unsloth = False
    try:
        from unsloth import FastVisionModel

        # The import is the availability probe; `_train_unsloth` imports its
        # own reference. Read the symbol here so the probe is not dead code.
        use_unsloth = FastVisionModel is not None
        logger.info("Unsloth detected -- using FastVisionModel for 2x speedup")
    except ImportError:
        logger.info("Unsloth not available -- using standard TRL + PEFT")

    if use_unsloth:
        _train_unsloth(
            base_model=base_model,
            dataset=dataset,
            output_dir=output_dir,
            lora_r=lora_r,
            lora_alpha=lora_alpha,
            epochs=epochs,
            batch_size=batch_size,
            learning_rate=learning_rate,
            max_seq_length=max_seq_length,
            use_4bit=use_4bit,
            gradient_accumulation_steps=gradient_accumulation_steps,
        )
    else:
        _train_standard(
            base_model=base_model,
            dataset=dataset,
            output_dir=output_dir,
            lora_r=lora_r,
            lora_alpha=lora_alpha,
            epochs=epochs,
            batch_size=batch_size,
            learning_rate=learning_rate,
            max_seq_length=max_seq_length,
            use_4bit=use_4bit,
            gradient_accumulation_steps=gradient_accumulation_steps,
        )


def _train_unsloth(
    base_model: str,
    dataset: Any,
    output_dir: Path,
    lora_r: int,
    lora_alpha: int,
    epochs: int,
    batch_size: int,
    learning_rate: float,
    max_seq_length: int,
    use_4bit: bool,
    gradient_accumulation_steps: int,
) -> None:
    """Train using Unsloth's FastVisionModel for optimized LoRA."""
    from trl import SFTConfig, SFTTrainer
    from unsloth import FastVisionModel

    logger.info("Loading base model with Unsloth: %s", base_model)

    model, tokenizer = FastVisionModel.from_pretrained(
        base_model,
        load_in_4bit=use_4bit,
        max_seq_length=max_seq_length,
    )

    model = FastVisionModel.get_peft_model(
        model,
        r=lora_r,
        lora_alpha=lora_alpha,
        lora_dropout=0.05,
        target_modules=[
            "q_proj", "k_proj", "v_proj", "o_proj",
            "gate_proj", "up_proj", "down_proj",
        ],
        bias="none",
        use_gradient_checkpointing="unsloth",
    )

    training_args = SFTConfig(
        output_dir=str(output_dir),
        num_train_epochs=epochs,
        per_device_train_batch_size=batch_size,
        gradient_accumulation_steps=gradient_accumulation_steps,
        learning_rate=learning_rate,
        lr_scheduler_type="cosine",
        warmup_ratio=0.1,
        logging_steps=10,
        save_steps=100,
        save_total_limit=3,
        bf16=True,
        max_seq_length=max_seq_length,
        dataset_text_field="",  # We use the formatting function
        dataset_kwargs={"skip_prepare_dataset": True},
        report_to="none",
        seed=42,
    )

    trainer = SFTTrainer(
        model=model,
        args=training_args,
        train_dataset=dataset,
        processing_class=tokenizer,
    )

    logger.info("Starting Unsloth training for %d epochs...", epochs)
    train_result = trainer.train()

    # Save final checkpoint
    logger.info("Saving final model to %s", output_dir)
    model.save_pretrained(str(output_dir / "final"))
    tokenizer.save_pretrained(str(output_dir / "final"))

    # Save training metrics
    metrics = train_result.metrics
    metrics_path = output_dir / "training_metrics.json"
    with open(metrics_path, "w") as f:
        json.dump(metrics, f, indent=2, default=str)

    logger.info("Training complete. Metrics: %s", metrics)


def _train_standard(
    base_model: str,
    dataset: Any,
    output_dir: Path,
    lora_r: int,
    lora_alpha: int,
    epochs: int,
    batch_size: int,
    learning_rate: float,
    max_seq_length: int,
    use_4bit: bool,
    gradient_accumulation_steps: int,
) -> None:
    """Train using standard TRL SFTTrainer + PEFT LoRA."""
    import torch
    from peft import LoraConfig, TaskType, get_peft_model
    from transformers import AutoModelForCausalLM, AutoProcessor, BitsAndBytesConfig
    from trl import SFTConfig, SFTTrainer

    logger.info("Loading base model: %s", base_model)

    # Quantization config
    quantization_config = None
    if use_4bit:
        quantization_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=True,
        )

    model = AutoModelForCausalLM.from_pretrained(
        base_model,
        quantization_config=quantization_config,
        torch_dtype=torch.bfloat16,
        device_map="auto",
        trust_remote_code=True,
    )

    processor = AutoProcessor.from_pretrained(
        base_model,
        trust_remote_code=True,
    )

    # Ensure pad token
    if processor.tokenizer.pad_token is None:
        processor.tokenizer.pad_token = processor.tokenizer.eos_token
        model.config.pad_token_id = model.config.eos_token_id

    # LoRA config
    lora_config = LoraConfig(
        r=lora_r,
        lora_alpha=lora_alpha,
        lora_dropout=0.05,
        target_modules=[
            "q_proj", "k_proj", "v_proj", "o_proj",
            "gate_proj", "up_proj", "down_proj",
        ],
        bias="none",
        task_type=TaskType.CAUSAL_LM,
    )

    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()

    training_args = SFTConfig(
        output_dir=str(output_dir),
        num_train_epochs=epochs,
        per_device_train_batch_size=batch_size,
        gradient_accumulation_steps=gradient_accumulation_steps,
        learning_rate=learning_rate,
        lr_scheduler_type="cosine",
        warmup_ratio=0.1,
        logging_steps=10,
        save_steps=100,
        save_total_limit=3,
        bf16=True,
        max_seq_length=max_seq_length,
        dataset_text_field="",
        dataset_kwargs={"skip_prepare_dataset": True},
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        report_to="none",
        seed=42,
    )

    trainer = SFTTrainer(
        model=model,
        args=training_args,
        train_dataset=dataset,
        processing_class=processor,
    )

    logger.info("Starting standard TRL training for %d epochs...", epochs)
    train_result = trainer.train()

    # Save final LoRA adapter
    logger.info("Saving final LoRA adapter to %s", output_dir / "final")
    model.save_pretrained(str(output_dir / "final"))
    processor.save_pretrained(str(output_dir / "final"))

    # Save training metrics
    metrics = train_result.metrics
    metrics_path = output_dir / "training_metrics.json"
    with open(metrics_path, "w") as f:
        json.dump(metrics, f, indent=2, default=str)

    logger.info("Training complete. Metrics: %s", metrics)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fine-tune a student VLM on distilled frontier model trajectories",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument(
        "--base-model",
        default="Qwen/Qwen3.5-9B",
        help="HuggingFace model ID for the student model (default: Qwen/Qwen3.5-9B)",
    )
    parser.add_argument(
        "--data-dir",
        required=True,
        help="Directory containing distillation data from collect_distillation_data.py",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help=(
            "Directory for saving checkpoints "
            "(default: checkpoints/<model_name>_distilled)"
        ),
    )

    # LoRA parameters
    parser.add_argument("--lora-r", type=int, default=16, help="LoRA rank (default: 16)")
    parser.add_argument(
        "--lora-alpha", type=int, default=32, help="LoRA alpha (default: 32)"
    )

    # Training parameters
    parser.add_argument(
        "--epochs", type=int, default=3, help="Number of training epochs (default: 3)"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1,
        help="Per-device training batch size (default: 1)",
    )
    parser.add_argument(
        "--learning-rate",
        type=float,
        default=2e-4,
        help="Learning rate (default: 2e-4)",
    )
    parser.add_argument(
        "--max-seq-length",
        type=int,
        default=2048,
        help="Maximum sequence length (default: 2048)",
    )
    parser.add_argument(
        "--gradient-accumulation-steps",
        type=int,
        default=4,
        help="Gradient accumulation steps (default: 4)",
    )
    parser.add_argument(
        "--no-4bit",
        action="store_true",
        help="Disable 4-bit quantization (use full bf16)",
    )

    # Mode
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Validate pipeline without loading models or GPU",
    )

    args = parser.parse_args()

    data_dir = Path(args.data_dir)

    # Default output dir based on model name
    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        model_short = args.base_model.split("/")[-1].lower().replace(".", "_")
        output_dir = Path(f"checkpoints/{model_short}_distilled")

    output_dir.mkdir(parents=True, exist_ok=True)

    if args.mock:
        return run_mock_validation(
            data_dir=data_dir,
            base_model=args.base_model,
            lora_r=args.lora_r,
            lora_alpha=args.lora_alpha,
            epochs=args.epochs,
            batch_size=args.batch_size,
        )

    # Save training config
    config = {
        "base_model": args.base_model,
        "data_dir": str(data_dir),
        "output_dir": str(output_dir),
        "lora_r": args.lora_r,
        "lora_alpha": args.lora_alpha,
        "epochs": args.epochs,
        "batch_size": args.batch_size,
        "learning_rate": args.learning_rate,
        "max_seq_length": args.max_seq_length,
        "gradient_accumulation_steps": args.gradient_accumulation_steps,
        "use_4bit": not args.no_4bit,
    }
    config_path = output_dir / "training_config.json"
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
    logger.info("Training config saved to %s", config_path)

    train(
        base_model=args.base_model,
        data_dir=data_dir,
        output_dir=output_dir,
        lora_r=args.lora_r,
        lora_alpha=args.lora_alpha,
        epochs=args.epochs,
        batch_size=args.batch_size,
        learning_rate=args.learning_rate,
        max_seq_length=args.max_seq_length,
        use_4bit=not args.no_4bit,
        gradient_accumulation_steps=args.gradient_accumulation_steps,
    )

    print("\n" + "=" * 70)
    print("FINE-TUNING COMPLETE")
    print("=" * 70)
    print(f"  Base model:    {args.base_model}")
    print(f"  LoRA adapter:  {output_dir / 'final'}")
    print(f"  Training data: {data_dir}")
    print(f"  Config:        {config_path}")
    print()
    print("Next steps:")
    print(f"  1. Evaluate: openadapt-evals run --agent policy --model {output_dir / 'final'}")
    print(f"  2. RL training: python scripts/train_trl_grpo.py --model {output_dir / 'final'}")
    print("=" * 70)

    return 0


if __name__ == "__main__":
    sys.exit(main())

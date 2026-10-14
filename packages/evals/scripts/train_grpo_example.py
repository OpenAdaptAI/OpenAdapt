#!/usr/bin/env python3
"""Self-contained GRPO training example for GUI agents.

Demonstrates the full RL loop: connect to WAA, collect rollouts with a VLM
policy, compute group-relative advantages, update LoRA weights via
REINFORCE with group-relative advantages (equivalent to single-epoch GRPO).
No openadapt-ml dependency -- all math and parsing are inline.

Requirements:
    pip install torch transformers peft pillow
    # openadapt-evals must be installed (provides RLEnvironment, adapters)

Usage:
    # Mock mode (no VM, random rewards -- for testing the loop):
    python scripts/train_grpo_example.py --mock --num-steps 2 --group-size 3

    # Live WAA server:
    python scripts/train_grpo_example.py \\
        --server http://localhost:5001 \\
        --task-id <WAA_UUID> \\
        --num-steps 10 \\
        --group-size 4

    # Custom model and learning rate:
    python scripts/train_grpo_example.py \\
        --mock \\
        --model-name Qwen/Qwen2.5-VL-7B-Instruct \\
        --lr 1e-5 \\
        --num-steps 5
"""

from __future__ import annotations

import io
import re
import time

import fire
import torch
from peft import LoraConfig, get_peft_model
from PIL import Image
from transformers import AutoProcessor

try:
    from transformers import AutoModelForImageTextToText as AutoVLM
except ImportError:
    from transformers import AutoModelForVision2Seq as AutoVLM

from openadapt_evals.adapters.base import BenchmarkAction, BenchmarkObservation
from openadapt_evals.adapters.rl_env import RLEnvironment

# -- Policy gradient loss ------------------------------------------------------


def policy_gradient_loss(
    current_logps: torch.Tensor,
    old_logps: torch.Tensor,
    advantages: torch.Tensor,
    epsilon: float = 0.2,
) -> torch.Tensor:
    """Policy gradient loss with optional PPO-style clipping.

    When old_logps == current_logps (single-epoch), reduces to REINFORCE.
    """
    ratio = torch.exp(current_logps - old_logps)
    clipped = torch.clamp(ratio, 1.0 - epsilon, 1.0 + epsilon)
    return -torch.min(ratio * advantages, clipped * advantages).mean()


def compute_advantages(rewards: list[float]) -> list[float]:
    """Group-relative advantage: (r - mean) / (std + eps)."""
    n = len(rewards)
    if n == 0:
        return []
    mean = sum(rewards) / n
    std = (sum((r - mean) ** 2 for r in rewards) / n) ** 0.5
    if std < 1e-8:
        return [0.0] * n
    return [(r - mean) / (std + 1e-8) for r in rewards]


# -- Helpers -------------------------------------------------------------------

# Aligned with openadapt_ml.datasets.next_action.SYSTEM_PROMPT
SYSTEM_PROMPT = (
    "You are a GUI automation agent. Given a screenshot and a user goal, "
    "predict the single next action.\n\n"
    "COORDINATE SYSTEM:\n"
    "- x=0.0 is the LEFT edge, x=1.0 is the RIGHT edge\n"
    "- y=0.0 is the TOP edge, y=1.0 is the BOTTOM edge\n"
    "- To click the CENTER of an element, estimate its center position "
    "as a fraction of screen width/height\n"
    "- Example: An element in the middle of the screen would be "
    "approximately x=0.5, y=0.5\n\n"
    "ALLOWED ACTIONS (use exactly this format):\n"
    "- CLICK(x=0.XX, y=0.XX)  → click at normalized coordinates\n"
    '- TYPE(text="...")     → type text into the currently focused field\n'
    "- WAIT()                 → wait for UI to update\n"
    "- DONE()                 → task is complete\n\n"
    "RESPONSE FORMAT (required):\n"
    "Thought: [Brief reasoning: what element to interact with and why]\n"
    "Action: [Exactly one action, e.g., CLICK(x=0.35, y=0.42)]\n\n"
    "IMPORTANT: Output coordinates with 2 decimal places. "
    "Estimate the center of target elements."
)

DEFAULT_SCREEN_SIZE = (1920, 1080)  # Aligned with openadapt-ml


def parse_action(
    text: str,
    width: int = 1920,
    height: int = 1080,
) -> BenchmarkAction:
    """Parse VLM text output into a BenchmarkAction.

    Supports: CLICK(x=0.XX, y=0.XX), TYPE(text="..."), WAIT(), DONE().
    Aligned with openadapt_ml.training.grpo.trainer.parse_vlm_output_to_action.
    """
    text = text.strip()

    # CLICK
    m = re.search(r"CLICK\(x=(-?[\d.]+),\s*y=(-?[\d.]+)\)", text, re.IGNORECASE)
    if m:
        x_frac = max(0.0, min(1.0, float(m.group(1))))
        y_frac = max(0.0, min(1.0, float(m.group(2))))
        return BenchmarkAction(
            type="click", x=float(int(x_frac * width)), y=float(int(y_frac * height))
        )

    # TYPE (handles escaped quotes)
    m = re.search(r"""TYPE\(text=["']([^"'\\]*(?:\\.[^"'\\]*)*)["']\)""", text, re.IGNORECASE)
    if m:
        typed_text = m.group(1).replace("\\\\", "\\").replace('\\"', '"').replace("\\'", "'")
        return BenchmarkAction(type="type", text=typed_text)

    # WAIT
    if re.search(r"\bWAIT\s*\(\s*\)", text, re.IGNORECASE):
        return BenchmarkAction(type="wait")

    # DONE
    if re.search(r"\bDONE\s*\(\s*\)", text, re.IGNORECASE):
        return BenchmarkAction(type="done")

    return BenchmarkAction(type="done")


def build_agent_messages(instruction: str) -> list[dict[str, str]]:
    """Build chat messages — aligned with openadapt-ml _build_agent_messages."""
    user_content = (
        f"Goal: {instruction}\n\n"
        "Look at the screenshot and determine the NEXT action.\n\n"
        'Action: [CLICK(x=..., y=...) or TYPE(text="...") or WAIT() or DONE()]'
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def format_action_as_text(
    action: BenchmarkAction,
    width: int = 1920,
    height: int = 1080,
) -> str:
    """Convert BenchmarkAction back to DSL text for log-prob computation."""
    action_type = getattr(action, "type", "done")
    if action_type == "click":
        x_px = getattr(action, "x", 0) or 0
        y_px = getattr(action, "y", 0) or 0
        x_frac = x_px / width if width > 0 else 0.0
        y_frac = y_px / height if height > 0 else 0.0
        return f"CLICK(x={x_frac:.2f}, y={y_frac:.2f})"
    if action_type == "type":
        text = getattr(action, "text", "") or ""
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'TYPE(text="{escaped}")'
    if action_type == "wait":
        return "WAIT()"
    return "DONE()"


def trajectory_logprob(
    model, processor, screenshot_bytes: bytes, instruction: str, action_text: str
) -> torch.Tensor:
    """Forward pass: compute log-prob of action_text given screenshot + prompt."""
    image = Image.open(io.BytesIO(screenshot_bytes)).convert("RGB")

    # Use chat template if available (aligned with openadapt-ml trainer)
    messages = build_agent_messages(instruction)
    if hasattr(processor, "apply_chat_template"):
        prompt = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    else:
        prompt = messages[-1]["content"]

    inputs = processor(prompt, images=[image], return_tensors="pt")
    inputs = {k: v.to(model.device) for k, v in inputs.items()}

    tokenizer = getattr(processor, "tokenizer", processor)
    action_ids = tokenizer(action_text, return_tensors="pt", add_special_tokens=False)[
        "input_ids"
    ].to(model.device)
    prompt_len = inputs["input_ids"].shape[1]

    full_ids = torch.cat([inputs["input_ids"], action_ids], dim=1)
    inputs["input_ids"] = full_ids
    inputs["attention_mask"] = torch.ones_like(full_ids)

    logits = model(**inputs).logits
    action_logits = logits[:, prompt_len - 1 : prompt_len - 1 + action_ids.shape[1], :]
    log_probs = torch.nn.functional.log_softmax(action_logits, dim=-1)
    token_lps = log_probs.gather(2, action_ids.unsqueeze(-1)).squeeze(-1)
    return token_lps.sum()


# -- Main training loop -------------------------------------------------------


def main(
    server: str = "http://localhost:5001",
    task_id: str | None = None,
    num_steps: int = 5,
    group_size: int = 4,
    max_episode_steps: int = 15,
    model_name: str = "Qwen/Qwen2.5-VL-7B-Instruct",
    lr: float = 1e-5,
    checkpoint_dir: str = "grpo_checkpoint",
    mock: bool = False,
) -> None:
    """Run GRPO training: rollouts -> advantages -> policy gradient -> update."""
    # 1. Load model with LoRA
    print(f"Loading {model_name} ...")
    processor = AutoProcessor.from_pretrained(model_name)
    model = AutoVLM.from_pretrained(
        model_name, torch_dtype=torch.bfloat16, device_map="auto"
    )
    lora = LoraConfig(
        r=16,
        lora_alpha=32,
        target_modules=["q_proj", "v_proj", "k_proj", "o_proj"],
        task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, lora)
    model.print_trainable_parameters()
    optimizer = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=lr)

    # 2. Create RL environment
    if mock:
        from openadapt_evals.adapters.waa.mock import WAAMockAdapter

        adapter = WAAMockAdapter(num_tasks=20)
    else:
        from openadapt_evals.adapters.waa.live import WAALiveAdapter, WAALiveConfig

        adapter = WAALiveAdapter(WAALiveConfig(server_url=server))

    if task_id is None:
        tasks = adapter.list_tasks()
        task_id = tasks[0].task_id
        print(f"Auto-selected task: {task_id}")

    env = RLEnvironment(adapter=adapter, default_task_id=task_id)
    w, h = env.screen_size

    # 3. Agent function: screenshot -> VLM -> action
    def agent_fn(obs: BenchmarkObservation) -> BenchmarkAction:
        if not obs.screenshot:
            return BenchmarkAction(type="done")
        image = Image.open(io.BytesIO(obs.screenshot)).convert("RGB")
        task = getattr(env, "_current_task", None)
        goal = task.instruction if task else "Complete the task."

        messages = build_agent_messages(goal)
        if hasattr(processor, "apply_chat_template"):
            prompt = processor.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )
        else:
            prompt = messages[-1]["content"]

        inputs = processor(prompt, images=[image], return_tensors="pt")
        inputs = {k: v.to(model.device) for k, v in inputs.items()}
        with torch.no_grad():
            out = model.generate(
                **inputs,
                max_new_tokens=100,
                do_sample=True,
                temperature=0.7,
            )
        text = processor.decode(
            out[0][inputs["input_ids"].shape[1] :],
            skip_special_tokens=True,
        )
        action = parse_action(text, w, h)
        action._raw_text = text  # stash for log-prob recomputation
        return action

    # 4. Training loop
    for step in range(num_steps):
        t0 = time.time()
        print(f"\n{'=' * 50}\nStep {step + 1}/{num_steps}: collecting {group_size} rollouts ...")

        # -- Collect rollouts --
        model.eval()
        rollouts, rewards = [], []
        for g in range(group_size):
            trajectory = env.collect_rollout(agent_fn=agent_fn, max_steps=max_episode_steps)
            reward = trajectory[-1].reward if trajectory else 0.0
            rollouts.append(trajectory)
            rewards.append(reward)
            print(f"  rollout {g + 1}: {len(trajectory)} steps, reward={reward:.2f}")

        # -- Compute advantages --
        advantages = compute_advantages(rewards)
        if all(a == 0.0 for a in advantages):
            print("  No variance in rewards, skipping gradient step.")
            continue

        # -- Policy gradient update --
        model.train()
        optimizer.zero_grad()
        total_loss = 0.0
        n_terms = 0

        task = getattr(env, "_current_task", None)
        instruction = task.instruction if task else ""

        n_valid = sum(1 for _, a in zip(rollouts, advantages) if abs(a) >= 1e-8)

        for traj, adv in zip(rollouts, advantages):
            if abs(adv) < 1e-8:
                continue
            num_steps = max(len(traj), 1)
            for s in traj:
                if not s.observation.screenshot:
                    continue
                # Use raw VLM text if available, else reconstruct from action
                action_text = getattr(s.action, "_raw_text", None)
                if not action_text:
                    action_text = format_action_as_text(s.action, w, h)
                logp = trajectory_logprob(
                    model, processor, s.observation.screenshot, instruction, action_text
                )
                adv_t = torch.tensor(adv, device=logp.device, dtype=logp.dtype)
                loss = policy_gradient_loss(
                    logp.unsqueeze(0),
                    logp.detach().unsqueeze(0),
                    adv_t.unsqueeze(0),
                )
                # Scale by 1/(num_valid_rollouts * num_steps) to match ml trainer
                scaled = loss / (n_valid * num_steps)
                scaled.backward()
                total_loss += loss.item()
                n_terms += 1

        torch.nn.utils.clip_grad_norm_([p for p in model.parameters() if p.requires_grad], 1.0)
        optimizer.step()

        avg_loss = total_loss / max(n_terms, 1)
        avg_reward = sum(rewards) / len(rewards)
        print(f"  loss={avg_loss:.4f}  mean_reward={avg_reward:.3f}  time={time.time() - t0:.1f}s")

    # 5. Save checkpoint
    model.save_pretrained(checkpoint_dir)
    print(f"\nCheckpoint saved to {checkpoint_dir}/")
    print("Done.")


if __name__ == "__main__":
    fire.Fire(main)

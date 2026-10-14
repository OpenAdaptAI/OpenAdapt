#!/usr/bin/env python3
"""
WAA Benchmark Cost Estimation for Vision Models

Calculates the estimated cost to run 154 WAA tasks with different vision models.
Based on official pricing from OpenAI and Anthropic (February 2026).

Sources:
- OpenAI: https://platform.openai.com/docs/pricing
- Anthropic: https://platform.claude.com/docs/en/about-claude/pricing
- OpenAI image tokens: https://community.openai.com/t/how-do-i-calculate-image-tokens-in-gpt4-vision/492318
- Claude vision: https://platform.claude.com/docs/en/build-with-claude/vision
"""

import math
from dataclasses import dataclass
from typing import Callable


@dataclass
class ModelPricing:
    """Pricing for a vision model."""
    name: str
    input_per_million: float  # USD per million input tokens
    output_per_million: float  # USD per million output tokens
    image_token_calculator: Callable[[int, int], int]  # (width, height) -> tokens
    notes: str = ""


def openai_image_tokens(width: int, height: int, detail: str = "high") -> int:
    """
    Calculate image tokens for OpenAI GPT-4o/GPT-4o-mini.

    Formula (high detail):
    1. Scale image to fit within 2048x2048
    2. Scale shortest side to 768px
    3. Divide into 512x512 tiles
    4. tokens = 85 + (170 * num_tiles)

    For low detail: always 85 tokens

    Source: https://community.openai.com/t/how-do-i-calculate-image-tokens-in-gpt4-vision/492318
    """
    if detail == "low":
        return 85

    # Step 1: Scale to fit within 2048x2048
    max_dim = 2048
    if width > max_dim or height > max_dim:
        scale = max_dim / max(width, height)
        width = int(width * scale)
        height = int(height * scale)

    # Step 2: Scale shortest side to 768px
    short_side = min(width, height)
    if short_side > 768:
        scale = 768 / short_side
        width = int(width * scale)
        height = int(height * scale)

    # Step 3: Calculate tiles (512x512)
    tiles_x = math.ceil(width / 512)
    tiles_y = math.ceil(height / 512)
    num_tiles = tiles_x * tiles_y

    # Step 4: Calculate tokens
    return 85 + (170 * num_tiles)


def claude_image_tokens(width: int, height: int) -> int:
    """
    Calculate image tokens for Claude models.

    Formula: tokens = (width * height) / 750

    If long edge > 1568px, image is resized to fit within 1568px on long edge.
    Maximum useful resolution is ~1.15 megapixels.

    Source: https://platform.claude.com/docs/en/build-with-claude/vision
    """
    # If long edge > 1568, scale down preserving aspect ratio
    long_edge = max(width, height)
    if long_edge > 1568:
        scale = 1568 / long_edge
        width = int(width * scale)
        height = int(height * scale)

    return int((width * height) / 750)


# Model pricing definitions (February 2026)
MODELS = [
    ModelPricing(
        name="GPT-4o",
        input_per_million=2.50,
        output_per_million=10.00,
        image_token_calculator=openai_image_tokens,
        notes="High detail mode. Image tokens charged as input tokens."
    ),
    ModelPricing(
        name="GPT-4o-mini",
        input_per_million=0.15,
        output_per_million=0.60,
        image_token_calculator=openai_image_tokens,
        notes="High detail mode. Much cheaper but less capable."
    ),
    ModelPricing(
        name="Claude Sonnet 4",
        input_per_million=3.00,
        output_per_million=15.00,
        image_token_calculator=claude_image_tokens,
        notes="Claude Sonnet 4.5 pricing ($3/$15). Good balance of cost and capability."
    ),
    ModelPricing(
        name="Claude Opus 4.5",
        input_per_million=15.00,
        output_per_million=75.00,
        image_token_calculator=claude_image_tokens,
        notes="Most capable, highest cost."
    ),
    ModelPricing(
        name="Claude Haiku 4.5",
        input_per_million=1.00,
        output_per_million=5.00,
        image_token_calculator=claude_image_tokens,
        notes="Fastest and cheapest Claude model."
    ),
]


# WAA benchmark parameters
WAA_PARAMS = {
    "num_tasks": 154,
    "avg_steps_per_task": 22,  # Average from WAA paper (range: 15-30)
    "screenshot_width": 1920,
    "screenshot_height": 1080,
    "text_tokens_per_step": 500,  # Task instruction, accessibility tree, context
    "output_tokens_per_step": 300,  # Action + reasoning
}


def calculate_cost_per_step(model: ModelPricing, params: dict) -> dict:
    """Calculate cost for a single step."""
    # Image tokens
    image_tokens = model.image_token_calculator(
        params["screenshot_width"],
        params["screenshot_height"]
    )

    # Total input tokens (image + text)
    total_input_tokens = image_tokens + params["text_tokens_per_step"]

    # Output tokens
    output_tokens = params["output_tokens_per_step"]

    # Cost calculation
    input_cost = (total_input_tokens / 1_000_000) * model.input_per_million
    output_cost = (output_tokens / 1_000_000) * model.output_per_million
    total_cost = input_cost + output_cost

    return {
        "image_tokens": image_tokens,
        "text_tokens": params["text_tokens_per_step"],
        "total_input_tokens": total_input_tokens,
        "output_tokens": output_tokens,
        "input_cost": input_cost,
        "output_cost": output_cost,
        "total_cost": total_cost,
    }


def calculate_full_benchmark_cost(model: ModelPricing, params: dict) -> dict:
    """Calculate cost for the full WAA benchmark."""
    step_costs = calculate_cost_per_step(model, params)

    total_steps = params["num_tasks"] * params["avg_steps_per_task"]

    return {
        "model": model.name,
        "num_tasks": params["num_tasks"],
        "avg_steps_per_task": params["avg_steps_per_task"],
        "total_steps": total_steps,
        "image_tokens_per_step": step_costs["image_tokens"],
        "total_input_tokens_per_step": step_costs["total_input_tokens"],
        "output_tokens_per_step": step_costs["output_tokens"],
        "cost_per_step": step_costs["total_cost"],
        "cost_per_task": step_costs["total_cost"] * params["avg_steps_per_task"],
        "total_cost": step_costs["total_cost"] * total_steps,
        "input_rate": f"${model.input_per_million}/M",
        "output_rate": f"${model.output_per_million}/M",
        "notes": model.notes,
    }


def print_results():
    """Print formatted cost estimation results."""
    print("=" * 100)
    print("WAA BENCHMARK COST ESTIMATION (154 tasks)")
    print("=" * 100)
    print()

    # Screenshot info
    print("## Screenshot Parameters")
    print(f"   Resolution: {WAA_PARAMS['screenshot_width']}x{WAA_PARAMS['screenshot_height']} (1920x1080)")
    print(f"   Avg steps per task: {WAA_PARAMS['avg_steps_per_task']}")
    print(f"   Text context per step: ~{WAA_PARAMS['text_tokens_per_step']} tokens")
    print(f"   Output per step: ~{WAA_PARAMS['output_tokens_per_step']} tokens")
    print(f"   Total steps: {WAA_PARAMS['num_tasks']} tasks × {WAA_PARAMS['avg_steps_per_task']} steps = {WAA_PARAMS['num_tasks'] * WAA_PARAMS['avg_steps_per_task']} steps")
    print()

    # Image token calculation comparison
    print("## Image Token Calculation")
    w, h = WAA_PARAMS['screenshot_width'], WAA_PARAMS['screenshot_height']
    print(f"   OpenAI (GPT-4o): {openai_image_tokens(w, h)} tokens")
    print("     - Formula: 85 + (170 × tiles), where tiles = ceil(W/512) × ceil(H/512)")
    print("     - 1920x1080 → scaled to 1365x768 → 3×2=6 tiles → 85 + 170×6 = 1,105 tokens")
    print()
    print(f"   Anthropic (Claude): {claude_image_tokens(w, h)} tokens")
    print("     - Formula: (W × H) / 750")
    print("     - 1920x1080 → scaled to 1568×882 → 1,568×882/750 = 1,844 tokens")
    print()

    # Cost summary table
    print("## Cost Summary Table")
    print()
    print("| Model            | Input Rate   | Output Rate  | Image Tokens | Total In/Step | Cost/Step | Cost/Task | TOTAL COST |")
    print("|------------------|--------------|--------------|--------------|---------------|-----------|-----------|------------|")

    results = []
    for model in MODELS:
        result = calculate_full_benchmark_cost(model, WAA_PARAMS)
        results.append(result)
        print(f"| {result['model']:<16} | {result['input_rate']:<12} | {result['output_rate']:<12} | "
              f"{result['image_tokens_per_step']:>12,} | {result['total_input_tokens_per_step']:>13,} | "
              f"${result['cost_per_step']:>8.5f} | ${result['cost_per_task']:>8.4f} | ${result['total_cost']:>9.2f} |")

    print()

    # Detailed breakdown
    print("## Detailed Breakdown")
    print()
    for result in results:
        print(f"### {result['model']}")
        print(f"   Input: {result['input_rate']} | Output: {result['output_rate']}")
        print(f"   Image tokens/step: {result['image_tokens_per_step']:,}")
        print(f"   Total input tokens/step: {result['total_input_tokens_per_step']:,}")
        print(f"   Output tokens/step: {result['output_tokens_per_step']:,}")
        print(f"   Cost per step: ${result['cost_per_step']:.6f}")
        print(f"   Cost per task: ${result['cost_per_task']:.4f}")
        print(f"   **Total cost (154 tasks): ${result['total_cost']:.2f}**")
        print(f"   Notes: {result['notes']}")
        print()

    # Recommendations
    print("## Recommendations")
    print()
    cheapest = min(results, key=lambda x: x['total_cost'])
    most_expensive = max(results, key=lambda x: x['total_cost'])

    # Find Claude Sonnet for "balanced" recommendation
    sonnet = next((r for r in results if "Sonnet" in r['model']), None)

    print(f"   **Cheapest option**: {cheapest['model']} at ${cheapest['total_cost']:.2f}")
    print(f"   **Most expensive**: {most_expensive['model']} at ${most_expensive['total_cost']:.2f}")
    if sonnet:
        print(f"   **Recommended (balanced)**: {sonnet['model']} at ${sonnet['total_cost']:.2f}")
    print()

    # Cost comparison
    print("## Cost Comparison Chart")
    print()
    max_cost = max(r['total_cost'] for r in results)
    for result in sorted(results, key=lambda x: x['total_cost']):
        bar_len = int(40 * result['total_cost'] / max_cost)
        bar = "█" * bar_len
        print(f"   {result['model']:<16} ${result['total_cost']:>7.2f} {bar}")


def _flow_estimate_main(argv=None) -> int:
    """Flow-aware estimate: extends the legacy table with the demonstrate-then-
    replay / hybrid modes and a cost-guarded --dry-run.

    Delegates to :mod:`openadapt_evals.flow.cost` (the canonical cost model) so
    the numbers match ``scripts/eval_flow_on_waa.py``.
    """
    import argparse
    import sys as _sys
    from pathlib import Path

    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from openadapt_evals.flow.cost import MODELS as FLOW_MODELS
    from openadapt_evals.flow.cost import estimate_flow_waa_cost

    ap = argparse.ArgumentParser(
        description="WAA cost estimate (legacy vision table, or flow-aware modes)."
    )
    ap.add_argument("--tasks", type=int, default=154)
    ap.add_argument("--mode", choices=["legacy", "replay", "hybrid"], default="legacy")
    ap.add_argument("--model", choices=sorted(FLOW_MODELS), default="claude-sonnet-4")
    ap.add_argument("--fallback-rate", type=float, default=0.30)
    ap.add_argument("--vm-hourly", type=float, default=0.19)
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the estimate and exit (never provisions anything).")
    args = ap.parse_args(argv)

    if args.mode == "legacy":
        print_results()
        return 0

    est = estimate_flow_waa_cost(
        args.tasks, mode=args.mode, model_name=args.model,
        fallback_rate=args.fallback_rate, vm_hourly=args.vm_hourly,
    )
    d = est.as_dict()
    print(f"mode={d['mode']} tasks={d['num_tasks']} model={d['model']}")
    print(f"  Azure VM cost:        ${d['vm_cost_usd']:.2f}  ({d['vm_hours']:.2f} vm-hours)")
    print(f"  Agent token cost:     ${d['token_cost_usd']:.2f}")
    print(f"  TOTAL:                ${d['total_cost_usd']:.2f}  (${d['cost_per_task_usd']:.4f}/task)")
    print(f"  Pure-agent baseline:  ${d['baseline_pure_agent_cost_usd']:.2f}")
    print(f"  Savings vs baseline:  ${d['savings_vs_baseline_usd']:.2f}")
    if args.dry_run:
        print("  (dry-run: no VM provisioned, no money spent)")
    return 0


if __name__ == "__main__":
    import sys as _sys
    # Backward compatible: no args -> the legacy table. Any flag -> flow-aware.
    if len(_sys.argv) > 1:
        raise SystemExit(_flow_estimate_main())
    print_results()

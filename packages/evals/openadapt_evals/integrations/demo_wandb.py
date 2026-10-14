#!/usr/bin/env python
"""Demo script to populate wandb with synthetic evaluation data.

This script generates and logs synthetic benchmark data to wandb for
demonstration and testing purposes. It creates runs for different
scenarios (noise, best, worst, median) to showcase the dashboards.

Usage:
    # Log all scenarios
    python -m openadapt_evals.integrations.demo_wandb --project my-project

    # Log specific scenarios
    python -m openadapt_evals.integrations.demo_wandb --scenarios noise median

    # Generate but don't upload (dry run)
    python -m openadapt_evals.integrations.demo_wandb --dry-run

    # Use specific seed for reproducibility
    python -m openadapt_evals.integrations.demo_wandb --seed 42

Environment Variables:
    WANDB_API_KEY: Your wandb API key (required unless --dry-run)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime

from dotenv import load_dotenv

from openadapt_evals.integrations.fixtures import WAA_DOMAINS, Scenario
from openadapt_evals.integrations.wandb_logger import WandbLogger

# Load .env file
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def create_demo_run(
    scenario: Scenario,
    project: str,
    entity: str | None = None,
    num_tasks: int = 154,
    seed: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Create a demo wandb run for a given scenario.

    Args:
        scenario: The scenario to generate data for.
        project: Wandb project name.
        entity: Wandb entity (team/org).
        num_tasks: Number of tasks to generate.
        seed: Random seed for reproducibility.
        dry_run: If True, generate data but don't upload.

    Returns:
        Dict with run metadata and metrics.
    """
    config = scenario.config
    logger.info(f"Generating {config.name} scenario: {config.description}")

    # Generate synthetic data
    results = scenario.generate(num_tasks=num_tasks, seed=seed)

    # Compute actual metrics
    success_count = sum(1 for r in results if r.success)
    success_rate = success_count / len(results)
    avg_steps = sum(r.num_steps for r in results) / len(results)
    avg_time = sum(r.total_time_seconds for r in results) / len(results)

    logger.info(
        f"  Generated {len(results)} tasks: "
        f"{success_rate:.1%} success rate, {avg_steps:.1f} avg steps"
    )

    run_metadata = {
        "scenario": config.name,
        "description": config.description,
        "num_tasks": len(results),
        "success_rate": success_rate,
        "expected_success_rate": config.expected_success_rate,
        "avg_steps": avg_steps,
        "avg_time_seconds": avg_time,
    }

    if dry_run:
        logger.info("  [DRY RUN] Would upload to wandb")
        return run_metadata

    # Upload to wandb
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_name = f"demo_{config.name}_{timestamp}"

    wandb_logger = WandbLogger(
        project=project,
        entity=entity,
        config={
            "model_id": f"synthetic-{config.name}",
            "benchmark": "waa",
            "scenario": config.name,
            "description": config.description,
            "expected_success_rate": config.expected_success_rate,
            "num_tasks": num_tasks,
            "seed": seed,
        },
        tags=["demo", "synthetic"] + config.tags,
        name=run_name,
        notes=f"Demo run for {config.name} scenario: {config.description}",
    )

    try:
        wandb_logger.init()
        wandb_logger.log_results(results)
        run_metadata["wandb_url"] = wandb_logger._run.url
        logger.info(f"  Uploaded to: {wandb_logger._run.url}")
    finally:
        wandb_logger.finish()

    return run_metadata


def create_comparison_run(
    project: str,
    entity: str | None = None,
    num_tasks: int = 154,
    seed: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Create a run comparing multiple model versions.

    This simulates tracking improvement over model iterations.

    Args:
        project: Wandb project name.
        entity: Wandb entity (team/org).
        num_tasks: Number of tasks per version.
        seed: Random seed for reproducibility.
        dry_run: If True, don't upload.

    Returns:
        Dict with comparison metadata.
    """
    logger.info("Generating model comparison data (v1 -> v5)")

    # Simulate 5 model versions with improving performance
    versions = [
        {"version": 1, "success_rate": 0.08, "name": "baseline"},
        {"version": 2, "success_rate": 0.12, "name": "+ demo prompting"},
        {"version": 3, "success_rate": 0.18, "name": "+ better grounding"},
        {"version": 4, "success_rate": 0.25, "name": "+ fine-tuned"},
        {"version": 5, "success_rate": 0.32, "name": "+ ensemble"},
    ]

    if dry_run:
        logger.info("  [DRY RUN] Would upload model comparison")
        return {"versions": versions}

    results_by_version = {}
    for v in versions:
        # Generate data with specific success rate
        import random
        if seed:
            random.seed(seed + v["version"])

        results = []
        for i in range(num_tasks):
            domain = WAA_DOMAINS[i % len(WAA_DOMAINS)]
            success = random.random() < v["success_rate"]
            results.append({
                "task_id": f"{domain}_{i + 1}",
                "success": success,
                "score": random.uniform(0.7, 1.0) if success else random.uniform(0.1, 0.4),
                "num_steps": random.randint(4, 10) if success else random.randint(10, 15),
            })

        results_by_version[v["version"]] = results

        # Create wandb run for this version
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_name = f"model_v{v['version']}_{timestamp}"

        from openadapt_evals.adapters import BenchmarkResult

        benchmark_results = [
            BenchmarkResult(
                task_id=r["task_id"],
                success=r["success"],
                score=r["score"],
                num_steps=r["num_steps"],
                total_time_seconds=random.uniform(20, 80),
            )
            for r in results
        ]

        wandb_logger = WandbLogger(
            project=project,
            entity=entity,
            config={
                "model_id": f"openadapt-v{v['version']}",
                "model_version": v["version"],
                "benchmark": "waa",
                "description": v["name"],
            },
            tags=["comparison", "model-iteration", f"v{v['version']}"],
            name=run_name,
            notes=f"Model v{v['version']}: {v['name']}",
        )

        try:
            wandb_logger.init()
            wandb_logger.log_results(benchmark_results)
            logger.info(f"  v{v['version']}: {v['success_rate']:.0%} -> {wandb_logger._run.url}")
        finally:
            wandb_logger.finish()

    return {"versions": versions}


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Populate wandb with synthetic evaluation data for demo"
    )
    parser.add_argument(
        "--project",
        default="openadapt-evals-demo",
        help="Wandb project name (default: openadapt-evals-demo)",
    )
    parser.add_argument(
        "--entity",
        default=None,
        help="Wandb entity (team/org)",
    )
    parser.add_argument(
        "--scenarios",
        nargs="+",
        choices=["noise", "best", "worst", "median", "comparison", "all"],
        default=["all"],
        help="Scenarios to generate (default: all)",
    )
    parser.add_argument(
        "--num-tasks",
        type=int,
        default=154,
        help="Number of tasks per scenario (default: 154)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Random seed for reproducibility",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate data but don't upload to wandb",
    )

    args = parser.parse_args()

    # Check for API key
    if not args.dry_run and not os.environ.get("WANDB_API_KEY"):
        logger.error(
            "WANDB_API_KEY not set. Either:\n"
            "  1. Set WANDB_API_KEY in your environment or .env file\n"
            "  2. Use --dry-run to test without uploading"
        )
        sys.exit(1)

    # Determine which scenarios to run
    scenarios_to_run = args.scenarios
    if "all" in scenarios_to_run:
        scenarios_to_run = ["noise", "best", "worst", "median", "comparison"]

    logger.info(f"Project: {args.project}")
    logger.info(f"Scenarios: {scenarios_to_run}")
    logger.info(f"Tasks per scenario: {args.num_tasks}")
    if args.dry_run:
        logger.info("DRY RUN MODE - no data will be uploaded")
    print()

    # Map scenario names to Scenario enum
    scenario_map = {
        "noise": Scenario.NOISE,
        "best": Scenario.BEST,
        "worst": Scenario.WORST,
        "median": Scenario.MEDIAN,
    }

    results = {}

    for scenario_name in scenarios_to_run:
        if scenario_name == "comparison":
            results["comparison"] = create_comparison_run(
                project=args.project,
                entity=args.entity,
                num_tasks=args.num_tasks,
                seed=args.seed,
                dry_run=args.dry_run,
            )
        else:
            scenario = scenario_map[scenario_name]
            results[scenario_name] = create_demo_run(
                scenario=scenario,
                project=args.project,
                entity=args.entity,
                num_tasks=args.num_tasks,
                seed=args.seed,
                dry_run=args.dry_run,
            )
        print()

    # Print summary
    print("=" * 60)
    print("Summary")
    print("=" * 60)
    for name, metadata in results.items():
        print(f"\n{name}:")
        if "wandb_url" in metadata:
            print(f"  URL: {metadata['wandb_url']}")
        if "success_rate" in metadata:
            print(f"  Success Rate: {metadata['success_rate']:.1%}")
        if "versions" in metadata:
            for v in metadata["versions"]:
                print(f"  v{v['version']}: {v['success_rate']:.0%} ({v['name']})")

    if not args.dry_run:
        print(f"\nView all runs at: https://wandb.ai/{args.entity or 'your-entity'}/{args.project}")


if __name__ == "__main__":
    main()

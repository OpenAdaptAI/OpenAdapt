"""Synthetic data fixtures for wandb integration testing and demos.

This module provides parametrizable synthetic data generators for:
- Pure noise (random baseline)
- Best case (high-performing model)
- Worst case (failing model)
- Median case (SOTA-like performance)

Example:
    from openadapt_evals.integrations.fixtures import generate_median_case_data, Scenario

    # Generate SOTA-like results
    results = generate_median_case_data(num_tasks=154)

    # Use pre-defined scenario
    results = Scenario.MEDIAN.generate(num_tasks=50)
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from openadapt_evals.adapters import BenchmarkResult

# WAA domains (11 domains, 154 tasks total in real benchmark)
WAA_DOMAINS = [
    "chrome",
    "clock",
    "file_explorer",
    "libreoffice_calc",
    "libreoffice_writer",
    "microsoft_paint",
    "msedge",
    "notepad",
    "settings",
    "vlc",
    "vs_code",
    "windows_calc",
]

# Domain difficulty mapping (success rates for median case)
DOMAIN_DIFFICULTY = {
    "notepad": 0.40,  # Easiest - simple text editing
    "windows_calc": 0.35,
    "clock": 0.30,
    "file_explorer": 0.28,
    "settings": 0.25,
    "microsoft_paint": 0.22,
    "chrome": 0.20,
    "msedge": 0.18,
    "vlc": 0.15,
    "libreoffice_writer": 0.12,
    "libreoffice_calc": 0.10,
    "vs_code": 0.08,  # Hardest - complex IDE
}

# Error types with relative frequencies
ERROR_TYPES = [
    ("timeout", 0.35),           # Max steps reached
    ("wrong_action", 0.30),      # Clicked wrong element
    ("navigation_error", 0.20),  # Got lost in UI
    ("element_not_found", 0.10), # Target element not visible
    ("api_error", 0.05),         # Model API failure
]


def _weighted_choice(choices: list[tuple[str, float]]) -> str:
    """Select from weighted choices."""
    total = sum(w for _, w in choices)
    r = random.uniform(0, total)
    cumulative = 0
    for choice, weight in choices:
        cumulative += weight
        if r <= cumulative:
            return choice
    return choices[-1][0]


def generate_noise_data(
    num_tasks: int = 154,
    domains: list[str] | None = None,
    seed: int | None = None,
) -> list[BenchmarkResult]:
    """Generate random baseline data with ~10% success rate.

    Simulates a random agent that takes arbitrary actions.

    Args:
        num_tasks: Number of tasks to generate.
        domains: List of domains to use (defaults to WAA_DOMAINS).
        seed: Random seed for reproducibility.

    Returns:
        List of BenchmarkResult with random outcomes.
    """
    if seed is not None:
        random.seed(seed)
    if domains is None:
        domains = WAA_DOMAINS

    results = []
    tasks_per_domain = max(1, num_tasks // len(domains))

    for domain in domains:
        for i in range(tasks_per_domain):
            if len(results) >= num_tasks:
                break

            success = random.random() < 0.10  # 10% random success
            error = None if success else _weighted_choice(ERROR_TYPES)

            results.append(BenchmarkResult(
                task_id=f"{domain}_{i + 1}",
                success=success,
                score=random.uniform(0.6, 1.0) if success else random.uniform(0, 0.3),
                num_steps=random.randint(12, 15),  # Usually max out
                total_time_seconds=random.uniform(50, 120),
                error=error,
                reason=f"Random baseline: {'succeeded' if success else error}",
            ))

    return results[:num_tasks]


def generate_best_case_data(
    num_tasks: int = 154,
    domains: list[str] | None = None,
    seed: int | None = None,
) -> list[BenchmarkResult]:
    """Generate high-performance data with ~85% success rate.

    Simulates a well-trained model with strong performance.

    Args:
        num_tasks: Number of tasks to generate.
        domains: List of domains to use (defaults to WAA_DOMAINS).
        seed: Random seed for reproducibility.

    Returns:
        List of BenchmarkResult with high success rates.
    """
    if seed is not None:
        random.seed(seed)
    if domains is None:
        domains = WAA_DOMAINS

    results = []
    tasks_per_domain = max(1, num_tasks // len(domains))

    for domain in domains:
        # Domain-specific boost (some domains are inherently easier)
        base_rate = DOMAIN_DIFFICULTY.get(domain, 0.20)
        domain_rate = min(0.95, 0.85 + (base_rate - 0.20) * 0.5)

        for i in range(tasks_per_domain):
            if len(results) >= num_tasks:
                break

            success = random.random() < domain_rate
            error = None if success else _weighted_choice(ERROR_TYPES)

            results.append(BenchmarkResult(
                task_id=f"{domain}_{i + 1}",
                success=success,
                score=random.uniform(0.8, 1.0) if success else random.uniform(0.3, 0.6),
                num_steps=random.randint(3, 8) if success else random.randint(8, 15),
                total_time_seconds=random.uniform(15, 45) if success else random.uniform(40, 90),
                error=error,
                reason=f"Best case model: {'completed efficiently' if success else error}",
            ))

    return results[:num_tasks]


def generate_worst_case_data(
    num_tasks: int = 154,
    domains: list[str] | None = None,
    seed: int | None = None,
) -> list[BenchmarkResult]:
    """Generate consistently failing data with ~5% success rate.

    Simulates a poorly performing or broken model.

    Args:
        num_tasks: Number of tasks to generate.
        domains: List of domains to use (defaults to WAA_DOMAINS).
        seed: Random seed for reproducibility.

    Returns:
        List of BenchmarkResult with very low success rates.
    """
    if seed is not None:
        random.seed(seed)
    if domains is None:
        domains = WAA_DOMAINS

    results = []
    tasks_per_domain = max(1, num_tasks // len(domains))

    for domain in domains:
        for i in range(tasks_per_domain):
            if len(results) >= num_tasks:
                break

            success = random.random() < 0.05  # 5% success rate
            error = None if success else _weighted_choice(ERROR_TYPES)

            results.append(BenchmarkResult(
                task_id=f"{domain}_{i + 1}",
                success=success,
                score=random.uniform(0.5, 0.8) if success else random.uniform(0, 0.2),
                num_steps=15,  # Always max out
                total_time_seconds=random.uniform(90, 180),
                error=error,
                reason=f"Worst case model: {'lucky success' if success else error}",
            ))

    return results[:num_tasks]


def generate_median_case_data(
    num_tasks: int = 154,
    domains: list[str] | None = None,
    seed: int | None = None,
) -> list[BenchmarkResult]:
    """Generate SOTA-like data with ~20% success rate and domain variation.

    Simulates current state-of-the-art performance on WAA benchmark,
    with realistic domain-specific success rate variation.

    Args:
        num_tasks: Number of tasks to generate.
        domains: List of domains to use (defaults to WAA_DOMAINS).
        seed: Random seed for reproducibility.

    Returns:
        List of BenchmarkResult with realistic SOTA-like performance.
    """
    if seed is not None:
        random.seed(seed)
    if domains is None:
        domains = WAA_DOMAINS

    results = []
    tasks_per_domain = max(1, num_tasks // len(domains))

    for domain in domains:
        domain_rate = DOMAIN_DIFFICULTY.get(domain, 0.20)

        for i in range(tasks_per_domain):
            if len(results) >= num_tasks:
                break

            success = random.random() < domain_rate
            error = None if success else _weighted_choice(ERROR_TYPES)

            # Realistic step distribution
            if success:
                steps = random.randint(4, 10)
                time_s = random.uniform(25, 60)
                score = random.uniform(0.7, 1.0)
            else:
                steps = random.randint(10, 15)
                time_s = random.uniform(50, 120)
                score = random.uniform(0.1, 0.4)

            results.append(BenchmarkResult(
                task_id=f"{domain}_{i + 1}",
                success=success,
                score=score,
                num_steps=steps,
                total_time_seconds=time_s,
                error=error,
                reason=f"Median case ({domain}): {'task completed' if success else error}",
            ))

    return results[:num_tasks]


@dataclass
class ScenarioConfig:
    """Configuration for a synthetic data scenario."""

    name: str
    description: str
    generator: Callable[..., list[BenchmarkResult]]
    expected_success_rate: float
    tags: list[str]


class Scenario(Enum):
    """Pre-defined synthetic data scenarios."""

    NOISE = ScenarioConfig(
        name="noise",
        description="Random baseline (~10% success)",
        generator=generate_noise_data,
        expected_success_rate=0.10,
        tags=["baseline", "random"],
    )
    BEST = ScenarioConfig(
        name="best",
        description="High performance (~85% success)",
        generator=generate_best_case_data,
        expected_success_rate=0.85,
        tags=["best-case", "high-performance"],
    )
    WORST = ScenarioConfig(
        name="worst",
        description="Consistently failing (~5% success)",
        generator=generate_worst_case_data,
        expected_success_rate=0.05,
        tags=["worst-case", "failing"],
    )
    MEDIAN = ScenarioConfig(
        name="median",
        description="SOTA-like performance (~20% success)",
        generator=generate_median_case_data,
        expected_success_rate=0.20,
        tags=["sota", "realistic"],
    )

    def generate(
        self,
        num_tasks: int = 154,
        domains: list[str] | None = None,
        seed: int | None = None,
    ) -> list[BenchmarkResult]:
        """Generate synthetic data for this scenario.

        Args:
            num_tasks: Number of tasks to generate.
            domains: List of domains to use.
            seed: Random seed for reproducibility.

        Returns:
            List of BenchmarkResult.
        """
        return self.value.generator(num_tasks=num_tasks, domains=domains, seed=seed)

    @property
    def config(self) -> ScenarioConfig:
        """Get the scenario configuration."""
        return self.value

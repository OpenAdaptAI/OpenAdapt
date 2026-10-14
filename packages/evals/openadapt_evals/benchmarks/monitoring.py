"""Cost tracking and monitoring for Azure evaluations.

This module provides cost calculation, tracking, and reporting for
Azure ML evaluations with support for tiered VMs and spot instances.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from openadapt_evals.benchmarks.azure import (
    VM_TIER_COSTS,
    VM_TIER_SPOT_COSTS,
)


@dataclass
class CostMetrics:
    """Cost metrics for a single worker or evaluation run."""

    vm_size: str
    vm_tier: str  # simple, medium, or complex
    is_spot: bool
    hourly_cost: float
    runtime_hours: float
    total_cost: float
    tasks_completed: int
    cost_per_task: float

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)


@dataclass
class EvaluationCostReport:
    """Cost report for an entire evaluation run."""

    run_id: str
    start_time: float
    end_time: float | None = None
    total_runtime_hours: float = 0.0
    total_cost: float = 0.0
    total_tasks: int = 0
    cost_per_task: float = 0.0
    workers: list[CostMetrics] = field(default_factory=list)
    # Cost breakdown
    simple_vm_hours: float = 0.0
    medium_vm_hours: float = 0.0
    complex_vm_hours: float = 0.0
    spot_savings: float = 0.0  # Amount saved by using spot instances
    tiered_savings: float = 0.0  # Amount saved by using tiered VMs vs all-medium

    def add_worker(self, metrics: CostMetrics) -> None:
        """Add worker cost metrics to the report."""
        self.workers.append(metrics)
        self.total_cost += metrics.total_cost
        self.total_tasks += metrics.tasks_completed
        self.total_runtime_hours += metrics.runtime_hours

        # Track VM hours by tier
        if metrics.vm_tier == "simple":
            self.simple_vm_hours += metrics.runtime_hours
        elif metrics.vm_tier == "medium":
            self.medium_vm_hours += metrics.runtime_hours
        elif metrics.vm_tier == "complex":
            self.complex_vm_hours += metrics.runtime_hours

        # Calculate savings
        if metrics.is_spot:
            # What would it have cost with regular instance?
            regular_cost = metrics.runtime_hours * VM_TIER_COSTS.get(metrics.vm_tier, 0.192)
            self.spot_savings += regular_cost - metrics.total_cost

    def finalize(self) -> None:
        """Finalize the report and calculate summary metrics."""
        if self.end_time is None:
            self.end_time = time.time()

        if self.total_tasks > 0:
            self.cost_per_task = self.total_cost / self.total_tasks

        # Calculate what it would have cost with all medium VMs
        baseline_cost = self.total_runtime_hours * VM_TIER_COSTS["medium"]
        self.tiered_savings = baseline_cost - self.total_cost - self.spot_savings

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "run_id": self.run_id,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "total_runtime_hours": round(self.total_runtime_hours, 2),
            "total_cost": round(self.total_cost, 2),
            "total_tasks": self.total_tasks,
            "cost_per_task": round(self.cost_per_task, 4),
            "vm_hours_by_tier": {
                "simple": round(self.simple_vm_hours, 2),
                "medium": round(self.medium_vm_hours, 2),
                "complex": round(self.complex_vm_hours, 2),
            },
            "savings": {
                "spot_savings": round(self.spot_savings, 2),
                "tiered_savings": round(self.tiered_savings, 2),
                "total_savings": round(self.spot_savings + self.tiered_savings, 2),
            },
            "workers": [w.to_dict() for w in self.workers],
        }

    def generate_summary(self) -> str:
        """Generate human-readable cost summary."""
        lines = [
            "=" * 60,
            f"Cost Report: {self.run_id}",
            "=" * 60,
            "",
            "Overall Metrics:",
            f"  Total Cost:        ${self.total_cost:.2f}",
            f"  Total Tasks:       {self.total_tasks}",
            f"  Cost per Task:     ${self.cost_per_task:.4f}",
            f"  Total Runtime:     {self.total_runtime_hours:.2f} hours",
            "",
            "VM Usage by Tier:",
            f"  Simple  (D2_v3):   {self.simple_vm_hours:.2f} hours",
            f"  Medium  (D4_v3):   {self.medium_vm_hours:.2f} hours",
            f"  Complex (D8_v3):   {self.complex_vm_hours:.2f} hours",
            "",
            "Cost Savings:",
            f"  Spot Instances:    ${self.spot_savings:.2f}",
            f"  Tiered VMs:        ${self.tiered_savings:.2f}",
            f"  Total Savings:     ${self.spot_savings + self.tiered_savings:.2f}",
            "",
        ]

        # Compare to baseline (all medium, no spot)
        baseline_cost = self.total_runtime_hours * VM_TIER_COSTS["medium"]
        if baseline_cost > 0:
            savings_pct = (baseline_cost - self.total_cost) / baseline_cost * 100
            lines.extend([
                "Baseline Comparison (all D4_v3, no spot):",
                f"  Baseline Cost:     ${baseline_cost:.2f}",
                f"  Actual Cost:       ${self.total_cost:.2f}",
                f"  Savings:           {savings_pct:.1f}%",
                "",
            ])

        lines.append("=" * 60)
        return "\n".join(lines)


class CostTracker:
    """Real-time cost tracking for live evaluations.

    This class tracks costs as the evaluation progresses and can
    generate live cost reports.
    """

    def __init__(
        self,
        run_id: str,
        output_file: str | Path = "cost_report.json",
    ):
        """Initialize cost tracker.

        Args:
            run_id: Unique identifier for this evaluation run.
            output_file: Path to write cost reports.
        """
        self.run_id = run_id
        self.output_file = Path(output_file)
        self.report = EvaluationCostReport(
            run_id=run_id,
            start_time=time.time(),
        )

    def record_worker_cost(
        self,
        worker_id: int,
        vm_size: str,
        vm_tier: str,
        is_spot: bool,
        hourly_cost: float,
        runtime_hours: float,
        tasks_completed: int,
    ) -> None:
        """Record cost for a completed worker.

        Args:
            worker_id: Worker identifier.
            vm_size: VM size used (e.g., "Standard_D4_v3").
            vm_tier: VM tier (simple, medium, complex).
            is_spot: Whether spot instance was used.
            hourly_cost: Hourly cost rate.
            runtime_hours: Total runtime in hours.
            tasks_completed: Number of tasks completed.
        """
        total_cost = runtime_hours * hourly_cost
        cost_per_task = total_cost / tasks_completed if tasks_completed > 0 else 0.0

        metrics = CostMetrics(
            vm_size=vm_size,
            vm_tier=vm_tier,
            is_spot=is_spot,
            hourly_cost=hourly_cost,
            runtime_hours=runtime_hours,
            total_cost=total_cost,
            tasks_completed=tasks_completed,
            cost_per_task=cost_per_task,
        )

        self.report.add_worker(metrics)
        self._write_report()

    def finalize(self) -> EvaluationCostReport:
        """Finalize the cost report.

        Returns:
            The completed cost report.
        """
        self.report.finalize()
        self._write_report()
        return self.report

    def _write_report(self) -> None:
        """Write current report to JSON file."""
        with open(self.output_file, "w") as f:
            json.dump(self.report.to_dict(), f, indent=2)

    def get_current_cost(self) -> float:
        """Get current total cost.

        Returns:
            Total cost so far.
        """
        return self.report.total_cost

    def get_estimated_final_cost(
        self,
        total_workers: int,
        avg_runtime_hours: float | None = None,
    ) -> float:
        """Estimate final cost based on current progress.

        Args:
            total_workers: Total number of workers in evaluation.
            avg_runtime_hours: Average runtime per worker (uses current avg if None).

        Returns:
            Estimated final cost.
        """
        if not self.report.workers:
            return 0.0

        # Use provided avg or calculate from completed workers
        if avg_runtime_hours is None:
            avg_runtime_hours = (
                sum(w.runtime_hours for w in self.report.workers) / len(self.report.workers)
            )

        avg_hourly_cost = (
            sum(w.hourly_cost for w in self.report.workers) / len(self.report.workers)
        )

        completed_workers = len(self.report.workers)
        remaining_workers = total_workers - completed_workers

        current_cost = self.report.total_cost
        estimated_remaining_cost = remaining_workers * avg_runtime_hours * avg_hourly_cost

        return current_cost + estimated_remaining_cost


def calculate_potential_savings(
    num_tasks: int,
    avg_task_duration_minutes: float = 1.0,
    num_workers: int = 1,
    enable_tiered_vms: bool = False,
    use_spot_instances: bool = False,
    task_complexity_distribution: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Calculate potential cost savings from optimization features.

    Args:
        num_tasks: Number of tasks to evaluate.
        avg_task_duration_minutes: Average duration per task.
        num_workers: Number of parallel workers.
        enable_tiered_vms: Whether to use tiered VM sizing.
        use_spot_instances: Whether to use spot instances.
        task_complexity_distribution: Distribution of task complexities
            (e.g., {"simple": 0.3, "medium": 0.5, "complex": 0.2}).
            If None, assumes all medium.

    Returns:
        Dictionary with cost breakdowns and savings estimates.
    """
    # Default: all tasks are medium complexity
    if task_complexity_distribution is None:
        task_complexity_distribution = {
            "simple": 0.0,
            "medium": 1.0,
            "complex": 0.0,
        }

    # Calculate total runtime
    tasks_per_worker = num_tasks / num_workers
    total_minutes = tasks_per_worker * avg_task_duration_minutes
    total_hours = total_minutes / 60
    overhead_hours = 0.25  # 15 minutes provisioning/cleanup
    runtime_hours_per_worker = total_hours + overhead_hours
    total_vm_hours = runtime_hours_per_worker * num_workers

    # Baseline: All medium VMs, no spot
    baseline_hourly_cost = VM_TIER_COSTS["medium"]
    baseline_cost = total_vm_hours * baseline_hourly_cost

    # Calculate cost with optimizations
    if enable_tiered_vms:
        # Weighted average cost based on task distribution
        weighted_cost = sum(
            VM_TIER_COSTS[tier] * ratio
            for tier, ratio in task_complexity_distribution.items()
        )
    else:
        weighted_cost = baseline_hourly_cost

    if use_spot_instances:
        if enable_tiered_vms:
            weighted_cost = sum(
                VM_TIER_SPOT_COSTS[tier] * ratio
                for tier, ratio in task_complexity_distribution.items()
            )
        else:
            weighted_cost = VM_TIER_SPOT_COSTS["medium"]

    optimized_cost = total_vm_hours * weighted_cost
    total_savings = baseline_cost - optimized_cost
    savings_pct = (total_savings / baseline_cost * 100) if baseline_cost > 0 else 0

    return {
        "num_tasks": num_tasks,
        "num_workers": num_workers,
        "runtime_hours_per_worker": round(runtime_hours_per_worker, 2),
        "total_vm_hours": round(total_vm_hours, 2),
        "baseline_cost": round(baseline_cost, 2),
        "optimized_cost": round(optimized_cost, 2),
        "total_savings": round(total_savings, 2),
        "savings_percentage": round(savings_pct, 1),
        "cost_per_task": round(optimized_cost / num_tasks, 4),
        "configuration": {
            "enable_tiered_vms": enable_tiered_vms,
            "use_spot_instances": use_spot_instances,
            "task_complexity_distribution": task_complexity_distribution,
        },
    }

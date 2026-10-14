"""Tests for Azure cost optimization features."""

from openadapt_evals.adapters import BenchmarkTask
from openadapt_evals.benchmarks.azure import (
    VM_TIER_COSTS,
    VM_TIER_SPOT_COSTS,
    VM_TIERS,
    classify_task_complexity,
    estimate_cost,
)
from openadapt_evals.benchmarks.monitoring import (
    CostMetrics,
    CostTracker,
    EvaluationCostReport,
    calculate_potential_savings,
)


def test_vm_tiers_defined():
    """Test that VM tier constants are properly defined."""
    assert "simple" in VM_TIERS
    assert "medium" in VM_TIERS
    assert "complex" in VM_TIERS

    assert VM_TIERS["simple"] == "Standard_D2_v3"
    assert VM_TIERS["medium"] == "Standard_D4_v3"
    assert VM_TIERS["complex"] == "Standard_D8_v3"


def test_vm_tier_costs():
    """Test that VM tier costs are reasonable."""
    # Simple should be cheapest
    assert VM_TIER_COSTS["simple"] < VM_TIER_COSTS["medium"]
    assert VM_TIER_COSTS["medium"] < VM_TIER_COSTS["complex"]

    # Spot costs should be significantly cheaper
    assert VM_TIER_SPOT_COSTS["simple"] < VM_TIER_COSTS["simple"]
    assert VM_TIER_SPOT_COSTS["medium"] < VM_TIER_COSTS["medium"]
    assert VM_TIER_SPOT_COSTS["complex"] < VM_TIER_COSTS["complex"]

    # Spot should be approximately 70-80% discount
    for tier in ["simple", "medium", "complex"]:
        discount = 1 - (VM_TIER_SPOT_COSTS[tier] / VM_TIER_COSTS[tier])
        assert 0.6 < discount < 0.9, f"Spot discount for {tier} should be 60-90%"


def test_classify_simple_tasks():
    """Test classification of simple tasks."""
    simple_tasks = [
        BenchmarkTask(task_id="notepad_1", instruction="Open Notepad", domain="notepad"),
        BenchmarkTask(task_id="calc_1", instruction="Open Calculator", domain="calculator"),
        BenchmarkTask(task_id="file_1", instruction="Create a new file", domain="file_explorer"),
        BenchmarkTask(task_id="paint_1", instruction="Open Paint", domain="paint"),
    ]

    for task in simple_tasks:
        tier = classify_task_complexity(task)
        assert tier == "simple", f"Task {task.task_id} should be classified as simple, got {tier}"


def test_classify_medium_tasks():
    """Test classification of medium tasks."""
    medium_tasks = [
        BenchmarkTask(task_id="browser_1", instruction="Open Chrome and navigate to google.com", domain="browser"),
        BenchmarkTask(task_id="word_1", instruction="Create a Word document", domain="office"),
        BenchmarkTask(task_id="excel_1", instruction="Open Excel", domain="office"),
        BenchmarkTask(task_id="email_1", instruction="Send an email", domain="outlook"),
    ]

    for task in medium_tasks:
        tier = classify_task_complexity(task)
        assert tier == "medium", f"Task {task.task_id} should be classified as medium, got {tier}"


def test_classify_complex_tasks():
    """Test classification of complex tasks."""
    complex_tasks = [
        BenchmarkTask(task_id="coding_1", instruction="Debug Python code in VS Code", domain="ide"),
        BenchmarkTask(task_id="git_1", instruction="Create a git commit", domain="terminal"),
        BenchmarkTask(task_id="excel_2", instruction="Create a pivot table with formulas", domain="office"),
        BenchmarkTask(task_id="multi_1", instruction="Switch between multiple applications", domain="multitasking"),
    ]

    for task in complex_tasks:
        tier = classify_task_complexity(task)
        assert tier == "complex", f"Task {task.task_id} should be classified as complex, got {tier}"


def test_estimate_cost_basic():
    """Test basic cost estimation."""
    estimate = estimate_cost(
        num_tasks=154,
        num_workers=10,
        avg_task_duration_minutes=1.0,
    )

    # Should return basic cost info
    assert estimate["num_tasks"] == 154
    assert estimate["num_workers"] == 10
    assert estimate["estimated_cost_usd"] > 0
    assert estimate["cost_per_task_usd"] > 0
    assert estimate["tasks_per_worker"] == 15.4
    assert estimate["total_vm_hours"] > 0


def test_estimate_cost_single_worker():
    """Test cost estimation with single worker."""
    estimate = estimate_cost(
        num_tasks=154,
        num_workers=1,
        avg_task_duration_minutes=1.0,
    )

    # Single worker should take longer but same total cost logic
    assert estimate["num_tasks"] == 154
    assert estimate["num_workers"] == 1
    assert estimate["estimated_cost_usd"] > 0
    assert estimate["tasks_per_worker"] == 154


def test_cost_tracker():
    """Test real-time cost tracking."""
    tracker = CostTracker(run_id="test-123")

    # Record some worker costs
    tracker.record_worker_cost(
        worker_id=0,
        vm_size="Standard_D2_v3",
        vm_tier="simple",
        is_spot=True,
        hourly_cost=0.024,
        runtime_hours=0.5,
        tasks_completed=15,
    )

    tracker.record_worker_cost(
        worker_id=1,
        vm_size="Standard_D4_v3",
        vm_tier="medium",
        is_spot=True,
        hourly_cost=0.048,
        runtime_hours=0.6,
        tasks_completed=15,
    )

    # Finalize report
    report = tracker.finalize()

    assert report.total_tasks == 30
    assert report.total_cost > 0
    assert len(report.workers) == 2
    assert report.cost_per_task == report.total_cost / report.total_tasks


def test_cost_report_summary():
    """Test cost report summary generation."""
    report = EvaluationCostReport(run_id="test-123", start_time=0.0)

    # Add some workers
    report.add_worker(
        CostMetrics(
            vm_size="Standard_D2_v3",
            vm_tier="simple",
            is_spot=True,
            hourly_cost=0.024,
            runtime_hours=0.5,
            total_cost=0.012,
            tasks_completed=15,
            cost_per_task=0.0008,
        )
    )

    report.add_worker(
        CostMetrics(
            vm_size="Standard_D4_v3",
            vm_tier="medium",
            is_spot=False,
            hourly_cost=0.192,
            runtime_hours=0.6,
            total_cost=0.1152,
            tasks_completed=15,
            cost_per_task=0.00768,
        )
    )

    report.finalize()

    # Generate summary
    summary = report.generate_summary()

    assert "Cost Report" in summary
    assert "test-123" in summary
    assert str(report.total_cost) in summary or f"{report.total_cost:.2f}" in summary
    assert "VM Usage by Tier" in summary


def test_calculate_potential_savings():
    """Test potential savings calculation."""
    savings = calculate_potential_savings(
        num_tasks=154,
        num_workers=10,
        avg_task_duration_minutes=1.0,
        enable_tiered_vms=True,
        use_spot_instances=True,
        task_complexity_distribution={
            "simple": 0.3,
            "medium": 0.5,
            "complex": 0.2,
        },
    )

    assert savings["optimized_cost"] < savings["baseline_cost"]
    assert savings["savings_percentage"] > 50
    assert savings["cost_per_task"] > 0


def test_target_cost_with_optimizations():
    """Test that target cost range is achievable with optimizations.

    Uses calculate_potential_savings which has full optimization support.
    """
    # Full optimization scenario using the monitoring function
    savings = calculate_potential_savings(
        num_tasks=154,
        num_workers=10,
        avg_task_duration_minutes=1.0,
        enable_tiered_vms=True,
        use_spot_instances=True,
        task_complexity_distribution={
            "simple": 0.3,
            "medium": 0.5,
            "complex": 0.2,
        },
    )

    # Should have significant savings with optimizations
    assert savings["optimized_cost"] < savings["baseline_cost"]
    assert savings["savings_percentage"] > 50


if __name__ == "__main__":
    # Run all tests
    print("Running cost optimization tests...")

    test_vm_tiers_defined()
    print("✓ VM tiers defined correctly")

    test_vm_tier_costs()
    print("✓ VM tier costs are reasonable")

    test_classify_simple_tasks()
    print("✓ Simple task classification works")

    test_classify_medium_tasks()
    print("✓ Medium task classification works")

    test_classify_complex_tasks()
    print("✓ Complex task classification works")

    test_estimate_cost_basic()
    print("✓ Basic cost estimation works")

    test_estimate_cost_single_worker()
    print("✓ Single worker cost estimation works")

    test_cost_tracker()
    print("✓ Cost tracker works")

    test_cost_report_summary()
    print("✓ Cost report summary generation works")

    test_calculate_potential_savings()
    print("✓ Savings calculation works")

    test_target_cost_with_optimizations()
    print("✓ Target cost with optimizations works")

    print("\n" + "="*50)
    print("All tests passed! ✓")
    print("="*50)

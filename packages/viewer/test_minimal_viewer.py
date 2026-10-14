#!/usr/bin/env python3
"""Test script for minimal benchmark viewer.

Tests:
1. Viewer HTML loads correctly
2. Generator works with real data
3. API endpoints would work (structure check)
"""

import json
import sys
from pathlib import Path


def test_viewer_html():
    """Test that viewer HTML file exists and is valid."""
    viewer_path = Path(__file__).parent / "viewers" / "benchmark" / "minimal_viewer.html"
    assert viewer_path.exists(), f"Viewer not found: {viewer_path}"

    html = viewer_path.read_text()

    # Check for key components
    assert "benchmarkViewer()" in html, "Alpine.js component missing"
    assert "/api/benchmark/runs" in html, "API endpoint missing"
    assert "window.BENCHMARK_DATA" in html, "Data placeholder missing"
    assert "alpinejs" in html, "Alpine.js script missing"

    # Check line count
    line_count = len(html.splitlines())
    print(f"✓ Viewer HTML: {line_count} lines")
    assert line_count < 650, f"Viewer too large: {line_count} lines (target: <650)"

    print("✓ Viewer HTML structure valid")


def test_generator():
    """Test that generator works with real benchmark data."""
    from viewers.benchmark.generator import generate_from_benchmark_results

    # Find a real benchmark run
    results_dir = Path(__file__).parent.parent / "openadapt-ml" / "benchmark_results"
    if not results_dir.exists():
        print("⚠ No benchmark_results directory, skipping generator test")
        return

    # Find first run
    runs = [d for d in results_dir.iterdir() if d.is_dir() and (d / "summary.json").exists()]
    if not runs:
        print("⚠ No benchmark runs found, skipping generator test")
        return

    run_name = runs[0].name
    output_path = Path("/tmp/test_minimal_viewer.html")

    # Generate viewer
    result = generate_from_benchmark_results(results_dir, run_name, output_path)
    assert result.exists(), "Generated HTML not found"

    # Verify embedded data
    html = result.read_text()
    assert "window.BENCHMARK_DATA = {" in html, "Data not embedded"
    assert run_name in html, "Run name not embedded"

    print(f"✓ Generator works with run: {run_name}")
    print(f"  Output: {output_path}")


def test_data_structure():
    """Test that real benchmark data matches expected structure."""
    results_dir = Path(__file__).parent.parent / "openadapt-ml" / "benchmark_results"
    if not results_dir.exists():
        print("⚠ No benchmark_results directory, skipping data structure test")
        return

    # Find first run
    runs = [d for d in results_dir.iterdir() if d.is_dir() and (d / "summary.json").exists()]
    if not runs:
        print("⚠ No benchmark runs found, skipping data structure test")
        return

    run_dir = runs[0]

    # Check summary.json
    summary_path = run_dir / "summary.json"
    summary = json.loads(summary_path.read_text())

    required_fields = ["run_name", "num_tasks", "success_rate", "avg_steps", "tasks"]
    for field in required_fields:
        assert field in summary, f"Missing field in summary: {field}"

    # Check task structure
    if summary["tasks"]:
        task = summary["tasks"][0]
        task_required = ["task_id", "success", "num_steps"]
        for field in task_required:
            assert field in task, f"Missing field in task: {field}"

    # Check execution.json
    task_id = summary["tasks"][0]["task_id"]
    execution_path = run_dir / "tasks" / task_id / "execution.json"
    if execution_path.exists():
        execution = json.loads(execution_path.read_text())
        assert "steps" in execution, "Missing steps in execution"
        if execution["steps"]:
            step = execution["steps"][0]
            assert "action" in step, "Missing action in step"
            assert "screenshot_path" in step, "Missing screenshot_path in step"

    print(f"✓ Data structure valid for run: {run_dir.name}")
    print(f"  Tasks: {summary['num_tasks']}")
    print(f"  Success rate: {summary['success_rate']:.1%}")


if __name__ == "__main__":
    print("\n=== Testing Minimal Benchmark Viewer ===\n")

    try:
        test_viewer_html()
        test_generator()
        test_data_structure()
        print("\n✅ All tests passed!")
    except AssertionError as e:
        print(f"\n❌ Test failed: {e}")
        # sys.exit, not the builtin `exit`: `exit` is injected by the `site`
        # module and is absent under `python -S` and in frozen builds, where
        # this script would have died with NameError instead of exiting 1.
        sys.exit(1)
    except Exception as e:  # noqa: BLE001
        # Top of a standalone script: report and exit 1 rather than crash.
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

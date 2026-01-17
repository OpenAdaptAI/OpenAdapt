#!/usr/bin/env python3
"""End-to-end example: Capture -> Train -> Evaluate.

This example demonstrates the full OpenAdapt workflow:
1. Record a GUI interaction demo
2. Convert capture to training format
3. Train a model (or use mock training)
4. Evaluate on benchmarks

Prerequisites:
    pip install openadapt

Usage:
    python examples/capture_train_eval.py

Note:
    This example uses mock/stub modes to run without GPU or external services.
    For real training and evaluation, see the CLI commands.
"""

from pathlib import Path
import tempfile
import time

# Import from the unified openadapt package
from openadapt import (
    # Capture
    CaptureSession,
    load_capture,
    process_events,
    get_action_events,
    # Evaluation
    SmartMockAgent,
    WAAMockAdapter,
    evaluate_agent_on_benchmark,
    compute_metrics,
    # Viewer
    PageBuilder,
    metrics_grid,
)


def main():
    """Run the complete capture -> train -> eval workflow."""
    print("=" * 60)
    print("OpenAdapt: Capture -> Train -> Evaluate Example")
    print("=" * 60)

    # =========================================================================
    # Step 1: Simulate a capture session
    # =========================================================================
    print("\n1. Capturing GUI interactions...")
    print("-" * 40)

    # In a real scenario, you would use:
    #   with CaptureSession(name="my-task") as session:
    #       # Perform task...
    #
    # For this example, we'll simulate capture data
    print("   (Simulating capture - in real use, perform GUI actions here)")

    # Create mock capture data
    mock_capture = {
        "name": "example-task",
        "events": [
            {"type": "mouse_click", "x": 100, "y": 200, "timestamp": 1.0},
            {"type": "key_type", "text": "hello world", "timestamp": 2.0},
            {"type": "mouse_click", "x": 300, "y": 400, "timestamp": 3.0},
        ],
        "duration": 3.5,
    }

    print(f"   Captured {len(mock_capture['events'])} events in {mock_capture['duration']}s")

    # =========================================================================
    # Step 2: Convert to training format (simulated)
    # =========================================================================
    print("\n2. Converting capture to training format...")
    print("-" * 40)

    # In a real scenario:
    #   from openadapt import CaptureConverter
    #   converter = CaptureConverter()
    #   episode = converter.convert("./captures/my-task")

    print("   (Simulating conversion - in real use, this creates Episode objects)")

    mock_episode = {
        "task": "Example task",
        "steps": [
            {"observation": "screenshot_1.png", "action": "click(100, 200)"},
            {"observation": "screenshot_2.png", "action": "type('hello world')"},
            {"observation": "screenshot_3.png", "action": "click(300, 400)"},
        ],
    }
    print(f"   Created episode with {len(mock_episode['steps'])} steps")

    # =========================================================================
    # Step 3: Train model (simulated with mock)
    # =========================================================================
    print("\n3. Training model...")
    print("-" * 40)

    # In a real scenario:
    #   from openadapt import train_supervised, TrainingConfig
    #   config = TrainingConfig(model_name="Qwen/Qwen3-VL-2B-Instruct", epochs=3)
    #   state = train_supervised(episode, config)

    print("   (Simulating training - in real use, run: openadapt train --stub)")

    # Simulate training progress
    for epoch in range(1, 4):
        print(f"   Epoch {epoch}/3: loss=0.{10-epoch*3}42")
        time.sleep(0.3)  # Simulate training time

    print("   Training complete!")

    # =========================================================================
    # Step 4: Evaluate on benchmark
    # =========================================================================
    print("\n4. Evaluating on benchmark...")
    print("-" * 40)

    # Create a mock agent (simulates model predictions)
    agent = SmartMockAgent(success_rate=0.7)

    # Create mock benchmark adapter (no VM required)
    adapter = WAAMockAdapter(num_tasks=5)

    print(f"   Running evaluation on {adapter.get_task_count()} tasks...")

    # Run evaluation
    results = evaluate_agent_on_benchmark(
        agent=agent,
        adapter=adapter,
        max_steps=10,
        verbose=True,
    )

    # =========================================================================
    # Step 5: Compute and display metrics
    # =========================================================================
    print("\n5. Computing metrics...")
    print("-" * 40)

    metrics = compute_metrics(results)

    print(f"   Total tasks: {metrics['total_tasks']}")
    print(f"   Passed: {metrics['passed_tasks']}")
    print(f"   Failed: {metrics['failed_tasks']}")
    print(f"   Success rate: {metrics['success_rate']:.1%}")
    print(f"   Average steps: {metrics.get('avg_steps', 'N/A')}")

    # =========================================================================
    # Step 6: Generate viewer (demonstration)
    # =========================================================================
    print("\n6. Generating results viewer...")
    print("-" * 40)

    # Use openadapt-viewer components to create a summary
    builder = PageBuilder(title="Evaluation Results")

    builder.add_header(
        title="Benchmark Evaluation Results",
        subtitle=f"Model: mock-agent | Tasks: {metrics['total_tasks']}"
    )

    builder.add_section(
        metrics_grid([
            {"label": "Total Tasks", "value": metrics["total_tasks"]},
            {"label": "Passed", "value": metrics["passed_tasks"], "color": "success"},
            {"label": "Failed", "value": metrics["failed_tasks"], "color": "error"},
            {"label": "Success Rate", "value": f"{metrics['success_rate']:.1%}"},
        ]),
        title="Summary"
    )

    # In a real scenario, save the viewer:
    #   output_path = builder.render_to_file("results.html")
    #   print(f"   Viewer saved to: {output_path}")

    print("   (Viewer HTML generated - in real use, this opens in browser)")

    # =========================================================================
    # Summary
    # =========================================================================
    print("\n" + "=" * 60)
    print("Workflow Complete!")
    print("=" * 60)
    print("""
Next steps:
    1. Record a real capture:
       openadapt capture start --name my-task

    2. Train on your capture:
       openadapt train --capture ./captures/my-task

    3. Evaluate on Windows Agent Arena:
       openadapt eval run --benchmark waa --server http://vm:5000

    4. View results:
       openadapt eval view --run-name my_eval
""")


if __name__ == "__main__":
    main()

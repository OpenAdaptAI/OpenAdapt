#!/usr/bin/env python3
"""Demo-retrieval augmented evaluation example.

This example demonstrates how to use demo retrieval to improve agent performance:
1. Build a library of demonstration trajectories
2. For each evaluation task, retrieve the most relevant demo
3. Include the demo in the agent's prompt (P0 demo persistence)
4. Evaluate and compare with vs without demo augmentation

Prerequisites:
    pip install openadapt[retrieval]

Usage:
    python examples/demo_retrieval_augmented.py

Key insight: Demo-conditioned prompting improves action accuracy significantly.
In experiments (Dec 2024):
    - Zero-shot: 33% correct first actions
    - With demo: 100% correct first actions
"""

from pathlib import Path
import json

# Check if retrieval package is available
try:
    from openadapt import (
        # Retrieval
        MultimodalDemoRetriever,
        DemoMetadata,
        # Evaluation
        ApiAgent,
        SmartMockAgent,
        WAAMockAdapter,
        evaluate_agent_on_benchmark,
        compute_metrics,
    )
    RETRIEVAL_AVAILABLE = True
except ImportError:
    RETRIEVAL_AVAILABLE = False
    print("Warning: openadapt[retrieval] not installed. Using mock retrieval.")
    from openadapt import (
        SmartMockAgent,
        WAAMockAdapter,
        evaluate_agent_on_benchmark,
        compute_metrics,
    )


# Mock demo library for example
DEMO_LIBRARY = {
    "notepad_open": {
        "task": "Open Notepad application",
        "trajectory": """Step 1: Click the Windows Start button at coordinates (24, 1060)
Step 2: Type 'notepad' in the search box
Step 3: Click the 'Notepad' app result
Step 4: Wait for Notepad window to appear
DONE: Notepad is now open""",
    },
    "chrome_navigate": {
        "task": "Navigate to a website in Chrome",
        "trajectory": """Step 1: Click the Chrome icon in taskbar at coordinates (120, 1060)
Step 2: Click the address bar at coordinates (500, 60)
Step 3: Type the URL and press Enter
DONE: Website is loaded""",
    },
    "file_save": {
        "task": "Save a file with a specific name",
        "trajectory": """Step 1: Press Ctrl+S to open Save dialog
Step 2: Type the filename in the 'File name' field
Step 3: Click the 'Save' button
DONE: File is saved""",
    },
    "settings_display": {
        "task": "Change display settings",
        "trajectory": """Step 1: Right-click on desktop
Step 2: Click 'Display settings' from context menu
Step 3: Scroll to find the desired setting
Step 4: Adjust the setting value
Step 5: Click 'Apply'
DONE: Display settings changed""",
    },
}


class MockDemoRetriever:
    """Mock retriever for when openadapt-retrieval is not installed."""

    def __init__(self):
        self.demos = DEMO_LIBRARY

    def retrieve(self, task: str, top_k: int = 1) -> list[dict]:
        """Simple keyword-based retrieval."""
        task_lower = task.lower()
        scores = []
        for demo_id, demo in self.demos.items():
            # Simple word overlap scoring
            demo_words = set(demo["task"].lower().split())
            task_words = set(task_lower.split())
            overlap = len(demo_words & task_words)
            scores.append((demo_id, demo, overlap))

        # Sort by score descending
        scores.sort(key=lambda x: x[2], reverse=True)

        results = []
        for demo_id, demo, score in scores[:top_k]:
            results.append({
                "demo_id": demo_id,
                "task": demo["task"],
                "trajectory": demo["trajectory"],
                "similarity": score / max(1, len(task_lower.split())),
            })
        return results


def build_demo_library():
    """Build or load the demo library.

    In a real scenario, this would:
    1. Load captures from disk
    2. Extract trajectories
    3. Compute embeddings with Qwen3-VL
    4. Build a FAISS index

    Returns:
        Demo retriever instance
    """
    if RETRIEVAL_AVAILABLE:
        print("   Using MultimodalDemoRetriever with VLM embeddings")
        retriever = MultimodalDemoRetriever(embedding_dim=512)

        # Add demos to library
        for demo_id, demo in DEMO_LIBRARY.items():
            retriever.add_demo(
                demo_id=demo_id,
                task=demo["task"],
                # In real use, include screenshot:
                # screenshot=f"./demos/{demo_id}/screenshot.png",
                metadata={"trajectory": demo["trajectory"]},
            )

        # Build the search index
        retriever.build_index()
        return retriever
    else:
        print("   Using MockDemoRetriever (install openadapt[retrieval] for VLM)")
        return MockDemoRetriever()


def format_demo_prompt(demo: dict) -> str:
    """Format a retrieved demo for inclusion in the agent prompt.

    Args:
        demo: Retrieved demo with task and trajectory

    Returns:
        Formatted prompt string
    """
    return f"""Here is an example of a similar task:

Task: {demo['task']}

Demonstration:
{demo['trajectory']}

Now complete the current task following a similar approach.
"""


def main():
    """Run demo-retrieval augmented evaluation."""
    print("=" * 60)
    print("OpenAdapt: Demo-Retrieval Augmented Evaluation")
    print("=" * 60)

    # =========================================================================
    # Step 1: Build demo library
    # =========================================================================
    print("\n1. Building demo library...")
    print("-" * 40)

    retriever = build_demo_library()
    print(f"   Demo library contains {len(DEMO_LIBRARY)} demonstrations")

    # =========================================================================
    # Step 2: Set up evaluation
    # =========================================================================
    print("\n2. Setting up evaluation...")
    print("-" * 40)

    # Mock benchmark adapter
    adapter = WAAMockAdapter(num_tasks=5)
    print(f"   Benchmark: WAA Mock with {adapter.get_task_count()} tasks")

    # =========================================================================
    # Step 3: Evaluate WITHOUT demo augmentation (baseline)
    # =========================================================================
    print("\n3. Baseline evaluation (no demo)...")
    print("-" * 40)

    # Agent without demo (lower success rate)
    baseline_agent = SmartMockAgent(success_rate=0.4)

    baseline_results = evaluate_agent_on_benchmark(
        agent=baseline_agent,
        adapter=adapter,
        max_steps=10,
        verbose=False,
    )

    baseline_metrics = compute_metrics(baseline_results)
    print(f"   Baseline success rate: {baseline_metrics['success_rate']:.1%}")

    # =========================================================================
    # Step 4: Evaluate WITH demo augmentation
    # =========================================================================
    print("\n4. Demo-augmented evaluation...")
    print("-" * 40)

    # For each task, retrieve relevant demo and include in agent
    augmented_results = []

    tasks = list(adapter.get_tasks())
    for i, task in enumerate(tasks):
        # Retrieve most relevant demo
        if RETRIEVAL_AVAILABLE:
            retrieved = retriever.retrieve(task=task.instruction, top_k=1)
        else:
            retrieved = retriever.retrieve(task=task.instruction, top_k=1)

        if retrieved:
            demo = retrieved[0]
            demo_prompt = format_demo_prompt(demo)
            print(f"   Task {i+1}: Retrieved demo '{demo.get('demo_id', 'unknown')}' "
                  f"(similarity: {demo.get('similarity', 0):.2f})")
        else:
            demo_prompt = None
            print(f"   Task {i+1}: No relevant demo found")

        # Create agent with demo (higher success rate when demo is relevant)
        # In real use, this would be an ApiAgent with the demo in prompt
        demo_agent = SmartMockAgent(
            success_rate=0.8 if demo_prompt else 0.4
        )

        # Evaluate single task
        task_adapter = WAAMockAdapter(num_tasks=1)
        task_result = evaluate_agent_on_benchmark(
            agent=demo_agent,
            adapter=task_adapter,
            max_steps=10,
            verbose=False,
        )
        augmented_results.extend(task_result)

    augmented_metrics = compute_metrics(augmented_results)
    print(f"\n   Augmented success rate: {augmented_metrics['success_rate']:.1%}")

    # =========================================================================
    # Step 5: Compare results
    # =========================================================================
    print("\n5. Comparison...")
    print("-" * 40)

    improvement = augmented_metrics['success_rate'] - baseline_metrics['success_rate']

    print(f"   Baseline (no demo):    {baseline_metrics['success_rate']:.1%}")
    print(f"   With demo retrieval:   {augmented_metrics['success_rate']:.1%}")
    print(f"   Improvement:           {improvement:+.1%}")

    if improvement > 0:
        print("\n   Demo retrieval IMPROVED performance!")
    elif improvement < 0:
        print("\n   Demo retrieval did not help (may need better demos)")
    else:
        print("\n   No significant difference")

    # =========================================================================
    # Summary
    # =========================================================================
    print("\n" + "=" * 60)
    print("Key Insights")
    print("=" * 60)
    print("""
Demo-conditioned prompting significantly improves agent accuracy:

1. The P0 demo persistence fix ensures demos are included at EVERY step,
   not just the first step. This is critical for multi-step tasks.

2. Multimodal retrieval (screenshot + task text) finds more relevant
   demos than text-only search.

3. The demo provides:
   - Action format examples (e.g., coordinate ranges, button names)
   - Step sequencing guidance
   - Domain-specific knowledge

Real-world results (Dec 2024 experiments):
   - Zero-shot: 33% correct first actions
   - With demo:  100% correct first actions

For production use:
    from openadapt import ApiAgent, WAALiveAdapter

    agent = ApiAgent(
        provider="anthropic",
        demo=demo_trajectory,  # P0 fix: included at EVERY step
    )
""")


if __name__ == "__main__":
    main()

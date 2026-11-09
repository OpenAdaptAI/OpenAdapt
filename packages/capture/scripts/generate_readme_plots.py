#!/usr/bin/env python3
"""Generate example plots for README documentation.

This script generates synthetic data to create illustrative plots showing:
1. Performance statistics (event latency over time)
2. Video vs image frame comparison (diff metrics)

Usage:
    uv run python scripts/generate_readme_plots.py
"""

from pathlib import Path

# Ensure matplotlib uses non-interactive backend
import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from openadapt_capture.comparison import ComparisonReport, FrameComparison
from openadapt_capture.stats import CaptureStats, PerfStat


def generate_performance_plot(output_dir: Path) -> None:
    """Generate a synthetic performance statistics plot."""
    print("Generating performance statistics plot...")

    stats = CaptureStats()

    # Generate realistic synthetic data
    # Simulate a 60-second capture with various event types
    np.random.seed(42)

    # Mouse events - more frequent, typically low latency
    for i in range(200):
        event_time = i * 0.3 + np.random.uniform(-0.05, 0.05)
        latency = np.random.exponential(0.005) + 0.002  # 2-15ms typically
        stats.stats.append(
            PerfStat(
                event_type="mouse.move" if i % 3 else "mouse.down",
                event_timestamp=event_time,
                write_timestamp=event_time + latency,
            )
        )

    # Keyboard events - less frequent
    for i in range(50):
        event_time = i * 1.2 + np.random.uniform(-0.1, 0.1)
        latency = np.random.exponential(0.003) + 0.001  # 1-8ms typically
        stats.stats.append(
            PerfStat(
                event_type="key.down",
                event_timestamp=event_time,
                write_timestamp=event_time + latency,
            )
        )

    # Screen frames - regular interval
    for i in range(60 * 24):  # 24 fps for 60 seconds
        event_time = i / 24.0
        latency = np.random.exponential(0.015) + 0.005  # 5-30ms typically
        stats.stats.append(
            PerfStat(
                event_type="screen.frame",
                event_timestamp=event_time,
                write_timestamp=event_time + latency,
            )
        )

    # Sort by timestamp
    stats.stats.sort(key=lambda s: s.event_timestamp)

    # Create plot
    fig, ax = plt.subplots(figsize=(12, 6))

    # Group by event type
    event_types = {}
    for stat in stats.stats:
        if stat.event_type not in event_types:
            event_types[stat.event_type] = {"ts": [], "lat": []}
        event_types[stat.event_type]["ts"].append(stat.event_timestamp)
        event_types[stat.event_type]["lat"].append(stat.latency * 1000)

    # Define colors and markers for each type
    styles = {
        "mouse.move": {"color": "#3498db", "marker": "o", "alpha": 0.5, "s": 10},
        "mouse.down": {"color": "#e74c3c", "marker": "s", "alpha": 0.7, "s": 30},
        "key.down": {"color": "#2ecc71", "marker": "D", "alpha": 0.7, "s": 30},
        "screen.frame": {"color": "#9b59b6", "marker": "^", "alpha": 0.4, "s": 15},
    }

    for event_type, data in event_types.items():
        style = styles.get(event_type, {"color": "gray", "marker": ".", "alpha": 0.5, "s": 10})
        ax.scatter(
            data["ts"],
            data["lat"],
            label=event_type,
            c=style["color"],
            marker=style["marker"],
            alpha=style["alpha"],
            s=style["s"],
        )

    ax.set_xlabel("Event Timestamp (s)", fontsize=12)
    ax.set_ylabel("Write Latency (ms)", fontsize=12)
    ax.set_title("Capture Performance: Event Write Latency Over Time", fontsize=14)
    ax.legend(loc="upper right", fontsize=10)
    ax.grid(True, alpha=0.3)
    ax.set_ylim(0, 60)  # Cap at 60ms for clarity

    # Add summary stats
    summary = stats.summary()
    summary_text = (
        f"Total Events: {summary['total_events']} | "
        f"Mean Latency: {summary['mean_latency_ms']:.1f}ms | "
        f"Max Latency: {summary['max_latency_ms']:.1f}ms"
    )
    ax.text(
        0.5,
        -0.12,
        summary_text,
        transform=ax.transAxes,
        ha="center",
        fontsize=10,
        color="gray",
    )

    plt.tight_layout()
    output_path = output_dir / "performance_stats.png"
    plt.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Saved: {output_path}")


def generate_comparison_plot(output_dir: Path) -> None:
    """Generate a synthetic video vs image comparison plot."""
    print("Generating frame comparison plot...")

    np.random.seed(123)

    # Generate synthetic comparison data
    # Simulate comparing 100 frames from a capture
    comparisons = []
    for i in range(100):
        timestamp = i * 0.5  # 2 fps for 50 seconds

        # Simulate near-lossless encoding with occasional minor artifacts
        mean_diff = np.random.exponential(0.3) + 0.1  # Very small differences
        max_diff = mean_diff * (2 + np.random.exponential(1))
        psnr = 45 + np.random.normal(0, 3)  # High PSNR (good quality)

        comparisons.append(
            FrameComparison(
                timestamp=timestamp,
                mean_diff=mean_diff,
                max_diff=max_diff,
                psnr=psnr,
            )
        )

    report = ComparisonReport(comparisons=comparisons)

    # Create plot
    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True)

    timestamps = [c.timestamp for c in comparisons]
    mean_diffs = [c.mean_diff for c in comparisons]
    max_diffs = [c.max_diff for c in comparisons]
    psnrs = [c.psnr for c in comparisons]

    # Plot differences
    ax1 = axes[0]
    ax1.plot(timestamps, mean_diffs, "b-", label="Mean Pixel Diff", alpha=0.7, linewidth=1)
    ax1.plot(timestamps, max_diffs, "r-", label="Max Pixel Diff", alpha=0.5, linewidth=1)
    ax1.axhline(y=1.0, color="g", linestyle="--", label="Lossless Threshold", alpha=0.7)
    ax1.set_ylabel("Pixel Difference (0-255)", fontsize=11)
    ax1.set_title("Video Frame Extraction Accuracy", fontsize=14)
    ax1.legend(loc="upper right", fontsize=9)
    ax1.grid(True, alpha=0.3)
    ax1.set_ylim(0, max(max_diffs) * 1.2)

    # Plot PSNR
    ax2 = axes[1]
    ax2.plot(timestamps, psnrs, "g-", label="PSNR", alpha=0.7, linewidth=1)
    ax2.axhline(y=40, color="orange", linestyle="--", label="Good Quality Threshold", alpha=0.7)
    ax2.set_xlabel("Timestamp (s)", fontsize=11)
    ax2.set_ylabel("PSNR (dB)", fontsize=11)
    ax2.legend(loc="lower right", fontsize=9)
    ax2.grid(True, alpha=0.3)

    # Add summary text
    summary = report.summary()
    summary_text = (
        f"Frames Compared: {summary['num_frames']} | "
        f"Mean Diff: {summary['mean_diff']:.2f} | "
        f"Mean PSNR: {summary['mean_psnr']:.1f} dB | "
        f"Quality: {'Lossless' if summary['is_lossless'] else 'Lossy'}"
    )
    fig.text(0.5, 0.02, summary_text, ha="center", fontsize=10, color="gray")

    plt.tight_layout(rect=[0, 0.05, 1, 1])
    output_path = output_dir / "frame_comparison.png"
    plt.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Saved: {output_path}")


def main() -> None:
    """Generate all plots for README."""
    # Create output directory
    output_dir = Path(__file__).parent.parent / "docs" / "images"
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Generating plots in {output_dir}...")
    print()

    generate_performance_plot(output_dir)
    generate_comparison_plot(output_dir)

    print()
    print("Done! Add these to README.md:")
    print()
    print("```markdown")
    print("![Performance Statistics](docs/images/performance_stats.png)")
    print("![Frame Comparison](docs/images/frame_comparison.png)")
    print("```")


if __name__ == "__main__":
    main()

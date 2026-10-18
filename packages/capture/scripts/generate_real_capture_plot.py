#!/usr/bin/env python3
"""Generate performance plot from a real capture.

This script performs an actual capture for a few seconds and generates
an authentic performance plot from the recorded data.

Usage:
    uv run python scripts/generate_real_capture_plot.py

Note: Requires display access for screen capture.
"""

import shutil
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openadapt_capture.stats import CaptureStats

# Set matplotlib backend before importing pyplot
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


def run_real_capture(duration: float = 5.0) -> "CaptureStats":
    """Run a real capture and return the stats.

    Args:
        duration: How long to capture in seconds.

    Returns:
        CaptureStats from the recording.
    """
    from openadapt_capture import Recorder

    # Use a temp directory for the capture
    capture_dir = Path(tempfile.mkdtemp(prefix="capture_perf_"))

    print(f"Starting {duration}s capture to {capture_dir}...")
    print("(Move your mouse and type to generate events - if accessibility is enabled)")
    print()

    try:
        with Recorder(
            capture_dir,
            task_description="Performance plot generation",
            capture_video=True,
            capture_mouse_moves=True,
        ) as recorder:
            start = time.time()
            while time.time() - start < duration:
                elapsed = time.time() - start
                remaining = duration - elapsed
                # Count stats instead of events (stats includes screen frames)
                stat_count = len(recorder.stats.stats)
                print(f"\r  Recording... {remaining:.1f}s remaining, {stat_count} stats recorded", end="", flush=True)
                time.sleep(0.1)
            print()

        stat_count = len(recorder.stats.stats)
        print(f"Captured {recorder.event_count} input events, {stat_count} total stats")
        return recorder.stats, capture_dir

    except Exception:
        # Clean up on error
        shutil.rmtree(capture_dir, ignore_errors=True)
        raise


def generate_performance_plot(stats: "CaptureStats", output_path: Path) -> None:
    """Generate performance plot from capture stats.

    Args:
        stats: CaptureStats from a real capture.
        output_path: Where to save the plot.
    """

    if not stats.stats:
        print("No stats recorded!")
        return

    # Group by event type
    event_types: dict[str, tuple[list[float], list[float]]] = {}
    for stat in stats.stats:
        if stat.event_type not in event_types:
            event_types[stat.event_type] = ([], [])
        timestamps, latencies = event_types[stat.event_type]
        timestamps.append(stat.event_timestamp)
        latencies.append(stat.latency * 1000)  # Convert to ms

    # Create plot
    fig, ax = plt.subplots(figsize=(12, 6))

    # Define styles for each event type
    styles = {
        "mouse.move": {"color": "#3498db", "marker": "o", "alpha": 0.4, "s": 15, "label": "mouse.move"},
        "mouse.down": {"color": "#e74c3c", "marker": "s", "alpha": 0.8, "s": 40, "label": "mouse.down"},
        "mouse.up": {"color": "#c0392b", "marker": "s", "alpha": 0.6, "s": 30, "label": "mouse.up"},
        "mouse.scroll": {"color": "#9b59b6", "marker": "D", "alpha": 0.7, "s": 35, "label": "mouse.scroll"},
        "key.down": {"color": "#2ecc71", "marker": "^", "alpha": 0.8, "s": 40, "label": "key.down"},
        "key.up": {"color": "#27ae60", "marker": "v", "alpha": 0.6, "s": 30, "label": "key.up"},
        "screen.frame": {"color": "#f39c12", "marker": ".", "alpha": 0.5, "s": 20, "label": "screen.frame"},
    }
    default_style = {"color": "gray", "marker": "x", "alpha": 0.5, "s": 20}

    # Normalize timestamps to start from 0
    if stats.stats:
        min_ts = min(s.event_timestamp for s in stats.stats)
    else:
        min_ts = 0

    for event_type, (timestamps, latencies) in event_types.items():
        style = styles.get(event_type, {**default_style, "label": event_type})
        normalized_ts = [t - min_ts for t in timestamps]
        ax.scatter(
            normalized_ts,
            latencies,
            c=style["color"],
            marker=style["marker"],
            alpha=style["alpha"],
            s=style["s"],
            label=style["label"],
        )

    ax.set_xlabel("Time (s)", fontsize=12)
    ax.set_ylabel("Write Latency (ms)", fontsize=12)
    ax.set_title("Capture Performance: Event Write Latency", fontsize=14)
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(True, alpha=0.3)

    # Add summary stats
    summary = stats.summary()
    summary_text = (
        f"Events: {summary['total_events']} | "
        f"Mean: {summary['mean_latency_ms']:.2f}ms | "
        f"Max: {summary['max_latency_ms']:.2f}ms"
    )
    ax.text(
        0.5, -0.1,
        summary_text,
        transform=ax.transAxes,
        ha="center",
        fontsize=10,
        color="gray",
    )

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close()

    print(f"Saved plot to {output_path}")


def main() -> None:
    """Run capture and generate plot."""
    import argparse

    parser = argparse.ArgumentParser(description="Generate real capture performance plot")
    parser.add_argument(
        "--duration", "-d",
        type=float,
        default=5.0,
        help="Capture duration in seconds (default: 5)",
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default=None,
        help="Output path for plot (default: docs/images/performance_stats.png)",
    )
    args = parser.parse_args()

    # Determine output path
    script_dir = Path(__file__).parent
    project_dir = script_dir.parent
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = project_dir / "docs" / "images" / "performance_stats.png"

    output_path.parent.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("Real Capture Performance Plot Generator")
    print("=" * 60)
    print()

    # Run capture
    stats, capture_dir = run_real_capture(duration=args.duration)

    print()
    print("Generating plot...")
    generate_performance_plot(stats, output_path)

    # Print summary
    print()
    print("Summary:")
    summary = stats.summary()
    print(f"  Total events: {summary['total_events']}")
    print(f"  Mean latency: {summary['mean_latency_ms']:.2f}ms")
    print(f"  Max latency: {summary['max_latency_ms']:.2f}ms")
    if "by_type" in summary:
        print("  By type:")
        for event_type, type_stats in summary["by_type"].items():
            print(f"    {event_type}: {type_stats['count']} events, mean {type_stats['mean_latency_ms']:.2f}ms")

    # Clean up capture directory
    print()
    print(f"Cleaning up {capture_dir}...")
    shutil.rmtree(capture_dir, ignore_errors=True)

    print()
    print(f"Done! Plot saved to: {output_path}")


if __name__ == "__main__":
    main()

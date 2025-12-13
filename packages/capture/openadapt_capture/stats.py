"""Performance statistics collection and plotting.

Track and visualize capture performance metrics like event processing
latency and memory usage.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from PIL import Image


@dataclass
class PerfStat:
    """A single performance measurement."""

    event_type: str
    event_timestamp: float  # When the event occurred
    write_timestamp: float  # When the event was written to storage

    @property
    def latency(self) -> float:
        """Time between event occurrence and storage write."""
        return self.write_timestamp - self.event_timestamp


@dataclass
class CaptureStats:
    """Collects performance statistics during capture.

    Usage:
        stats = CaptureStats()

        # Record event write times
        stats.record_event("mouse.down", event_timestamp=1.0)

        # After capture, analyze or plot
        stats.summary()
        stats.plot("performance.png")
    """

    stats: list[PerfStat] = field(default_factory=list)
    _start_time: float | None = None

    def start(self) -> None:
        """Mark capture start time."""
        self._start_time = time.time()

    def record_event(self, event_type: str, event_timestamp: float) -> None:
        """Record when an event was written to storage.

        Args:
            event_type: Type of event (e.g., "mouse.down").
            event_timestamp: Original timestamp of the event.
        """
        self.stats.append(
            PerfStat(
                event_type=event_type,
                event_timestamp=event_timestamp,
                write_timestamp=time.time(),
            )
        )

    def summary(self) -> dict[str, Any]:
        """Get summary statistics.

        Returns:
            Dict with counts, latency stats per event type.
        """
        if not self.stats:
            return {"total_events": 0}

        by_type: dict[str, list[float]] = defaultdict(list)
        for stat in self.stats:
            by_type[stat.event_type].append(stat.latency)

        result = {
            "total_events": len(self.stats),
            "by_type": {},
        }

        for event_type, latencies in by_type.items():
            result["by_type"][event_type] = {
                "count": len(latencies),
                "mean_latency_ms": sum(latencies) / len(latencies) * 1000,
                "max_latency_ms": max(latencies) * 1000,
                "min_latency_ms": min(latencies) * 1000,
            }

        # Overall stats
        all_latencies = [s.latency for s in self.stats]
        result["mean_latency_ms"] = sum(all_latencies) / len(all_latencies) * 1000
        result["max_latency_ms"] = max(all_latencies) * 1000

        return result

    def plot(
        self,
        output_path: str | Path | None = None,
        show: bool = False,
        title: str | None = None,
    ) -> "Image" | None:
        """Plot performance statistics.

        Args:
            output_path: Path to save the plot image.
            show: Whether to display the plot interactively.
            title: Optional title for the plot.

        Returns:
            PIL Image if neither output_path nor show, else None.
        """
        try:
            import matplotlib.pyplot as plt
        except ImportError:
            raise ImportError(
                "matplotlib is required for plotting. Install with: uv add matplotlib"
            )

        if not self.stats:
            return None

        # Group by event type
        by_type: dict[str, tuple[list[float], list[float]]] = defaultdict(
            lambda: ([], [])
        )
        for stat in self.stats:
            timestamps, latencies = by_type[stat.event_type]
            timestamps.append(stat.event_timestamp)
            latencies.append(stat.latency * 1000)  # Convert to ms

        # Create plot
        fig, ax = plt.subplots(figsize=(12, 6))

        markers = ["o", "s", "D", "^", "v", ">", "<", "p", "*", "h"]
        for i, (event_type, (timestamps, latencies)) in enumerate(by_type.items()):
            marker = markers[i % len(markers)]
            ax.scatter(timestamps, latencies, label=event_type, marker=marker, alpha=0.7)

        ax.set_xlabel("Event Timestamp (s)")
        ax.set_ylabel("Write Latency (ms)")
        ax.set_title(title or "Capture Performance")
        ax.legend(loc="upper right")
        ax.grid(True, alpha=0.3)

        if output_path:
            plt.savefig(output_path, dpi=150, bbox_inches="tight")
            plt.close()
            return None
        elif show:
            plt.show()
            plt.close()
            return None
        else:
            # Return as PIL Image
            from io import BytesIO

            from PIL import Image

            buf = BytesIO()
            plt.savefig(buf, format="png", dpi=150, bbox_inches="tight")
            plt.close()
            buf.seek(0)
            return Image.open(buf)


def plot_capture_performance(
    capture_dir: str | Path,
    output_path: str | Path | None = None,
    show: bool = False,
) -> "Image" | None:
    """Plot performance stats from a saved capture.

    Args:
        capture_dir: Path to capture directory.
        output_path: Path to save plot image.
        show: Whether to display interactively.

    Returns:
        PIL Image if neither output_path nor show.
    """
    from openadapt_capture.storage import load_capture

    capture, storage = load_capture(capture_dir)

    # Get all events and calculate write latencies
    # Note: This is an approximation since we don't store actual write times
    # In a full implementation, we'd store perf stats in the DB
    events = storage.get_events()
    storage.close()

    if not events:
        return None

    stats = CaptureStats()
    for event in events:
        # Approximate: assume events were written immediately
        stats.stats.append(
            PerfStat(
                event_type=event.type if isinstance(event.type, str) else event.type.value,
                event_timestamp=event.timestamp,
                write_timestamp=event.timestamp,  # Approximation
            )
        )

    return stats.plot(output_path=output_path, show=show, title=f"Capture: {capture.id}")

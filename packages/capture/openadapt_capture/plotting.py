"""Plotting utilities for performance visualization.

Copied from legacy OpenAdapt plotting.py — only the plot_performance function
and its dependencies. Import paths adapted for openadapt-capture.
"""

import os
import sys
from collections import defaultdict
from itertools import cycle

import matplotlib.pyplot as plt
from loguru import logger

from openadapt_capture.db import models


def plot_performance(
    session,
    recording: models.Recording | None = None,
    perf_stats=None,
    mem_stats=None,
    view_file: bool = False,
    save_file: bool = True,
    save_dir: str | None = None,
    dark_mode: bool = False,
) -> str | None:
    """Plot the performance of the event processing and writing.

    Args:
        session: SQLAlchemy session.
        recording: The Recording whose performance to plot.
        perf_stats: List of PerformanceStat objects (if None, queries from DB).
        mem_stats: List of MemoryStat objects (if None, queries from DB).
        view_file: Whether to view the file after saving it.
        save_file: Whether to save the file.
        save_dir: Directory to save plots. Defaults to capture dir.
        dark_mode: Whether to use dark mode.

    Returns:
        str | None: Path to saved plot file, if saved.
    """
    type_to_proc_times = defaultdict(list)
    type_to_timestamps = defaultdict(list)

    if dark_mode:
        plt.style.use("dark_background")

    if perf_stats is None:
        perf_stats = (
            session.query(models.PerformanceStat)
            .filter(models.PerformanceStat.recording_id == recording.id)
            .order_by(models.PerformanceStat.start_time)
            .all()
        )

    for perf_stat in perf_stats:
        event_type = perf_stat.event_type
        start_time = perf_stat.start_time
        end_time = perf_stat.end_time
        type_to_proc_times[event_type].append(end_time - start_time)
        type_to_timestamps[event_type].append(start_time)

    fig, ax = plt.subplots(1, 1, figsize=(20, 10))

    # Define markers to distinguish different event types
    markers = [
        "o",
        "s",
        "D",
        "^",
        "v",
        ">",
        "<",
        "p",
        "*",
        "h",
        "H",
        "+",
        "x",
        "X",
        "d",
        "|",
        "_",
    ]
    marker_cycle = cycle(markers)

    for event_type in type_to_proc_times:
        x = type_to_timestamps[event_type]
        y = type_to_proc_times[event_type]
        ax.scatter(x, y, label=event_type, marker=next(marker_cycle))

    ax.legend()
    ax.set_ylabel("Duration (seconds)")

    if mem_stats is None:
        mem_stats = (
            session.query(models.MemoryStat)
            .filter(models.MemoryStat.recording_id == recording.id)
            .order_by(models.MemoryStat.timestamp)
            .all()
        )

    timestamps = []
    mem_usages = []
    for mem_stat in mem_stats:
        mem_usages.append(mem_stat.memory_usage_bytes)
        timestamps.append(mem_stat.timestamp)

    memory_ax = ax.twinx()
    memory_ax.plot(
        timestamps,
        mem_usages,
        label="memory usage",
        color="red",
    )
    memory_ax.set_ylabel("Memory Usage (bytes)")

    if len(mem_usages) > 0:
        handles1, labels1 = ax.get_legend_handles_labels()
        handles2, labels2 = memory_ax.get_legend_handles_labels()

        all_handles = handles1 + handles2
        all_labels = labels1 + labels2

        ax.legend(all_handles, all_labels)

    if recording:
        ax.set_title(f"{recording.timestamp=}")

    if save_file:
        fname_parts = ["performance"]
        if recording:
            fname_parts.append(str(recording.timestamp))
        fname = "-".join(fname_parts) + ".png"
        if save_dir is None:
            save_dir = os.getcwd()
        os.makedirs(save_dir, exist_ok=True)
        fpath = os.path.join(save_dir, fname)
        logger.info(f"{fpath=}")
        plt.savefig(fpath)
        if view_file:
            if sys.platform == "darwin":
                os.system(f"open {fpath}")
            elif sys.platform == "win32":
                os.system(f"start {fpath}")
            else:
                os.system(f"xdg-open {fpath}")
        return fpath
    else:
        if view_file:
            plt.show()
        else:
            plt.close()
        return None

"""Performance test for openadapt-capture recorder.

Runs a short recording with synthetic input (pynput Controllers), then
loads the capture and prints a summary. Generates performance plots if
PLOT_PERFORMANCE is enabled.

Usage:
    cd /Users/abrichr/oa/src/openadapt-capture
    uv run python scripts/perf_test.py
"""

import json
import os
import sys
import threading
import time
from pathlib import Path

import psutil

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))


def memory_sampler(pid, interval, samples, stop_event):
    """Sample memory usage of process and its children at regular intervals."""
    proc = psutil.Process(pid)
    while not stop_event.is_set():
        try:
            main_rss = proc.memory_info().rss / (1024 * 1024)  # MB
            children = proc.children(recursive=True)
            child_rss = sum(
                c.memory_info().rss / (1024 * 1024) for c in children
            )
            samples.append({
                "time": time.time(),
                "main_rss_mb": main_rss,
                "child_rss_mb": child_rss,
                "total_rss_mb": main_rss + child_rss,
                "num_children": len(children),
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        stop_event.wait(interval)


def generate_synthetic_input(duration, stop_event):
    """Generate synthetic mouse/keyboard input using pynput Controllers.

    Args:
        duration: How long to generate input (seconds).
        stop_event: Event to signal early stop.
    """
    from pynput.mouse import Controller as MouseController
    from pynput.keyboard import Controller as KeyboardController, Key

    mouse = MouseController()
    keyboard = KeyboardController()

    start = time.time()
    i = 0
    while time.time() - start < duration and not stop_event.is_set():
        # Move mouse in a small pattern
        x_offset = (i % 10) * 10
        y_offset = (i % 5) * 10
        mouse.position = (100 + x_offset, 100 + y_offset)
        time.sleep(0.05)

        # Click every 10th iteration
        if i % 10 == 0:
            mouse.click(mouse.Button.left if hasattr(mouse, 'Button') else None)
            time.sleep(0.05)

        # Type a character every 20th iteration
        if i % 20 == 0:
            keyboard.press('a')
            keyboard.release('a')
            time.sleep(0.05)

        i += 1

    print(f"  Generated {i} synthetic input cycles")


def main():
    from openadapt_capture.recorder import Recorder

    capture_dir = Path("/tmp/openadapt_perf_test")
    if capture_dir.exists():
        import shutil
        shutil.rmtree(capture_dir)

    duration = 10  # seconds
    print("=== openadapt-capture Performance Test ===")
    print(f"Duration: {duration}s")
    print(f"Output: {capture_dir}")
    print()

    # Track memory
    memory_samples = []
    mem_stop = threading.Event()
    mem_thread = threading.Thread(
        target=memory_sampler,
        args=(os.getpid(), 0.25, memory_samples, mem_stop),
        daemon=True,
    )

    # Record timestamps for CPU tracking
    t_start = time.time()
    cpu_start = time.process_time()

    mem_thread.start()

    print("Starting recording...")
    input_stop = threading.Event()

    with Recorder(str(capture_dir), task_description="Performance test") as recorder:
        t_recording_started = time.time()
        print(f"  Recorder started in: {t_recording_started - t_start:.3f}s")
        print(f"  Generating synthetic input for {duration}s...")
        print()

        # Generate synthetic input in a separate thread
        input_thread = threading.Thread(
            target=generate_synthetic_input,
            args=(duration, input_stop),
            daemon=True,
        )
        input_thread.start()

        # Wait for duration
        time.sleep(duration)
        input_stop.set()
        input_thread.join(timeout=5)

        print("Stopping recording...")
        t_stop_start = time.time()

    t_stop_end = time.time()
    print(f"  Recorder.stop() took: {t_stop_end - t_stop_start:.3f}s")
    print()

    mem_stop.set()
    mem_thread.join(timeout=2)

    cpu_end = time.process_time()
    t_end = time.time()

    # === Report ===
    wall_time = t_end - t_start
    cpu_time = cpu_end - cpu_start

    print("=" * 60)
    print("PERFORMANCE REPORT")
    print("=" * 60)
    print()

    # Timing
    print(f"Wall time:     {wall_time:.2f}s")
    print(f"CPU time:      {cpu_time:.2f}s")
    print(f"CPU usage:     {cpu_time / wall_time * 100:.1f}%")
    print()

    # Memory
    if memory_samples:
        main_rss = [s["main_rss_mb"] for s in memory_samples]
        child_rss = [s["child_rss_mb"] for s in memory_samples]
        total_rss = [s["total_rss_mb"] for s in memory_samples]
        print("Memory (current RSS via psutil):")
        print(f"  Main process:")
        print(f"    Start:  {main_rss[0]:.1f} MB")
        print(f"    End:    {main_rss[-1]:.1f} MB")
        print(f"    Peak:   {max(main_rss):.1f} MB")
        print(f"    Growth: {main_rss[-1] - main_rss[0]:.1f} MB")
        print(f"  Child processes:")
        print(f"    Peak:   {max(child_rss):.1f} MB")
        print(f"  Total (main + children):")
        print(f"    Peak:   {max(total_rss):.1f} MB")
        print()

    # File sizes
    print("Output files:")
    for f in sorted(capture_dir.rglob("*")):
        if f.is_file():
            size_mb = f.stat().st_size / (1024 * 1024)
            print(f"  {f.name}: {size_mb:.2f} MB")
    print()

    # Try loading the capture
    print("Loading capture...")
    try:
        from openadapt_capture.capture import CaptureSession
        capture = CaptureSession.load(str(capture_dir))
        actions = list(capture.actions())
        raw = capture.raw_events()
        print(f"  Recording ID: {capture.id}")
        print(f"  Platform: {capture.platform}")
        print(f"  Screen size: {capture.screen_size}")
        print(f"  Raw events: {len(raw)}")
        print(f"  Processed actions: {len(actions)}")
        if actions:
            from collections import Counter
            types = Counter(a.type for a in actions)
            print("  Action types:")
            for etype, count in types.most_common():
                print(f"    {etype}: {count}")
        capture.close()
    except Exception as e:
        print(f"  Failed to load capture: {e}")
    print()

    # Generate memory plot
    if memory_samples:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            t0 = memory_samples[0]["time"]
            times = [s["time"] - t0 for s in memory_samples]

            fig, ax = plt.subplots(figsize=(12, 5))
            ax.plot(times, main_rss, "b-", linewidth=2, label="Main process")
            ax.plot(times, child_rss, "r-", linewidth=2, label="Child processes")
            ax.plot(times, total_rss, "k--", linewidth=1, label="Total")
            ax.set_xlabel("Time (s)")
            ax.set_ylabel("RSS (MB)")
            ax.set_title("Memory Usage During Recording (psutil)")
            ax.legend()
            ax.grid(True, alpha=0.3)

            mem_plot_path = capture_dir / "memory_plot.png"
            plt.savefig(str(mem_plot_path), dpi=150, bbox_inches="tight")
            plt.close()
            print(f"Memory plot saved: {mem_plot_path}")
        except Exception as e:
            print(f"Failed to generate memory plot: {e}")

    # Save raw data as JSON
    report = {
        "wall_time_s": wall_time,
        "cpu_time_s": cpu_time,
        "cpu_percent": cpu_time / wall_time * 100,
        "duration_s": duration,
        "memory_samples": memory_samples,
    }
    report_path = capture_dir / "perf_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"Raw report saved: {report_path}")
    print()

    # Open plots on macOS
    if sys.platform == "darwin":
        mem_plot_path = capture_dir / "memory_plot.png"
        if mem_plot_path.exists():
            os.system(f"open {mem_plot_path}")


if __name__ == "__main__":
    main()

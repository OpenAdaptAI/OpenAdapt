"""Side-by-side benchmark: legacy OpenAdapt vs new openadapt-capture recording patterns.

Extracts the core screenshot capture + video encoding loops from both codebases
and runs them in identical conditions for a fair comparison.

Usage:
    cd /Users/abrichr/oa/src/openadapt-capture
    uv run python scripts/legacy_vs_new_benchmark.py
"""

import multiprocessing
import os
import queue
import signal
import sys
import threading
import time
from collections import namedtuple
from pathlib import Path

import mss
import mss.base
import psutil
from PIL import Image

if sys.platform == "win32":
    import mss.windows
    mss.windows.CAPTUREBLT = 0

# ===================================================================
# Legacy Pattern (from OpenAdapt/legacy/openadapt/record.py)
# ===================================================================

Event = namedtuple("Event", ("timestamp", "type", "data"))


def _legacy_take_screenshot(sct, monitor):
    """Matches legacy utils.take_screenshot()."""
    sct_img = sct.grab(monitor)
    return Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")


def _legacy_read_screen_events(event_q, terminate_processing):
    """Legacy read_screen_events thread — captures screenshots into event_q."""
    sct = mss.mss()
    monitor = sct.monitors[0]
    while not terminate_processing.is_set():
        screenshot = _legacy_take_screenshot(sct, monitor)
        event_q.put(Event(time.time(), "screen", screenshot))


def _legacy_process_events(
    event_q, video_write_q, terminate_processing,
    record_full_video=False,
):
    """Legacy process_events thread — routes screen events to video queue.

    In action-gated mode (record_full_video=False):
      - stores prev_screen_event
      - (action events would trigger writing prev_screen_event to video_write_q)
      - For this benchmark, we simulate action-gated by writing every Nth screen event

    In full video mode (record_full_video=True):
      - every screen event goes to video_write_q
    """
    prev_screen_event = None
    prev_saved_screen_timestamp = 0
    frame_count = 0

    while not terminate_processing.is_set() or not event_q.empty():
        try:
            event = event_q.get(timeout=0.05)
        except queue.Empty:
            continue

        if event.type == "screen":
            prev_screen_event = event
            frame_count += 1

            if record_full_video:
                # Full video mode: every frame goes to encoder
                video_event = event._replace(type="screen/video")
                video_write_q.put(video_event)
            else:
                # Action-gated: simulate ~5 actions/sec (every ~5th frame at 24fps)
                if frame_count % 5 == 0 and prev_saved_screen_timestamp < prev_screen_event.timestamp:
                    video_event = prev_screen_event._replace(type="screen/video")
                    video_write_q.put(video_event)
                    prev_saved_screen_timestamp = prev_screen_event.timestamp


def _legacy_video_writer(video_write_q, video_path, width, height, fps, terminate_processing):
    """Legacy video writer process — encodes frames from queue."""
    import av

    signal.signal(signal.SIGINT, signal.SIG_IGN)

    container = av.open(str(video_path), mode="w")
    stream = container.add_stream("libx264", rate=fps)
    stream.width = width
    stream.height = height
    stream.pix_fmt = "yuv444p"
    stream.options = {"crf": "0", "preset": "veryslow"}

    start_ts = None
    last_pts = 0
    last_frame = None
    last_frame_ts = None

    while not terminate_processing.is_set() or not video_write_q.empty():
        try:
            event = video_write_q.get(timeout=0.1)
        except Exception:
            continue

        screenshot_image = event.data
        screenshot_ts = event.timestamp

        if start_ts is None:
            start_ts = screenshot_ts

        av_frame = av.VideoFrame.from_image(screenshot_image)

        force_key_frame = last_pts == 0
        if force_key_frame:
            av_frame.pict_type = av.video.frame.PictureType.I

        time_diff = screenshot_ts - start_ts
        pts = int(time_diff * fps)
        if pts <= last_pts:
            pts = last_pts + 1
        av_frame.pts = pts
        last_pts = pts

        for packet in stream.encode(av_frame):
            packet.pts = pts
            container.mux(packet)

        last_frame = screenshot_image
        last_frame_ts = screenshot_ts

    # Finalize (matches legacy video.finalize_video_writer)
    if last_frame and last_frame_ts and start_ts:
        av_frame = av.VideoFrame.from_image(last_frame)
        av_frame.pict_type = av.video.frame.PictureType.I
        time_diff = last_frame_ts - start_ts
        pts = int(time_diff * fps)
        if pts <= last_pts:
            pts = last_pts + 1
        av_frame.pts = pts
        for packet in stream.encode(av_frame):
            packet.pts = pts
            container.mux(packet)

    for packet in stream.encode():
        container.mux(packet)

    close_thread = threading.Thread(target=container.close)
    close_thread.start()
    close_thread.join()


def run_legacy_benchmark(output_dir, duration, record_full_video=False):
    """Run the legacy recording pattern."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Get screen dims
    sct = mss.mss()
    monitor = sct.monitors[0]
    sct_img = sct.grab(monitor)
    width, height = sct_img.size
    del sct

    event_q = queue.Queue()
    video_write_q = multiprocessing.Queue()
    terminate_processing = multiprocessing.Event()

    video_path = output_dir / "video.mp4"

    # Start video writer process
    video_proc = multiprocessing.Process(
        target=_legacy_video_writer,
        args=(video_write_q, str(video_path), width, height, 24, terminate_processing),
    )
    video_proc.start()

    # Start process_events thread
    process_thread = threading.Thread(
        target=_legacy_process_events,
        args=(event_q, video_write_q, terminate_processing, record_full_video),
    )
    process_thread.start()

    # Start screen reader thread
    screen_thread = threading.Thread(
        target=_legacy_read_screen_events,
        args=(event_q, terminate_processing),
    )
    screen_thread.start()

    # Wait
    time.sleep(duration)

    # Shutdown (legacy pattern: set terminate, then join)
    terminate_processing.set()
    screen_thread.join(timeout=5)
    process_thread.join(timeout=5)
    video_proc.join(timeout=30)
    if video_proc.is_alive():
        video_proc.terminate()


# ===================================================================
# New Pattern (from openadapt-capture)
# ===================================================================

def _new_video_writer_worker(q, video_path, width, height, fps):
    """New video encoder process — matches recorder.py _video_writer_worker."""
    import av

    signal.signal(signal.SIGINT, signal.SIG_IGN)

    container = av.open(str(video_path), mode="w")
    stream = container.add_stream("libx264", rate=fps)
    stream.width = width
    stream.height = height
    stream.pix_fmt = "yuv444p"
    stream.options = {"crf": "0", "preset": "veryslow"}

    start_ts = None
    last_pts = -1
    last_frame = None
    last_frame_ts = None
    is_first = True

    while True:
        item = q.get()
        if item is None:
            break

        image_bytes, size, timestamp = item
        image = Image.frombytes("RGB", size, image_bytes)

        if start_ts is None:
            start_ts = timestamp

        av_frame = av.VideoFrame.from_image(image)
        if is_first:
            av_frame.pict_type = av.video.frame.PictureType.I
            is_first = False

        time_diff = timestamp - start_ts
        pts = int(time_diff * fps)
        if pts <= last_pts:
            pts = last_pts + 1
        av_frame.pts = pts
        last_pts = pts

        for packet in stream.encode(av_frame):
            packet.pts = pts
            container.mux(packet)

        last_frame = image
        last_frame_ts = timestamp

    # Finalize
    if last_frame and last_frame_ts and start_ts:
        av_frame = av.VideoFrame.from_image(last_frame)
        av_frame.pict_type = av.video.frame.PictureType.I
        time_diff = last_frame_ts - start_ts
        pts = int(time_diff * fps)
        if pts <= last_pts:
            pts = last_pts + 1
        av_frame.pts = pts
        for packet in stream.encode(av_frame):
            packet.pts = pts
            container.mux(packet)

    for packet in stream.encode():
        container.mux(packet)

    close_thread = threading.Thread(target=container.close)
    close_thread.start()
    close_thread.join()


def run_new_benchmark(output_dir, duration, record_full_video=False):
    """Run the new openadapt-capture recording pattern."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Get screen dims
    sct_init = mss.mss()
    monitor = sct_init.monitors[0]
    sct_img = sct_init.grab(monitor)
    width, height = sct_img.size
    del sct_init

    video_path = output_dir / "video.mp4"
    video_q = multiprocessing.Queue()

    # Start video writer process
    video_proc = multiprocessing.Process(
        target=_new_video_writer_worker,
        args=(video_q, str(video_path), width, height, 24),
        daemon=False,
    )
    video_proc.start()

    # Action-gated state
    prev_screen_image = None
    prev_screen_timestamp = 0.0
    prev_saved_screen_timestamp = 0.0
    frame_count = 0
    stop_event = threading.Event()

    def on_screen_frame(image, timestamp):
        nonlocal prev_screen_image, prev_screen_timestamp, frame_count
        if record_full_video:
            video_q.put((image.tobytes(), image.size, timestamp))
        else:
            prev_screen_image = image
            prev_screen_timestamp = timestamp
        frame_count += 1

    def simulate_actions():
        """Simulate action-gated frame writes at ~5 actions/sec."""
        nonlocal prev_saved_screen_timestamp
        while not stop_event.is_set():
            if (
                not record_full_video
                and prev_screen_image is not None
                and prev_screen_timestamp > prev_saved_screen_timestamp
            ):
                image = prev_screen_image
                video_q.put((image.tobytes(), image.size, prev_screen_timestamp))
                prev_saved_screen_timestamp = prev_screen_timestamp
            stop_event.wait(0.2)  # ~5 actions/sec

    def capture_loop():
        """Screenshot capture thread — matches ScreenCapturer._capture_loop."""
        sct = mss.mss()
        mon = sct.monitors[0]
        interval = 1.0 / 24.0
        while not stop_event.is_set():
            ts = time.time()
            sct_img = sct.grab(mon)
            screenshot = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
            on_screen_frame(screenshot, ts)
            elapsed = time.time() - ts
            sleep_time = max(0, interval - elapsed)
            if sleep_time > 0:
                stop_event.wait(sleep_time)

    # Start threads
    capture_thread = threading.Thread(target=capture_loop, daemon=True)
    action_thread = threading.Thread(target=simulate_actions, daemon=True)
    capture_thread.start()
    action_thread.start()

    # Wait
    time.sleep(duration)

    # Shutdown (new pattern: set stop, sentinel, join)
    stop_event.set()
    capture_thread.join(timeout=2)
    action_thread.join(timeout=2)
    video_q.put(None)  # Sentinel
    video_proc.join(timeout=30)
    if video_proc.is_alive():
        video_proc.terminate()


# ===================================================================
# Benchmark Runner
# ===================================================================

def sample_memory(pid, interval, samples, stop_event):
    proc = psutil.Process(pid)
    while not stop_event.is_set():
        try:
            main_rss = proc.memory_info().rss / (1024 * 1024)
            children = proc.children(recursive=True)
            child_rss = sum(c.memory_info().rss / (1024 * 1024) for c in children)
            samples.append({
                "time": time.time(),
                "main_rss_mb": main_rss,
                "child_rss_mb": child_rss,
                "total_rss_mb": main_rss + child_rss,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        stop_event.wait(interval)


def run_benchmark(name, run_fn, output_dir, duration):
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")

    mem_samples = []
    mem_stop = threading.Event()
    mem_thread = threading.Thread(
        target=sample_memory,
        args=(os.getpid(), 0.25, mem_samples, mem_stop),
        daemon=True,
    )

    t_start = time.time()
    cpu_start = time.process_time()
    mem_thread.start()

    run_fn(output_dir, duration)

    cpu_end = time.process_time()
    t_end = time.time()
    mem_stop.set()
    mem_thread.join(timeout=2)

    wall = t_end - t_start
    cpu = cpu_end - cpu_start

    print(f"  Wall time:   {wall:.2f}s")
    print(f"  CPU time:    {cpu:.2f}s")
    print(f"  CPU usage:   {cpu / wall * 100:.1f}%")

    if mem_samples:
        main_rss = [s["main_rss_mb"] for s in mem_samples]
        child_rss = [s["child_rss_mb"] for s in mem_samples]
        total_rss = [s["total_rss_mb"] for s in mem_samples]
        print(f"  Main RSS:    {main_rss[0]:.0f} → {main_rss[-1]:.0f} MB (peak {max(main_rss):.0f})")
        print(f"  Child RSS:   {child_rss[0]:.0f} → {child_rss[-1]:.0f} MB (peak {max(child_rss):.0f})")
        print(f"  Total RSS:   {total_rss[0]:.0f} → {total_rss[-1]:.0f} MB (peak {max(total_rss):.0f})")

    # File sizes
    od = Path(output_dir)
    for f in sorted(od.rglob("*")):
        if f.is_file():
            print(f"  {f.name}: {f.stat().st_size / 1024 / 1024:.2f} MB")

    return mem_samples


def main():
    import matplotlib.pyplot as plt

    base_dir = Path("/tmp/openadapt_benchmark")
    if base_dir.exists():
        import shutil
        shutil.rmtree(base_dir)

    duration = 15

    print(f"Benchmark: {duration}s recording, action-gated mode (~5 fps to encoder)")
    print(f"Each test uses identical video encoding: libx264/yuv444p/crf=0/veryslow")

    # Run legacy pattern
    legacy_samples = run_benchmark(
        "LEGACY PATTERN (event_q → process_events → video_write_q → writer process)",
        lambda od, d: run_legacy_benchmark(od, d, record_full_video=False),
        base_dir / "legacy",
        duration,
    )

    # Force GC between tests
    import gc
    gc.collect()
    time.sleep(2)

    # Run new pattern
    new_samples = run_benchmark(
        "NEW PATTERN (callback → buffer image → action → tobytes → queue → writer process)",
        lambda od, d: run_new_benchmark(od, d, record_full_video=False),
        base_dir / "new",
        duration,
    )

    # Plot comparison
    if legacy_samples and new_samples:
        fig, axes = plt.subplots(1, 2, figsize=(16, 6))

        # Main process memory
        ax = axes[0]
        t0_l = legacy_samples[0]["time"]
        t0_n = new_samples[0]["time"]
        ax.plot(
            [s["time"] - t0_l for s in legacy_samples],
            [s["main_rss_mb"] for s in legacy_samples],
            "b-", linewidth=2, label="Legacy (main)",
        )
        ax.plot(
            [s["time"] - t0_n for s in new_samples],
            [s["main_rss_mb"] for s in new_samples],
            "r-", linewidth=2, label="New (main)",
        )
        ax.set_xlabel("Time (s)")
        ax.set_ylabel("RSS (MB)")
        ax.set_title("Main Process Memory")
        ax.legend()
        ax.grid(True, alpha=0.3)

        # Total memory
        ax = axes[1]
        ax.plot(
            [s["time"] - t0_l for s in legacy_samples],
            [s["total_rss_mb"] for s in legacy_samples],
            "b-", linewidth=2, label="Legacy (total)",
        )
        ax.plot(
            [s["time"] - t0_n for s in new_samples],
            [s["total_rss_mb"] for s in new_samples],
            "r-", linewidth=2, label="New (total)",
        )
        ax.set_xlabel("Time (s)")
        ax.set_ylabel("RSS (MB)")
        ax.set_title("Total Memory (Main + Children)")
        ax.legend()
        ax.grid(True, alpha=0.3)

        plt.suptitle(
            f"Legacy vs New Recording Pattern ({duration}s, action-gated, crf=0/veryslow)",
            fontsize=14,
        )
        plt.tight_layout()

        plot_path = base_dir / "comparison.png"
        plt.savefig(str(plot_path), dpi=150, bbox_inches="tight")
        plt.close()
        print(f"\nComparison plot: {plot_path}")

        if sys.platform == "darwin":
            os.system(f"open {plot_path}")


if __name__ == "__main__":
    main()

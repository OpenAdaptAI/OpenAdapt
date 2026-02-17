"""Performance and integration tests for the recording pipeline.

These tests use pynput Controllers to inject synthetic input, record it
with the Recorder, then load the capture and verify correctness and
performance characteristics.

Marked as 'slow' — skip with:  pytest -m "not slow"
Run only these:                pytest -m slow -v

NOTE: The legacy recorder uses multiprocessing.Process for writer tasks.
On macOS (Python "spawn" start method), writer processes may fail to start
because each child re-imports modules and triggers side effects like
take_screenshot(). These tests are designed for Windows (the primary
recording platform) and will skip on macOS/Linux if the recorder
cannot start all processes within a timeout.
"""

import os
import sys
import threading
import time
from collections import Counter
from pathlib import Path

import psutil
import pytest

from openadapt_capture.capture import CaptureSession

# Recorder requires pynput which needs a display server
try:
    from openadapt_capture.recorder import Recorder
except ImportError:
    Recorder = None

# Skip on non-Windows platforms where the legacy recorder has known issues
_SKIP_REASON = (
    "Legacy recorder uses multiprocessing.Process which requires Windows "
    "or fork-safe environment. On macOS/Linux with 'spawn' start method, "
    "writer processes may fail to start."
)
_ON_WINDOWS = sys.platform == "win32"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _generate_synthetic_input(duration: float, stop_event: threading.Event) -> int:
    """Generate synthetic mouse/keyboard input via pynput Controllers.

    Returns the number of input cycles completed.
    """
    from pynput.keyboard import Controller as KeyboardController
    from pynput.mouse import Button
    from pynput.mouse import Controller as MouseController

    mouse = MouseController()
    keyboard = KeyboardController()

    start = time.time()
    i = 0
    while time.time() - start < duration and not stop_event.is_set():
        # Move mouse in a small pattern
        x_offset = (i % 10) * 10
        y_offset = (i % 5) * 10
        mouse.position = (100 + x_offset, 100 + y_offset)
        time.sleep(0.04)

        # Click every 10th iteration
        if i % 10 == 0:
            mouse.click(Button.left)
            time.sleep(0.04)

        # Type a character every 20th iteration
        if i % 20 == 0:
            keyboard.press("a")
            keyboard.release("a")
            time.sleep(0.04)

        i += 1
    return i


def _sample_memory(pid: int, interval: float, samples: list, stop: threading.Event):
    """Sample RSS of process + children at regular intervals."""
    proc = psutil.Process(pid)
    while not stop.is_set():
        try:
            main_mb = proc.memory_info().rss / (1024 * 1024)
            children = proc.children(recursive=True)
            child_mb = sum(c.memory_info().rss / (1024 * 1024) for c in children)
            samples.append({
                "time": time.time(),
                "main_mb": main_mb,
                "child_mb": child_mb,
                "total_mb": main_mb + child_mb,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        stop.wait(interval)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def capture_dir(tmp_path):
    """Provide a clean temporary capture directory."""
    d = tmp_path / "perf_capture"
    yield str(d)
    # Cleanup handled by tmp_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.slow
@pytest.mark.skipif(not _ON_WINDOWS, reason=_SKIP_REASON)
class TestRecorderIntegration:
    """Integration tests that run the full recording pipeline."""

    def test_record_and_load_roundtrip(self, capture_dir):
        """Record synthetic input, stop, reload, and verify events round-trip."""
        duration = 3  # seconds

        input_stop = threading.Event()
        cycles = [0]

        with Recorder(capture_dir, task_description="Integration test"):
            # Give recorder a moment to start listeners
            time.sleep(1)

            # Generate synthetic input in background thread
            def run_input():
                cycles[0] = _generate_synthetic_input(duration, input_stop)

            t = threading.Thread(target=run_input, daemon=True)
            t.start()
            time.sleep(duration)
            input_stop.set()
            t.join(timeout=5)

        # --- Verify capture loads correctly ---
        capture = CaptureSession.load(capture_dir)

        assert capture.task_description == "Integration test"
        assert capture.platform != ""
        assert capture.screen_size[0] > 0
        assert capture.screen_size[1] > 0

        raw = capture.raw_events()
        actions = list(capture.actions())

        # We injected clicks, moves, and key presses — should have events
        assert len(raw) > 0, "No raw events captured"
        assert len(actions) > 0, "No processed actions produced"

        # Check event types are present
        raw_types = {e.type for e in raw}
        assert "mouse.move" in raw_types or "mouse.down" in raw_types, (
            f"Expected mouse events, got: {raw_types}"
        )

        action_types = Counter(a.type for a in actions)
        # Should have at least some click or type actions
        assert len(action_types) > 0

        capture.close()

    def test_recorder_reuse(self, tmp_path):
        """Test that Recorder can be used twice in the same process.

        Validates fix for stop_sequence_detected not being reset.
        """
        for i in range(2):
            d = str(tmp_path / f"capture_{i}")
            input_stop = threading.Event()

            with Recorder(d, task_description=f"Reuse test {i}"):
                time.sleep(1)

                def run_input():
                    _generate_synthetic_input(1, input_stop)

                t = threading.Thread(target=run_input, daemon=True)
                t.start()
                time.sleep(1)
                input_stop.set()
                t.join(timeout=5)

            # Verify capture is loadable
            capture = CaptureSession.load(d)
            assert capture.task_description == f"Reuse test {i}"
            raw = capture.raw_events()
            assert len(raw) > 0, f"Run {i}: no events captured"
            capture.close()

    def test_shutdown_time(self, capture_dir):
        """Test that recorder shuts down within a reasonable time."""
        duration = 2
        input_stop = threading.Event()

        with Recorder(capture_dir, task_description="Shutdown test"):
            time.sleep(0.5)

            def run_input():
                _generate_synthetic_input(duration, input_stop)

            t = threading.Thread(target=run_input, daemon=True)
            t.start()
            time.sleep(duration)
            input_stop.set()
            t.join(timeout=5)

            t_stop_start = time.time()

        t_stop_end = time.time()
        shutdown_time = t_stop_end - t_stop_start

        # Shutdown should complete within 30 seconds
        assert shutdown_time < 30, (
            f"Shutdown took {shutdown_time:.1f}s (expected < 30s)"
        )

    def test_memory_bounded(self, capture_dir):
        """Test that memory growth during recording is bounded."""
        duration = 3
        input_stop = threading.Event()
        memory_samples = []
        mem_stop = threading.Event()

        mem_thread = threading.Thread(
            target=_sample_memory,
            args=(os.getpid(), 0.25, memory_samples, mem_stop),
            daemon=True,
        )
        mem_thread.start()

        with Recorder(capture_dir, task_description="Memory test"):
            time.sleep(0.5)

            def run_input():
                _generate_synthetic_input(duration, input_stop)

            t = threading.Thread(target=run_input, daemon=True)
            t.start()
            time.sleep(duration)
            input_stop.set()
            t.join(timeout=5)

        mem_stop.set()
        mem_thread.join(timeout=2)

        assert len(memory_samples) >= 2, "Not enough memory samples"

        total_mb = [s["total_mb"] for s in memory_samples]
        growth = total_mb[-1] - total_mb[0]

        # Memory growth should be < 500 MB for a short recording
        assert growth < 500, (
            f"Memory grew {growth:.1f} MB (start={total_mb[0]:.1f}, "
            f"end={total_mb[-1]:.1f}, peak={max(total_mb):.1f})"
        )

    def test_db_file_created(self, capture_dir):
        """Test that recording.db is created in the capture directory."""
        input_stop = threading.Event()

        with Recorder(capture_dir, task_description="DB test"):
            time.sleep(0.5)

            def run_input():
                _generate_synthetic_input(1, input_stop)

            t = threading.Thread(target=run_input, daemon=True)
            t.start()
            time.sleep(1)
            input_stop.set()
            t.join(timeout=5)

        db_path = Path(capture_dir) / "recording.db"
        assert db_path.exists(), f"recording.db not found in {capture_dir}"
        assert db_path.stat().st_size > 0, "recording.db is empty"

    def test_event_throughput(self, capture_dir):
        """Test that event capture rate is reasonable."""
        duration = 3
        input_stop = threading.Event()
        cycles = [0]

        with Recorder(capture_dir, task_description="Throughput test"):
            time.sleep(0.5)

            def run_input():
                cycles[0] = _generate_synthetic_input(duration, input_stop)

            t = threading.Thread(target=run_input, daemon=True)
            t.start()
            time.sleep(duration)
            input_stop.set()
            t.join(timeout=5)

        capture = CaptureSession.load(capture_dir)
        raw = capture.raw_events()
        capture.close()

        # We generated ~20 events/sec (moves + clicks + keys at 40ms intervals)
        # Should capture at least some fraction of them
        events_per_sec = len(raw) / duration if duration > 0 else 0
        assert events_per_sec > 1, (
            f"Only {events_per_sec:.1f} events/sec captured "
            f"({len(raw)} events in {duration}s)"
        )

"""Correction capture for the correction flywheel.

Captures a human correction using openadapt-capture's Recorder (primary path)
or falls back to simple periodic screenshots via PIL if openadapt-capture is
not available.

The Recorder provides full input event recording (mouse + keyboard) plus
action-gated screenshots, which gives the VLM parser much richer context
for understanding what the human did.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class CorrectionResult:
    """Result of a correction capture session."""

    screenshots: list[str] = field(default_factory=list)  # paths
    capture_dir: str | None = None  # openadapt-capture directory (if used)
    duration_seconds: float = 0.0
    output_dir: str = ""


def _take_screenshot(output_path: str) -> str | None:
    """Take a screenshot and save to output_path. Returns path or None."""
    try:
        from PIL import ImageGrab

        img = ImageGrab.grab()
        img.save(output_path)
        return output_path
    except Exception as exc:
        logger.warning("Screenshot failed: %s", exc)
        return None


def _has_recorder() -> bool:
    """Check if openadapt-capture Recorder is available."""
    try:
        from openadapt_capture.recorder import Recorder  # noqa: F401

        return True
    except ImportError:
        return False


def _prompt_user(step_desc: str, explanation: str) -> None:
    """Print the correction prompt to the terminal."""
    print("\n" + "=" * 60)
    print("CORRECTION NEEDED")
    print("=" * 60)
    print(f"Failed step: {step_desc}")
    if explanation:
        print(f"Reason: {explanation}")
    print("\nPlease complete this step manually.")
    print("Press Enter when done...")
    print("=" * 60 + "\n")


def _wait_for_enter(timeout_seconds: int) -> None:
    """Block until user presses Enter or timeout expires."""
    try:
        import select
        import sys

        if hasattr(select, "select"):
            remaining = timeout_seconds
            while remaining > 0:
                ready, _, _ = select.select([sys.stdin], [], [], 1.0)
                if ready:
                    sys.stdin.readline()
                    break
                remaining -= 1.0
        else:
            input()
    except EOFError:
        logger.info("stdin closed, stopping capture after timeout")
        time.sleep(min(timeout_seconds, 10))


class CorrectionCapture:
    """Capture a human correction for a failed step."""

    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def capture_correction(
        self,
        failure_context: dict,
        timeout_seconds: int = 120,
        interval_seconds: float = 2.0,
    ) -> CorrectionResult:
        """Capture a human correction.

        Uses openadapt-capture Recorder if available (full input events +
        action-gated screenshots), otherwise falls back to periodic PIL
        screenshots.
        """
        # Save the failure screenshot as "before"
        before_path = os.path.join(self.output_dir, "before.png")
        before_screenshots = []
        if failure_context.get("screenshot_bytes"):
            with open(before_path, "wb") as f:
                f.write(failure_context["screenshot_bytes"])
            before_screenshots.append(before_path)
        elif failure_context.get("screenshot_path"):
            before_screenshots.append(failure_context["screenshot_path"])

        step_desc = failure_context.get("step_action", "this step")
        explanation = failure_context.get("explanation", "")

        _prompt_user(step_desc, explanation)

        if _has_recorder():
            return self._capture_with_recorder(
                before_screenshots, timeout_seconds
            )
        else:
            logger.info("openadapt-capture not available, using simple screenshot capture")
            return self._capture_simple(
                before_screenshots, timeout_seconds, interval_seconds
            )

    def _capture_with_recorder(
        self,
        before_screenshots: list[str],
        timeout_seconds: int,
    ) -> CorrectionResult:
        """Full capture using openadapt-capture Recorder."""
        from openadapt_capture.recorder import Recorder

        capture_dir = os.path.join(self.output_dir, "recording")
        start = time.monotonic()

        with Recorder(
            capture_dir,
            task_description="Human correction for failed agent step",
            capture_video=False,  # screenshots only, faster
            capture_audio=False,
        ) as recorder:
            recorder.wait_for_ready(timeout=30)
            _wait_for_enter(timeout_seconds)
            recorder.stop()

        duration = time.monotonic() - start

        # Extract screenshots from the capture
        screenshot_paths = list(before_screenshots)
        try:
            from openadapt_capture.capture import CaptureSession

            session = CaptureSession.load(capture_dir)
            for i, action in enumerate(session.actions()):
                if action.screenshot is not None:
                    path = os.path.join(self.output_dir, f"action_{i:04d}.png")
                    action.screenshot.save(path)
                    screenshot_paths.append(path)
        except Exception as exc:
            logger.warning("Failed to extract screenshots from capture: %s", exc)
            # Fall back to taking a final screenshot
            after_path = os.path.join(self.output_dir, "after.png")
            taken = _take_screenshot(after_path)
            if taken:
                screenshot_paths.append(taken)

        logger.info(
            "Recorder capture complete: %d screenshots in %.1fs",
            len(screenshot_paths),
            duration,
        )
        return CorrectionResult(
            screenshots=screenshot_paths,
            capture_dir=capture_dir,
            duration_seconds=duration,
            output_dir=self.output_dir,
        )

    def _capture_simple(
        self,
        before_screenshots: list[str],
        timeout_seconds: int,
        interval_seconds: float,
    ) -> CorrectionResult:
        """Fallback: periodic PIL screenshots."""
        import threading

        start = time.monotonic()
        stop_event = threading.Event()
        screenshot_paths: list[str] = []

        def _capture_loop():
            idx = 0
            while not stop_event.is_set():
                stop_event.wait(interval_seconds)
                if stop_event.is_set():
                    break
                path = os.path.join(self.output_dir, f"capture_{idx:04d}.png")
                taken = _take_screenshot(path)
                if taken:
                    screenshot_paths.append(taken)
                idx += 1

        capture_thread = threading.Thread(target=_capture_loop, daemon=True)
        capture_thread.start()

        _wait_for_enter(timeout_seconds)

        stop_event.set()
        capture_thread.join(timeout=5)

        # Final "after" screenshot
        after_path = os.path.join(self.output_dir, "after.png")
        taken = _take_screenshot(after_path)
        if taken:
            screenshot_paths.append(taken)

        all_screenshots = list(before_screenshots) + screenshot_paths
        duration = time.monotonic() - start

        logger.info(
            "Simple capture complete: %d screenshots in %.1fs",
            len(all_screenshots),
            duration,
        )
        return CorrectionResult(
            screenshots=all_screenshots,
            duration_seconds=duration,
            output_dir=self.output_dir,
        )

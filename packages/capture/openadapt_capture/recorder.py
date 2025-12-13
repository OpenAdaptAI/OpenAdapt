"""High-level recording API.

Provides a simple interface for capturing GUI interactions.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openadapt_capture.events import ScreenFrameEvent
from openadapt_capture.stats import CaptureStats
from openadapt_capture.storage import Capture, CaptureStorage

if TYPE_CHECKING:
    from PIL import Image


def _get_screen_dimensions() -> tuple[int, int]:
    """Get screen dimensions in physical pixels (for video).

    Returns the actual screenshot pixel dimensions, which may be
    larger than logical dimensions on HiDPI/Retina displays.
    """
    try:
        from PIL import ImageGrab
        screenshot = ImageGrab.grab()
        return screenshot.size
    except Exception:
        return (1920, 1080)  # Default fallback


def _get_display_pixel_ratio() -> float:
    """Get the display pixel ratio (e.g., 2.0 for Retina).

    This is the ratio of physical pixels to logical pixels.
    Mouse coordinates from pynput are in logical space.

    Uses mss to get logical monitor dimensions (like OpenAdapt).
    """
    try:
        import mss
        from PIL import ImageGrab

        # Get physical dimensions from screenshot
        screenshot = ImageGrab.grab()
        physical_width = screenshot.size[0]

        # Get logical dimensions from mss (works on macOS, Windows, Linux)
        with mss.mss() as sct:
            # monitors[0] is the "all monitors" bounding box on multi-monitor setups
            # monitors[1] is typically the primary monitor
            monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            logical_width = monitor["width"]

        if logical_width > 0:
            return physical_width / logical_width

        return 1.0
    except ImportError:
        # mss not installed, try alternative methods
        try:
            from PIL import ImageGrab

            screenshot = ImageGrab.grab()
            physical_width = screenshot.size[0]

            if sys.platform == "win32":
                import ctypes
                user32 = ctypes.windll.user32
                user32.SetProcessDPIAware()
                logical_width = user32.GetSystemMetrics(0)
                return physical_width / logical_width
        except Exception:
            pass

        return 1.0
    except Exception:
        return 1.0


class Recorder:
    """High-level recorder for GUI interactions.

    Captures mouse, keyboard, and screen events with minimal configuration.

    Usage:
        with Recorder("./my_capture") as recorder:
            # Recording happens automatically
            input("Press Enter to stop...")

        print(f"Captured {recorder.event_count} events")
    """

    def __init__(
        self,
        capture_dir: str | Path,
        task_description: str | None = None,
        capture_video: bool = True,
        capture_audio: bool = False,
        video_fps: int = 24,
        capture_mouse_moves: bool = True,
    ) -> None:
        """Initialize recorder.

        Args:
            capture_dir: Directory to store capture files.
            task_description: Optional description of the task being recorded.
            capture_video: Whether to capture screen video.
            capture_audio: Whether to capture audio.
            video_fps: Video frames per second.
            capture_mouse_moves: Whether to capture mouse move events.
        """
        self.capture_dir = Path(capture_dir)
        self.task_description = task_description
        self.capture_video = capture_video
        self.capture_audio = capture_audio
        self.video_fps = video_fps
        self.capture_mouse_moves = capture_mouse_moves

        self._capture: Capture | None = None
        self._storage: CaptureStorage | None = None
        self._input_listener = None
        self._screen_capturer = None
        self._video_writer = None
        self._audio_recorder = None
        self._running = False
        self._event_count = 0
        self._lock = threading.Lock()
        self._stats = CaptureStats()

    @property
    def event_count(self) -> int:
        """Get the number of events captured."""
        return self._event_count

    @property
    def is_recording(self) -> bool:
        """Check if recording is active."""
        return self._running

    @property
    def stats(self) -> CaptureStats:
        """Get performance statistics."""
        return self._stats

    def _on_input_event(self, event: Any) -> None:
        """Handle input events from listener."""
        if self._storage is not None and self._running:
            self._storage.write_event(event)
            with self._lock:
                self._event_count += 1
            # Record performance stat
            event_type = event.type if isinstance(event.type, str) else event.type.value
            self._stats.record_event(event_type, event.timestamp)

    def _on_screen_frame(self, image: "Image", timestamp: float) -> None:
        """Handle screen frames."""
        if self._video_writer is not None and self._running:
            self._video_writer.write_frame(image, timestamp)

            # Also record screen frame event
            if self._storage is not None:
                event = ScreenFrameEvent(
                    timestamp=timestamp,
                    video_timestamp=timestamp - (self._video_writer.start_time or timestamp),
                    width=image.width,
                    height=image.height,
                )
                self._storage.write_event(event)
                # Record performance stat
                self._stats.record_event("screen.frame", timestamp)

    def start(self) -> None:
        """Start recording."""
        if self._running:
            return

        # Create capture directory
        self.capture_dir.mkdir(parents=True, exist_ok=True)

        # Start performance stats tracking
        self._stats.start()

        # Get screen dimensions and pixel ratio
        screen_width, screen_height = _get_screen_dimensions()
        pixel_ratio = _get_display_pixel_ratio()

        # Initialize storage
        import uuid
        capture_id = str(uuid.uuid4())[:8]
        self._capture = Capture(
            id=capture_id,
            started_at=time.time(),
            platform=sys.platform,
            screen_width=screen_width,
            screen_height=screen_height,
            pixel_ratio=pixel_ratio,
            task_description=self.task_description,
        )

        db_path = self.capture_dir / "capture.db"
        self._storage = CaptureStorage(db_path)
        self._storage.init_capture(self._capture)

        self._running = True

        # Start input capture
        try:
            from openadapt_capture.input import InputListener
            self._input_listener = InputListener(
                callback=self._on_input_event,
                capture_mouse_moves=self.capture_mouse_moves,
            )
            self._input_listener.start()
        except ImportError:
            pass  # Input capture not available

        # Start video capture
        if self.capture_video:
            try:
                from openadapt_capture.input import ScreenCapturer
                from openadapt_capture.video import VideoWriter

                video_path = self.capture_dir / "video.mp4"
                self._video_writer = VideoWriter(
                    video_path,
                    width=screen_width,
                    height=screen_height,
                    fps=self.video_fps,
                )

                self._screen_capturer = ScreenCapturer(
                    callback=self._on_screen_frame,
                    fps=self.video_fps,
                )
                self._screen_capturer.start()
            except ImportError:
                pass  # Video capture not available

        # Start audio capture
        if self.capture_audio:
            try:
                from openadapt_capture.audio import AudioRecorder
                self._audio_recorder = AudioRecorder()
                self._audio_recorder.start()
            except ImportError:
                pass  # Audio capture not available

    def stop(self) -> None:
        """Stop recording."""
        if not self._running:
            return

        self._running = False

        # Stop input capture
        if self._input_listener is not None:
            self._input_listener.stop()
            self._input_listener = None

        # Stop screen capture
        if self._screen_capturer is not None:
            self._screen_capturer.stop()
            self._screen_capturer = None

        # Stop video writer
        if self._video_writer is not None:
            if self._capture is not None:
                self._capture.video_start_time = self._video_writer.start_time
            self._video_writer.close()
            self._video_writer = None

        # Stop audio capture
        if self._audio_recorder is not None:
            if self._capture is not None:
                self._capture.audio_start_time = self._audio_recorder.start_time
            self._audio_recorder.stop()
            # Save audio file
            audio_path = self.capture_dir / "audio.flac"
            self._audio_recorder.save_flac(audio_path)
            self._audio_recorder = None

        # Update capture metadata
        if self._capture is not None and self._storage is not None:
            self._capture.ended_at = time.time()
            self._storage.update_capture(self._capture)

        # Close storage
        if self._storage is not None:
            self._storage.close()
            self._storage = None

    def __enter__(self) -> "Recorder":
        """Context manager entry."""
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        self.stop()

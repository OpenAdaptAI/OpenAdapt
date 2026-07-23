"""Input capture built on OpenAdapt's native platform observers."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from openadapt_capture.events import (
    KeyDownEvent,
    KeyUpEvent,
    MouseDownEvent,
    MouseMoveEvent,
    MouseScrollEvent,
    MouseUpEvent,
)
from openadapt_capture.input_observer import (
    InputObserver,
    ObservedInput,
    ObservedKey,
    ObservedMouseButton,
    ObservedMouseMove,
    ObservedMouseScroll,
    create_input_observer,
)
from openadapt_capture.x11_threads import ensure_xlib_thread_support


def _get_timestamp() -> float:
    """Get current timestamp."""
    return time.time()


def _to_public_event(observed: ObservedInput):
    """Convert a native normalized event to the public Pydantic event model."""
    timestamp = (
        observed.timestamp
        if observed.timestamp is not None
        else _get_timestamp()
    )
    if isinstance(observed, ObservedMouseMove):
        return MouseMoveEvent(timestamp=timestamp, x=observed.x, y=observed.y)
    if isinstance(observed, ObservedMouseButton):
        event_type = MouseDownEvent if observed.pressed else MouseUpEvent
        return event_type(
            timestamp=timestamp,
            x=observed.x,
            y=observed.y,
            button=observed.button,
        )
    if isinstance(observed, ObservedMouseScroll):
        return MouseScrollEvent(
            timestamp=timestamp,
            x=observed.x,
            y=observed.y,
            dx=observed.dx,
            dy=observed.dy,
        )
    event_type = KeyDownEvent if observed.pressed else KeyUpEvent
    return event_type(
        timestamp=timestamp,
        key_name=observed.key_name,
        key_char=observed.key_char,
        key_vk=observed.key_vk,
        canonical_key_name=observed.canonical_key_name,
        canonical_key_char=observed.canonical_key_char,
        canonical_key_vk=observed.canonical_key_vk,
    )


class _StopSequenceMatcher:
    def __init__(
        self,
        sequences: list[str] | None,
        callback: Callable[[], None] | None,
    ) -> None:
        self.sequences = [sequence for sequence in (sequences or []) if sequence]
        self.callback = callback
        self.indices = [0 for _ in self.sequences]

    def observe(self, event: ObservedKey) -> None:
        if not event.pressed:
            return
        candidate = event.canonical_key_char or event.canonical_key_name
        if candidate is None:
            self.indices = [0 for _ in self.sequences]
            return
        candidate = candidate.lower()
        for index, sequence in enumerate(self.sequences):
            expected = sequence[self.indices[index]].lower()
            if candidate == expected:
                self.indices[index] += 1
            else:
                self.indices[index] = 1 if candidate == sequence[0].lower() else 0
            if self.indices[index] == len(sequence):
                self.indices[index] = 0
                if self.callback is not None:
                    self.callback()


# =============================================================================
# Mouse Listener
# =============================================================================


class MouseListener:
    """Captures mouse events (move, click, scroll).

    Usage:
        def on_event(event):
            print(event)

        listener = MouseListener(on_event)
        listener.start()
        # ... later ...
        listener.stop()

    Or as context manager:
        with MouseListener(on_event):
            # Capture events
            pass
    """

    def __init__(
        self,
        callback: Callable[[MouseMoveEvent | MouseDownEvent | MouseUpEvent | MouseScrollEvent], None],
        capture_moves: bool = True,
    ) -> None:
        """Initialize mouse listener.

        Args:
            callback: Function called for each mouse event.
            capture_moves: Whether to capture mouse move events (can be noisy).
        """
        self.callback = callback
        self.capture_moves = capture_moves
        self._observer: InputObserver | None = None
        self._running = False

    def _on_observed(self, observed: ObservedInput) -> None:
        if isinstance(
            observed,
            (ObservedMouseMove, ObservedMouseButton, ObservedMouseScroll),
        ):
            self.callback(_to_public_event(observed))

    def start(self) -> None:
        """Start capturing mouse events."""
        if self._running:
            return
        observer = create_input_observer(
            self._on_observed,
            observe_keyboard=False,
            observe_mouse=True,
            capture_mouse_moves=self.capture_moves,
        )
        observer.start()
        self._observer = observer
        self._running = True

    def stop(self) -> None:
        """Stop capturing mouse events."""
        if not self._running:
            return
        try:
            if self._observer is not None:
                self._observer.stop()
        finally:
            self._observer = None
            self._running = False

    def __enter__(self) -> "MouseListener":
        """Context manager entry."""
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        self.stop()


# =============================================================================
# Keyboard Listener
# =============================================================================


class KeyboardListener:
    """Captures keyboard events (key press and release).

    Usage:
        def on_event(event):
            print(event)

        listener = KeyboardListener(on_event)
        listener.start()
        # ... later ...
        listener.stop()
    """

    def __init__(
        self,
        callback: Callable[[KeyDownEvent | KeyUpEvent], None],
        stop_sequences: list[str] | None = None,
        on_stop_sequence: Callable[[], None] | None = None,
    ) -> None:
        """Initialize keyboard listener.

        Args:
            callback: Function called for each keyboard event.
            stop_sequences: Optional list of key sequences that trigger stop.
            on_stop_sequence: Callback when a stop sequence is detected.
        """
        self.callback = callback
        self._matcher = _StopSequenceMatcher(stop_sequences, on_stop_sequence)
        self._observer: InputObserver | None = None
        self._running = False

    def _on_observed(self, observed: ObservedInput) -> None:
        if not isinstance(observed, ObservedKey):
            return
        self.callback(_to_public_event(observed))
        self._matcher.observe(observed)

    def start(self) -> None:
        """Start capturing keyboard events."""
        if self._running:
            return
        observer = create_input_observer(
            self._on_observed,
            observe_keyboard=True,
            observe_mouse=False,
            capture_mouse_moves=False,
        )
        observer.start()
        self._observer = observer
        self._running = True

    def stop(self) -> None:
        """Stop capturing keyboard events."""
        if not self._running:
            return

        try:
            if self._observer is not None:
                self._observer.stop()
        finally:
            self._observer = None
            self._running = False

    def __enter__(self) -> "KeyboardListener":
        """Context manager entry."""
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        self.stop()


# =============================================================================
# Combined Input Listener
# =============================================================================


class InputListener:
    """Combined mouse and keyboard listener.

    Captures all input events and sends them to a single callback.

    Usage:
        def on_event(event):
            print(event)

        listener = InputListener(on_event)
        listener.start()
        # ... later ...
        listener.stop()
    """

    def __init__(
        self,
        callback: Callable[[Any], None],
        capture_mouse_moves: bool = True,
        stop_sequences: list[str] | None = None,
        on_stop_sequence: Callable[[], None] | None = None,
    ) -> None:
        """Initialize combined input listener.

        Args:
            callback: Function called for each input event.
            capture_mouse_moves: Whether to capture mouse move events.
            stop_sequences: Optional list of key sequences that trigger stop.
            on_stop_sequence: Callback when a stop sequence is detected.
        """
        self.callback = callback
        self.capture_mouse_moves = capture_mouse_moves
        self._matcher = _StopSequenceMatcher(stop_sequences, on_stop_sequence)
        self._observer: InputObserver | None = None
        self._running = False

    def _on_observed(self, observed: ObservedInput) -> None:
        self.callback(_to_public_event(observed))
        if isinstance(observed, ObservedKey):
            self._matcher.observe(observed)

    def start(self) -> None:
        """Start capturing all input events."""
        if self._running:
            return
        observer = create_input_observer(
            self._on_observed,
            observe_keyboard=True,
            observe_mouse=True,
            capture_mouse_moves=self.capture_mouse_moves,
        )
        observer.start()
        self._observer = observer
        self._running = True

    def stop(self) -> None:
        """Stop capturing all input events."""
        if not self._running:
            return

        try:
            if self._observer is not None:
                self._observer.stop()
        finally:
            self._observer = None
            self._running = False

    def __enter__(self) -> "InputListener":
        """Context manager entry."""
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        self.stop()


# =============================================================================
# Screen Capture
# =============================================================================


class ScreenCapturer:
    """Captures screenshots at regular intervals.

    Usage:
        def on_frame(image, timestamp):
            print(f"Frame at {timestamp}")

        capturer = ScreenCapturer(on_frame, fps=10)
        capturer.start()
        # ... later ...
        capturer.stop()
    """

    def __init__(
        self,
        callback: Callable[[Any, float], None],
        fps: float = 24.0,
    ) -> None:
        """Initialize screen capturer.

        Args:
            callback: Function called with (image, timestamp) for each frame.
            fps: Target frames per second.
        """
        self.callback = callback
        self.fps = fps
        self._interval = 1.0 / fps
        self._running = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def _capture_loop(self) -> None:
        """Main capture loop running in background thread.

        Uses mss for screenshots (same as legacy OpenAdapt record.py),
        which is 2-4x faster than PIL.ImageGrab on Windows.
        """
        import sys

        import mss
        import mss.base
        from PIL import Image

        if sys.platform == "win32":
            import mss.windows
            # Fix cursor flicker on Windows (from legacy OpenAdapt)
            # https://github.com/BoboTiG/python-mss/issues/179#issuecomment-673292002
            mss.windows.CAPTUREBLT = 0

        sct = mss.mss()
        monitor = sct.monitors[0]  # All monitors combined

        while not self._stop_event.is_set():
            timestamp = _get_timestamp()
            try:
                sct_img = sct.grab(monitor)
                screenshot = Image.frombytes(
                    "RGB", sct_img.size, sct_img.bgra, "raw", "BGRX"
                )
                self.callback(screenshot, timestamp)
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(f"Screenshot capture failed: {e}")

            # Sleep for remaining interval
            elapsed = _get_timestamp() - timestamp
            sleep_time = max(0, self._interval - elapsed)
            if sleep_time > 0:
                self._stop_event.wait(sleep_time)

    def start(self) -> None:
        """Start capturing screenshots."""
        if self._running:
            return

        ensure_xlib_thread_support()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()
        self._running = True

    def stop(self) -> None:
        """Stop capturing screenshots."""
        if not self._running:
            return

        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._running = False

    def __enter__(self) -> "ScreenCapturer":
        """Context manager entry."""
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        self.stop()

"""Input capture for mouse and keyboard events.

This module provides cross-platform input capture using pynput,
following OpenAdapt's proven implementation.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any, Callable

from pynput import keyboard as _pynput_keyboard
from pynput import mouse as _pynput_mouse

if TYPE_CHECKING:
    from pynput import keyboard, mouse

from openadapt_capture.events import (
    KeyDownEvent,
    KeyUpEvent,
    MouseButton,
    MouseDownEvent,
    MouseMoveEvent,
    MouseScrollEvent,
    MouseUpEvent,
)


def _get_timestamp() -> float:
    """Get current timestamp."""
    return time.time()


def _button_to_mouse_button(button: "mouse.Button") -> MouseButton:
    """Convert pynput button to MouseButton enum."""
    name = button.name.lower()
    if name == "left":
        return MouseButton.LEFT
    elif name == "right":
        return MouseButton.RIGHT
    elif name == "middle":
        return MouseButton.MIDDLE
    else:
        return MouseButton.LEFT  # Default fallback


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
        self._listener: "mouse.Listener" | None = None
        self._running = False

    def _on_move(self, x: int, y: int) -> None:
        """Handle mouse move event."""
        if not self.capture_moves:
            return
        event = MouseMoveEvent(
            timestamp=_get_timestamp(),
            x=float(x),
            y=float(y),
        )
        self.callback(event)

    def _on_click(
        self,
        x: int,
        y: int,
        button: "mouse.Button",
        pressed: bool,
    ) -> None:
        """Handle mouse click event."""
        timestamp = _get_timestamp()
        mouse_button = _button_to_mouse_button(button)

        if pressed:
            event = MouseDownEvent(
                timestamp=timestamp,
                x=float(x),
                y=float(y),
                button=mouse_button,
            )
        else:
            event = MouseUpEvent(
                timestamp=timestamp,
                x=float(x),
                y=float(y),
                button=mouse_button,
            )
        self.callback(event)

    def _on_scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        """Handle mouse scroll event."""
        event = MouseScrollEvent(
            timestamp=_get_timestamp(),
            x=float(x),
            y=float(y),
            dx=float(dx),
            dy=float(dy),
        )
        self.callback(event)

    def start(self) -> None:
        """Start capturing mouse events."""
        if self._running:
            return

        self._listener = _pynput_mouse.Listener(
            on_move=self._on_move,
            on_click=self._on_click,
            on_scroll=self._on_scroll,
        )
        self._listener.start()
        self._running = True

    def stop(self) -> None:
        """Stop capturing mouse events."""
        if not self._running:
            return

        if self._listener is not None:
            self._listener.stop()
            self._listener = None
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
        self.stop_sequences = stop_sequences or []
        self.on_stop_sequence = on_stop_sequence
        self._listener: "keyboard.Listener" | None = None
        self._running = False
        self._stop_sequence_indices = [0 for _ in self.stop_sequences]

    def _extract_key_info(
        self,
        key: "keyboard.Key | keyboard.KeyCode",
        canonical_key: "keyboard.Key | keyboard.KeyCode | None" = None,
    ) -> dict[str, str | None]:
        """Extract key information from pynput key object."""
        key_name = getattr(key, "name", None)
        key_char = getattr(key, "char", None)
        key_vk = str(getattr(key, "vk", None)) if hasattr(key, "vk") and key.vk else None

        canonical_key_name = None
        canonical_key_char = None
        canonical_key_vk = None

        if canonical_key is not None:
            canonical_key_name = getattr(canonical_key, "name", None)
            canonical_key_char = getattr(canonical_key, "char", None)
            canonical_key_vk = (
                str(getattr(canonical_key, "vk", None))
                if hasattr(canonical_key, "vk") and canonical_key.vk
                else None
            )

        return {
            "key_name": key_name,
            "key_char": key_char,
            "key_vk": key_vk,
            "canonical_key_name": canonical_key_name,
            "canonical_key_char": canonical_key_char,
            "canonical_key_vk": canonical_key_vk,
        }

    def _check_stop_sequence(
        self,
        key: "keyboard.Key | keyboard.KeyCode",
        canonical_key: "keyboard.Key | keyboard.KeyCode | None",
    ) -> bool:
        """Check if key press completes a stop sequence."""
        if not self.stop_sequences:
            return False

        canonical_key_name = getattr(canonical_key, "name", None) if canonical_key else None

        for i, stop_sequence in enumerate(self.stop_sequences):
            current_char = stop_sequence[self._stop_sequence_indices[i]]

            # Get canonical representation for comparison
            canonical_sequence_key = self._listener.canonical(
                _pynput_keyboard.KeyCode.from_char(current_char)
            )

            matches = (
                canonical_key == canonical_sequence_key
                or canonical_key_name == current_char
            )

            if matches:
                self._stop_sequence_indices[i] += 1
            else:
                self._stop_sequence_indices[i] = 0

            if self._stop_sequence_indices[i] == len(stop_sequence):
                return True

        return False

    def _on_press(self, key: "keyboard.Key | keyboard.KeyCode") -> None:
        """Handle key press event."""
        canonical_key = self._listener.canonical(key) if self._listener else None
        key_info = self._extract_key_info(key, canonical_key)

        event = KeyDownEvent(
            timestamp=_get_timestamp(),
            **key_info,
        )
        self.callback(event)

        # Check stop sequences
        if self._check_stop_sequence(key, canonical_key):
            if self.on_stop_sequence:
                self.on_stop_sequence()

    def _on_release(self, key: "keyboard.Key | keyboard.KeyCode") -> None:
        """Handle key release event."""
        canonical_key = self._listener.canonical(key) if self._listener else None
        key_info = self._extract_key_info(key, canonical_key)

        event = KeyUpEvent(
            timestamp=_get_timestamp(),
            **key_info,
        )
        self.callback(event)

    def start(self) -> None:
        """Start capturing keyboard events."""
        if self._running:
            return

        self._listener = _pynput_keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
        )
        self._listener.start()
        self._running = True

    def stop(self) -> None:
        """Stop capturing keyboard events."""
        if not self._running:
            return

        if self._listener is not None:
            self._listener.stop()
            self._listener = None
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
        self._mouse_listener = MouseListener(
            callback=callback,
            capture_moves=capture_mouse_moves,
        )
        self._keyboard_listener = KeyboardListener(
            callback=callback,
            stop_sequences=stop_sequences,
            on_stop_sequence=on_stop_sequence,
        )
        self._running = False

    def start(self) -> None:
        """Start capturing all input events."""
        if self._running:
            return

        self._mouse_listener.start()
        self._keyboard_listener.start()
        self._running = True

    def stop(self) -> None:
        """Stop capturing all input events."""
        if not self._running:
            return

        self._mouse_listener.stop()
        self._keyboard_listener.stop()
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
        """Main capture loop running in background thread."""
        try:
            from PIL import ImageGrab
        except ImportError as e:
            raise ImportError(
                "Pillow is required for screen capture. Install with: pip install Pillow"
            ) from e

        while not self._stop_event.is_set():
            timestamp = _get_timestamp()
            try:
                screenshot = ImageGrab.grab()
                self.callback(screenshot, timestamp)
            except Exception:
                pass  # Ignore capture errors

            # Sleep for remaining interval
            elapsed = _get_timestamp() - timestamp
            sleep_time = max(0, self._interval - elapsed)
            if sleep_time > 0:
                self._stop_event.wait(sleep_time)

    def start(self) -> None:
        """Start capturing screenshots."""
        if self._running:
            return

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

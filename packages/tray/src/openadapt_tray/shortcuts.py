"""Global hotkey handling for OpenAdapt Tray."""

from dataclasses import dataclass
from typing import Callable, Dict, Optional
import threading


@dataclass
class HotkeyConfig:
    """Hotkey configuration."""

    toggle_recording: str = "<ctrl>+<shift>+r"
    open_dashboard: str = "<ctrl>+<shift>+d"
    stop_recording: str = "<ctrl>+<ctrl>+<ctrl>"  # Triple ctrl (legacy compat)


class HotkeyManager:
    """Manages global hotkeys."""

    def __init__(self, config: Optional[HotkeyConfig] = None):
        self.config = config or HotkeyConfig()
        self._handlers: Dict[str, Callable] = {}
        self._listener = None
        self._key_listener = None
        self._ctrl_count = 0
        self._ctrl_timer: Optional[threading.Timer] = None
        self._running = False

    def register(self, hotkey: str, handler: Callable) -> None:
        """Register a hotkey handler.

        Args:
            hotkey: The hotkey combination string (e.g., "<ctrl>+<shift>+r").
            handler: The callback function to invoke when the hotkey is pressed.
        """
        self._handlers[hotkey] = handler

    def unregister(self, hotkey: str) -> None:
        """Unregister a hotkey handler.

        Args:
            hotkey: The hotkey combination string to unregister.
        """
        if hotkey in self._handlers:
            del self._handlers[hotkey]

    def start(self) -> None:
        """Start listening for hotkeys."""
        if self._running:
            return

        self._running = True

        try:
            from pynput import keyboard

            # Build hotkey dict for pynput
            hotkeys = {}
            for combo, handler in self._handlers.items():
                if combo == "<ctrl>+<ctrl>+<ctrl>":
                    # Special handling for triple-ctrl
                    continue
                hotkeys[combo] = handler

            if hotkeys:
                self._listener = keyboard.GlobalHotKeys(hotkeys)
                self._listener.start()

            # Also listen for triple-ctrl pattern
            if "<ctrl>+<ctrl>+<ctrl>" in self._handlers:
                self._start_ctrl_listener()

        except ImportError:
            print("Warning: pynput not available, hotkeys disabled")
        except Exception as e:
            print(f"Warning: Could not start hotkey listener: {e}")

    def _start_ctrl_listener(self) -> None:
        """Start listener for triple-ctrl pattern."""
        try:
            from pynput import keyboard

            def on_press(key):
                if key == keyboard.Key.ctrl_l or key == keyboard.Key.ctrl_r:
                    self._on_ctrl_press()

            def on_release(key):
                pass

            self._key_listener = keyboard.Listener(
                on_press=on_press,
                on_release=on_release,
            )
            self._key_listener.start()
        except Exception as e:
            print(f"Warning: Could not start ctrl listener: {e}")

    def _on_ctrl_press(self) -> None:
        """Handle ctrl key press for triple-ctrl detection."""
        self._ctrl_count += 1

        # Reset timer
        if self._ctrl_timer:
            self._ctrl_timer.cancel()

        if self._ctrl_count >= 3:
            self._ctrl_count = 0
            handler = self._handlers.get("<ctrl>+<ctrl>+<ctrl>")
            if handler:
                try:
                    handler()
                except Exception as e:
                    print(f"Error in hotkey handler: {e}")
        else:
            # Reset count after 500ms
            self._ctrl_timer = threading.Timer(0.5, self._reset_ctrl_count)
            self._ctrl_timer.daemon = True
            self._ctrl_timer.start()

    def _reset_ctrl_count(self) -> None:
        """Reset the ctrl key press counter."""
        self._ctrl_count = 0

    def stop(self) -> None:
        """Stop listening for hotkeys."""
        self._running = False

        if self._listener:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None

        if self._key_listener:
            try:
                self._key_listener.stop()
            except Exception:
                pass
            self._key_listener = None

        if self._ctrl_timer:
            self._ctrl_timer.cancel()
            self._ctrl_timer = None

    def is_running(self) -> bool:
        """Check if the hotkey manager is running."""
        return self._running

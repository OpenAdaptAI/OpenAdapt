"""Global hotkey handling for OpenAdapt Tray."""

import contextlib
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass

# Display names for pynput key names. macOS menus use symbols and no
# separator; Windows and Linux spell the keys out and join them with "+".
# Keys not listed here are title-cased ("page_up" shows as "Page Up").
_MAC_KEYS = {
    "ctrl": "⌃",
    "shift": "⇧",
    "alt": "⌥",
    "cmd": "⌘",
    "enter": "↩",
    "esc": "⎋",
    "tab": "⇥",
    "backspace": "⌫",
    "delete": "⌦",
    "up": "↑",
    "down": "↓",
    "left": "←",
    "right": "→",
}
_PC_KEYS = {
    "ctrl": "Ctrl",
    "shift": "Shift",
    "alt": "Alt",
    "alt_gr": "AltGr",
    "esc": "Esc",
}
_SIDE_SUFFIXES = ("_l", "_r", "_gr")


def _split_hotkey(spec: str) -> list[str] | None:
    """Split a pynput hotkey string into keys, as pynput's parser does.

    A "+" right after a separator is the plus key itself, so "<ctrl>++" is
    Ctrl and "+". Returns None for a string pynput would reject.
    """
    parts = []
    start = 0
    for i, c in enumerate(spec):
        if c == "+" and i != start:
            parts.append(spec[start:i])
            start = i + 1
    if start == len(spec):
        return None
    parts.append(spec[start:])
    return parts


def _display_key(key: str, platform: str) -> str | None:
    """Return the display name of one key from a pynput hotkey string.

    Returns None for a key pynput would reject: more than one character
    without angle brackets ("ctrl"), or empty brackets ("<>").
    """
    if not (key.startswith("<") and key.endswith(">") and len(key) > 2):
        return key.upper() if len(key) == 1 else None
    name = key[1:-1].lower()
    if name.isdigit():
        # A virtual key code has no display name; show it as configured.
        return key
    if platform == "darwin":
        names = _MAC_KEYS
    else:
        names = {**_PC_KEYS, "cmd": "Win" if platform == "win32" else "Super"}
    if name not in names:
        # Left and right modifiers (ctrl_l, shift_r, ...) show as the modifier.
        base, sep, side = name.rpartition("_")
        if sep and f"_{side}" in _SIDE_SUFFIXES and base in names:
            name = base
    if name in names:
        return names[name]
    if name[0] == "f" and name[1:].isdigit():
        return name.upper()
    return name.replace("_", " ").title()


def format_hotkey(spec: str, platform: str | None = None) -> str:
    """Format a pynput hotkey string the way the platform shows shortcuts.

    "<ctrl>+<shift>+r" becomes "⌃⇧R" on macOS and "Ctrl+Shift+R" on Windows
    and Linux. A string that is not in pynput's form ("ctrl+shift+r", "<ctrl>+")
    is returned unchanged, and a value that is not a string (null in tray.json)
    is shown as ``str()`` gives it, so a bad setting still shows up in the menu
    instead of breaking it. Key names are not checked against pynput's list.

    Args:
        spec: The hotkey as configured, in pynput GlobalHotKeys syntax.
        platform: A ``sys.platform`` value. Defaults to the running platform.
    """
    if not isinstance(spec, str):
        return str(spec)
    platform = sys.platform if platform is None else platform
    keys = _split_hotkey(spec)
    if keys is None:
        return spec
    shown = [_display_key(key, platform) for key in keys]
    if None in shown:
        return spec
    return "".join(shown) if platform == "darwin" else "+".join(shown)


@dataclass
class HotkeyConfig:
    """Hotkey configuration."""

    toggle_recording: str = "<ctrl>+<shift>+r"
    open_dashboard: str = "<ctrl>+<shift>+d"
    stop_recording: str = "<ctrl>+<ctrl>+<ctrl>"  # Triple ctrl (legacy compat)


class HotkeyManager:
    """Manages global hotkeys."""

    def __init__(self, config: HotkeyConfig | None = None):
        self.config = config or HotkeyConfig()
        self._handlers: dict[str, Callable] = {}
        self._listener = None
        self._key_listener = None
        self._ctrl_count = 0
        self._ctrl_timer: threading.Timer | None = None
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

        # stop() is called from quit(). pynput's platform backends can raise on
        # teardown (X11 display already gone, macOS run loop torn down); the
        # listener threads are daemons either way, so there is nothing to act on
        # and refusing to quit over it would be worse.
        if self._listener:
            with contextlib.suppress(Exception):
                self._listener.stop()
            self._listener = None

        if self._key_listener:
            with contextlib.suppress(Exception):
                self._key_listener.stop()
            self._key_listener = None

        if self._ctrl_timer:
            self._ctrl_timer.cancel()
            self._ctrl_timer = None

    def is_running(self) -> bool:
        """Check if the hotkey manager is running."""
        return self._running

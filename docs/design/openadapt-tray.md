# openadapt-tray Package Design

## Overview

`openadapt-tray` is a cross-platform system tray application that provides a graphical interface for the OpenAdapt ecosystem. It serves as a thin orchestration layer, allowing users to control recording, monitor training, view captures, and access settings without using the command line.

## Legacy Implementation Analysis

### Current Features (Legacy `openadapt/app/tray.py`)

The legacy implementation uses **PySide6/Qt** for cross-platform system tray functionality:

**Architecture:**
- `QSystemTrayIcon` for the system tray icon
- `QMenu` for context menu
- `QDialog` for configuration dialogs (replay strategy, delete confirmation)
- `pyqttoast` for toast notifications
- Multiprocessing pipes (`multiprocessing.Pipe`) for IPC with recording process
- `QThread` + `Worker` pattern for async signal handling
- Platform-specific Dock hiding on macOS via `AppKit`

**Menu Structure:**
- Record / Stop Recording (toggle)
- Visualize submenu (lists all recordings)
- Replay submenu (lists all recordings, opens strategy dialog)
- Delete submenu (lists all recordings, confirms deletion)
- Quit

**Key Patterns:**
- `TrackedQAction` - wraps `QAction` to send analytics events via PostHog
- Signal-based state updates (`record.starting`, `record.started`, `record.stopping`, `record.stopped`, `replay.*`)
- Toast notifications for status updates (recording started/stopped, etc.)
- Dashboard launched automatically as a background thread
- Recording process runs in a separate `multiprocessing.Process`

**Stop Sequences:**
- Typing `oa.stop` or pressing `Ctrl` three times stops recording
- Configurable via `STOP_SEQUENCES` in config

### Limitations of Legacy Implementation

1. **Heavyweight dependency** - PySide6 is a large dependency (~100MB+)
2. **No global hotkeys** - Recording can only be stopped via stop sequences or tray menu
3. **Tightly coupled** - Direct imports of internal modules (crud, models, etc.)
4. **No status icons** - Same icon regardless of state
5. **No auto-start** - Manual setup required for login startup
6. **Single dashboard** - Only supports the legacy Next.js dashboard

## New Architecture Design

### Design Principles

1. **Thin wrapper** - Minimal business logic; delegate to CLI or sub-packages
2. **Cross-platform first** - Consistent behavior on macOS, Windows, and Linux
3. **Lightweight** - Prefer smaller dependencies (pystray ~50KB vs PySide6 ~100MB)
4. **Event-driven** - Async status updates via IPC
5. **Configurable** - User-customizable hotkeys, icons, and behaviors

### Package Structure

```
openadapt-tray/
├── src/openadapt_tray/
│   ├── __init__.py           # Package exports, version
│   ├── __main__.py           # Entry point: python -m openadapt_tray
│   ├── app.py                # Main TrayApplication class
│   ├── menu.py               # Menu construction and actions
│   ├── icons.py              # Icon loading and status icons
│   ├── notifications.py      # Cross-platform notifications
│   ├── shortcuts.py          # Global hotkey handling
│   ├── config.py             # Tray-specific configuration
│   ├── ipc.py                # Inter-process communication
│   ├── state.py              # Application state machine
│   └── platform/
│       ├── __init__.py       # Platform detection and abstraction
│       ├── base.py           # Abstract base class
│       ├── macos.py          # macOS-specific (AppKit, rumps optional)
│       ├── windows.py        # Windows-specific (win32api)
│       └── linux.py          # Linux-specific (AppIndicator)
├── assets/
│   ├── icons/
│   │   ├── idle.png          # Default state
│   │   ├── idle@2x.png       # Retina support
│   │   ├── recording.png     # Recording active
│   │   ├── recording@2x.png
│   │   ├── training.png      # Training in progress
│   │   ├── training@2x.png
│   │   ├── error.png         # Error state
│   │   └── error@2x.png
│   └── logo.ico              # Windows icon format
├── pyproject.toml
├── README.md
└── tests/
    ├── test_app.py
    ├── test_menu.py
    ├── test_shortcuts.py
    └── test_platform.py
```

### Dependencies

**Required:**
```toml
[project]
dependencies = [
    "pystray>=0.19.0",         # Cross-platform system tray
    "Pillow>=9.0.0",           # Icon handling
    "pynput>=1.7.0",           # Global hotkeys
    "click>=8.0.0",            # CLI integration (consistent with meta-package)
]
```

**Optional Platform Enhancements:**
```toml
[project.optional-dependencies]
macos-native = [
    "rumps>=0.4.0",            # Native macOS menu bar
]
all = [
    "openadapt-tray[macos-native]",
]
```

**Why pystray over PySide6/Qt:**
- Dramatically smaller (~50KB vs ~100MB)
- Pure Python, easier to install
- Sufficient for system tray use case
- Works well with pynput for hotkeys

### Core Components

#### 1. State Machine (`state.py`)

```python
from enum import Enum, auto
from dataclasses import dataclass
from typing import Optional, Callable

class TrayState(Enum):
    """Application states."""
    IDLE = auto()
    RECORDING_STARTING = auto()
    RECORDING = auto()
    RECORDING_STOPPING = auto()
    TRAINING = auto()
    TRAINING_PAUSED = auto()
    ERROR = auto()

@dataclass
class AppState:
    """Current application state."""
    state: TrayState = TrayState.IDLE
    current_capture: Optional[str] = None
    training_progress: Optional[float] = None
    error_message: Optional[str] = None

    def can_start_recording(self) -> bool:
        return self.state == TrayState.IDLE

    def can_stop_recording(self) -> bool:
        return self.state == TrayState.RECORDING

class StateManager:
    """Manages application state transitions."""

    def __init__(self):
        self._state = AppState()
        self._listeners: list[Callable[[AppState], None]] = []

    def add_listener(self, callback: Callable[[AppState], None]):
        self._listeners.append(callback)

    def transition(self, new_state: TrayState, **kwargs):
        """Transition to a new state and notify listeners."""
        self._state = AppState(state=new_state, **kwargs)
        for listener in self._listeners:
            listener(self._state)

    @property
    def current(self) -> AppState:
        return self._state
```

#### 2. Main Application (`app.py`)

```python
import sys
import threading
from typing import Optional

import pystray
from PIL import Image

from openadapt_tray.state import StateManager, TrayState
from openadapt_tray.menu import MenuBuilder
from openadapt_tray.icons import IconManager
from openadapt_tray.shortcuts import HotkeyManager
from openadapt_tray.notifications import NotificationManager
from openadapt_tray.ipc import IPCClient
from openadapt_tray.config import TrayConfig
from openadapt_tray.platform import get_platform_handler

class TrayApplication:
    """Main system tray application."""

    def __init__(self, config: Optional[TrayConfig] = None):
        self.config = config or TrayConfig.load()
        self.state = StateManager()
        self.platform = get_platform_handler()

        # Initialize components
        self.icons = IconManager()
        self.notifications = NotificationManager()
        self.menu_builder = MenuBuilder(self)
        self.hotkeys = HotkeyManager(self.config.hotkeys)
        self.ipc = IPCClient()

        # Create tray icon
        self.icon = pystray.Icon(
            name="openadapt",
            icon=self.icons.get(TrayState.IDLE),
            title="OpenAdapt",
            menu=self.menu_builder.build(),
        )

        # Register state change handler
        self.state.add_listener(self._on_state_change)

        # Register hotkey handlers
        self._setup_hotkeys()

    def _setup_hotkeys(self):
        """Configure global hotkeys."""
        self.hotkeys.register(
            self.config.hotkeys.toggle_recording,
            self._toggle_recording
        )
        self.hotkeys.register(
            self.config.hotkeys.open_dashboard,
            self._open_dashboard
        )

    def _on_state_change(self, state):
        """Handle state changes."""
        # Update icon
        self.icon.icon = self.icons.get(state.state)

        # Update menu
        self.icon.menu = self.menu_builder.build()

        # Show notification if appropriate
        self._show_state_notification(state)

    def _show_state_notification(self, state):
        """Show notification for state transitions."""
        messages = {
            TrayState.RECORDING: ("Recording Started", f"Capturing: {state.current_capture}"),
            TrayState.IDLE: ("Recording Stopped", "Capture saved"),
            TrayState.TRAINING: ("Training Started", "Model training in progress"),
            TrayState.ERROR: ("Error", state.error_message or "An error occurred"),
        }
        if state.state in messages:
            title, body = messages[state.state]
            self.notifications.show(title, body)

    def _toggle_recording(self):
        """Toggle recording state."""
        if self.state.current.can_start_recording():
            self.start_recording()
        elif self.state.current.can_stop_recording():
            self.stop_recording()

    def start_recording(self, name: Optional[str] = None):
        """Start a new capture session."""
        if not self.state.current.can_start_recording():
            return

        # Prompt for name if not provided (platform-specific)
        if name is None:
            name = self.platform.prompt_input(
                "New Recording",
                "Enter a name for this capture:"
            )
            if not name:
                return

        self.state.transition(TrayState.RECORDING_STARTING, current_capture=name)

        # Start capture via CLI subprocess or direct API
        threading.Thread(
            target=self._run_capture,
            args=(name,),
            daemon=True
        ).start()

    def _run_capture(self, name: str):
        """Run capture in background thread."""
        try:
            # Option 1: Via subprocess (preferred for isolation)
            import subprocess
            self.capture_process = subprocess.Popen(
                ["openadapt", "capture", "start", "--name", name],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.state.transition(TrayState.RECORDING, current_capture=name)

        except Exception as e:
            self.state.transition(TrayState.ERROR, error_message=str(e))

    def stop_recording(self):
        """Stop the current capture session."""
        if not self.state.current.can_stop_recording():
            return

        self.state.transition(TrayState.RECORDING_STOPPING)

        # Send stop signal to capture process
        if hasattr(self, 'capture_process') and self.capture_process:
            self.capture_process.terminate()

        self.state.transition(TrayState.IDLE)

    def _open_dashboard(self):
        """Open the web dashboard."""
        import webbrowser
        webbrowser.open(f"http://localhost:{self.config.dashboard_port}")

    def run(self):
        """Run the application."""
        # Start hotkey listener
        self.hotkeys.start()

        # Platform-specific setup
        self.platform.setup()

        # Run the tray icon (blocks)
        self.icon.run()

    def quit(self):
        """Quit the application."""
        self.hotkeys.stop()
        self.ipc.close()
        self.icon.stop()

def main():
    """Entry point."""
    app = TrayApplication()
    try:
        app.run()
    except KeyboardInterrupt:
        app.quit()

if __name__ == "__main__":
    main()
```

#### 3. Menu Builder (`menu.py`)

```python
from typing import TYPE_CHECKING, Callable, Optional
from functools import partial

import pystray
from pystray import MenuItem as Item, Menu

if TYPE_CHECKING:
    from openadapt_tray.app import TrayApplication

from openadapt_tray.state import TrayState

class MenuBuilder:
    """Builds the system tray context menu."""

    def __init__(self, app: "TrayApplication"):
        self.app = app

    def build(self) -> Menu:
        """Build the current menu based on application state."""
        state = self.app.state.current

        items = [
            self._build_recording_item(state),
            Menu.SEPARATOR,
            self._build_captures_submenu(),
            self._build_training_item(state),
            Menu.SEPARATOR,
            Item("Open Dashboard", self._open_dashboard),
            Item("Settings...", self._open_settings),
            Menu.SEPARATOR,
            Item("Quit", self._quit),
        ]

        return Menu(*items)

    def _build_recording_item(self, state) -> Item:
        """Build record/stop recording menu item."""
        if state.state == TrayState.RECORDING:
            return Item(
                f"Stop Recording ({state.current_capture})",
                self.app.stop_recording,
            )
        elif state.state in (TrayState.RECORDING_STARTING, TrayState.RECORDING_STOPPING):
            return Item(
                "Recording..." if state.state == TrayState.RECORDING_STARTING else "Stopping...",
                None,
                enabled=False,
            )
        else:
            return Item(
                f"Start Recording ({self.app.config.hotkeys.toggle_recording})",
                self.app.start_recording,
                enabled=state.can_start_recording(),
            )

    def _build_captures_submenu(self) -> Item:
        """Build captures submenu."""
        captures = self._get_recent_captures()

        if not captures:
            return Item(
                "Recent Captures",
                Menu(Item("No captures", None, enabled=False)),
            )

        capture_items = [
            Item(
                f"{c.name} ({c.timestamp})",
                Menu(
                    Item("View", partial(self._view_capture, c.path)),
                    Item("Delete", partial(self._delete_capture, c.path)),
                ),
            )
            for c in captures[:10]  # Limit to 10 most recent
        ]

        capture_items.append(Menu.SEPARATOR)
        capture_items.append(Item("View All...", self._open_captures_list))

        return Item("Recent Captures", Menu(*capture_items))

    def _build_training_item(self, state) -> Item:
        """Build training status/control item."""
        if state.state == TrayState.TRAINING:
            progress = state.training_progress or 0
            return Item(
                f"Training: {progress:.0%}",
                Menu(
                    Item("View Progress", self._open_training_dashboard),
                    Item("Stop Training", self._stop_training),
                ),
            )
        else:
            return Item(
                "Training",
                Menu(
                    Item("Start Training...", self._start_training),
                    Item("View Last Results", self._view_training_results),
                ),
            )

    def _get_recent_captures(self):
        """Get list of recent captures."""
        try:
            from pathlib import Path
            from openadapt_tray.config import TrayConfig

            captures_dir = Path(TrayConfig.load().captures_directory)
            if not captures_dir.exists():
                return []

            # Simple capture detection - look for capture directories
            captures = []
            for d in sorted(captures_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
                if d.is_dir() and (d / "metadata.json").exists():
                    from dataclasses import dataclass
                    from datetime import datetime

                    @dataclass
                    class CaptureInfo:
                        name: str
                        path: str
                        timestamp: str

                    mtime = datetime.fromtimestamp(d.stat().st_mtime)
                    captures.append(CaptureInfo(
                        name=d.name,
                        path=str(d),
                        timestamp=mtime.strftime("%Y-%m-%d %H:%M"),
                    ))

            return captures
        except Exception:
            return []

    def _open_dashboard(self):
        self.app._open_dashboard()

    def _open_settings(self):
        """Open settings dialog."""
        self.app.platform.open_settings_dialog(self.app.config)

    def _quit(self):
        self.app.quit()

    def _view_capture(self, path: str):
        """View a capture."""
        import subprocess
        subprocess.run(["openadapt", "capture", "view", path])

    def _delete_capture(self, path: str):
        """Delete a capture after confirmation."""
        if self.app.platform.confirm_dialog(
            "Delete Capture",
            f"Are you sure you want to delete this capture?\n{path}"
        ):
            import shutil
            shutil.rmtree(path)
            self.app.notifications.show("Capture Deleted", "The capture has been removed.")

    def _open_captures_list(self):
        """Open captures list in dashboard."""
        import webbrowser
        webbrowser.open(f"http://localhost:{self.app.config.dashboard_port}/captures")

    def _open_training_dashboard(self):
        """Open training dashboard."""
        import webbrowser
        webbrowser.open(f"http://localhost:{self.app.config.dashboard_port}/training")

    def _start_training(self):
        """Open training configuration dialog."""
        # This would open a dialog to select capture and model
        self.app.platform.open_training_dialog()

    def _stop_training(self):
        """Stop current training."""
        import subprocess
        subprocess.run(["openadapt", "train", "stop"])
        self.app.state.transition(TrayState.IDLE)

    def _view_training_results(self):
        """View last training results."""
        import subprocess
        subprocess.run(["openadapt", "train", "status"])
```

#### 4. Global Hotkeys (`shortcuts.py`)

```python
from dataclasses import dataclass
from typing import Callable, Dict, Optional
import threading

from pynput import keyboard

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
        self._listener: Optional[keyboard.GlobalHotKeys] = None
        self._ctrl_count = 0
        self._ctrl_timer: Optional[threading.Timer] = None

    def register(self, hotkey: str, handler: Callable):
        """Register a hotkey handler."""
        self._handlers[hotkey] = handler

    def start(self):
        """Start listening for hotkeys."""
        # Build hotkey dict for pynput
        hotkeys = {}
        for combo, handler in self._handlers.items():
            if combo == "<ctrl>+<ctrl>+<ctrl>":
                # Special handling for triple-ctrl
                continue
            hotkeys[combo] = handler

        self._listener = keyboard.GlobalHotKeys(hotkeys)
        self._listener.start()

        # Also listen for triple-ctrl pattern
        if "<ctrl>+<ctrl>+<ctrl>" in self._handlers:
            self._start_ctrl_listener()

    def _start_ctrl_listener(self):
        """Start listener for triple-ctrl pattern."""
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

    def _on_ctrl_press(self):
        """Handle ctrl key press for triple-ctrl detection."""
        self._ctrl_count += 1

        # Reset timer
        if self._ctrl_timer:
            self._ctrl_timer.cancel()

        if self._ctrl_count >= 3:
            self._ctrl_count = 0
            handler = self._handlers.get("<ctrl>+<ctrl>+<ctrl>")
            if handler:
                handler()
        else:
            # Reset count after 500ms
            self._ctrl_timer = threading.Timer(0.5, self._reset_ctrl_count)
            self._ctrl_timer.start()

    def _reset_ctrl_count(self):
        self._ctrl_count = 0

    def stop(self):
        """Stop listening for hotkeys."""
        if self._listener:
            self._listener.stop()
        if hasattr(self, '_key_listener'):
            self._key_listener.stop()
        if self._ctrl_timer:
            self._ctrl_timer.cancel()
```

#### 5. Platform Abstraction (`platform/`)

**Base class (`platform/base.py`):**

```python
from abc import ABC, abstractmethod
from typing import Optional

class PlatformHandler(ABC):
    """Abstract base class for platform-specific functionality."""

    @abstractmethod
    def setup(self):
        """Platform-specific setup."""
        pass

    @abstractmethod
    def prompt_input(self, title: str, message: str) -> Optional[str]:
        """Show input dialog and return user input."""
        pass

    @abstractmethod
    def confirm_dialog(self, title: str, message: str) -> bool:
        """Show confirmation dialog and return result."""
        pass

    @abstractmethod
    def open_settings_dialog(self, config):
        """Open settings dialog."""
        pass

    @abstractmethod
    def open_training_dialog(self):
        """Open training configuration dialog."""
        pass

    def setup_autostart(self, enabled: bool):
        """Configure auto-start on login."""
        pass
```

**macOS implementation (`platform/macos.py`):**

```python
import subprocess
from typing import Optional

from .base import PlatformHandler

class MacOSHandler(PlatformHandler):
    """macOS-specific functionality."""

    def setup(self):
        """Hide from Dock, show only in menu bar."""
        try:
            from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
            NSApplication.sharedApplication().setActivationPolicy_(
                NSApplicationActivationPolicyAccessory
            )
        except ImportError:
            pass  # AppKit not available

    def prompt_input(self, title: str, message: str) -> Optional[str]:
        """Show native macOS input dialog."""
        script = f'''
        tell application "System Events"
            display dialog "{message}" default answer "" with title "{title}"
            return text returned of result
        end tell
        '''
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return None

    def confirm_dialog(self, title: str, message: str) -> bool:
        """Show native macOS confirmation dialog."""
        script = f'''
        tell application "System Events"
            display dialog "{message}" with title "{title}" buttons {{"Cancel", "OK"}} default button "OK"
            return button returned of result
        end tell
        '''
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
            )
            return result.returncode == 0 and "OK" in result.stdout
        except Exception:
            return False

    def open_settings_dialog(self, config):
        """Open settings in default browser."""
        import webbrowser
        webbrowser.open(f"http://localhost:{config.dashboard_port}/settings")

    def open_training_dialog(self):
        """Open training dialog in browser."""
        import webbrowser
        webbrowser.open("http://localhost:8080/training/new")

    def setup_autostart(self, enabled: bool):
        """Configure Launch Agent for auto-start."""
        import os
        from pathlib import Path

        plist_path = Path.home() / "Library/LaunchAgents/ai.openadapt.tray.plist"

        if enabled:
            plist_content = '''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ai.openadapt.tray</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/openadapt-tray</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
</dict>
</plist>'''
            plist_path.parent.mkdir(parents=True, exist_ok=True)
            plist_path.write_text(plist_content)
            subprocess.run(["launchctl", "load", str(plist_path)])
        else:
            if plist_path.exists():
                subprocess.run(["launchctl", "unload", str(plist_path)])
                plist_path.unlink()
```

**Windows implementation (`platform/windows.py`):**

```python
import ctypes
from typing import Optional

from .base import PlatformHandler

class WindowsHandler(PlatformHandler):
    """Windows-specific functionality."""

    def setup(self):
        """Windows-specific setup."""
        pass  # No special setup needed

    def prompt_input(self, title: str, message: str) -> Optional[str]:
        """Show Windows input dialog using ctypes."""
        try:
            import tkinter as tk
            from tkinter import simpledialog

            root = tk.Tk()
            root.withdraw()
            result = simpledialog.askstring(title, message)
            root.destroy()
            return result
        except Exception:
            return None

    def confirm_dialog(self, title: str, message: str) -> bool:
        """Show Windows confirmation dialog."""
        MB_OKCANCEL = 0x01
        MB_ICONQUESTION = 0x20
        IDOK = 1

        result = ctypes.windll.user32.MessageBoxW(
            0, message, title, MB_OKCANCEL | MB_ICONQUESTION
        )
        return result == IDOK

    def open_settings_dialog(self, config):
        import webbrowser
        webbrowser.open(f"http://localhost:{config.dashboard_port}/settings")

    def open_training_dialog(self):
        import webbrowser
        webbrowser.open("http://localhost:8080/training/new")

    def setup_autostart(self, enabled: bool):
        """Configure Windows Registry for auto-start."""
        import winreg

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        app_name = "OpenAdapt"

        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS)

            if enabled:
                import sys
                exe_path = sys.executable.replace("python.exe", "Scripts\\openadapt-tray.exe")
                winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, exe_path)
            else:
                try:
                    winreg.DeleteValue(key, app_name)
                except FileNotFoundError:
                    pass

            winreg.CloseKey(key)
        except Exception:
            pass
```

#### 6. Configuration (`config.py`)

```python
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import json

from openadapt_tray.shortcuts import HotkeyConfig

@dataclass
class TrayConfig:
    """Tray application configuration."""

    # Hotkeys
    hotkeys: HotkeyConfig = field(default_factory=HotkeyConfig)

    # Paths
    captures_directory: str = "~/openadapt/captures"
    training_output_directory: str = "~/openadapt/training"

    # Dashboard
    dashboard_port: int = 8080
    auto_launch_dashboard: bool = True

    # Behavior
    auto_start_on_login: bool = False
    minimize_to_tray: bool = True
    show_notifications: bool = True
    notification_duration_ms: int = 5000

    # Recording
    default_record_audio: bool = True
    default_transcribe: bool = True
    stop_on_triple_ctrl: bool = True

    # Appearance
    use_native_dialogs: bool = True

    @classmethod
    def config_path(cls) -> Path:
        """Get configuration file path."""
        return Path.home() / ".config" / "openadapt" / "tray.json"

    @classmethod
    def load(cls) -> "TrayConfig":
        """Load configuration from file."""
        path = cls.config_path()
        if path.exists():
            try:
                data = json.loads(path.read_text())
                hotkeys_data = data.pop("hotkeys", {})
                return cls(
                    hotkeys=HotkeyConfig(**hotkeys_data),
                    **data
                )
            except Exception:
                pass
        return cls()

    def save(self):
        """Save configuration to file."""
        path = self.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "hotkeys": {
                "toggle_recording": self.hotkeys.toggle_recording,
                "open_dashboard": self.hotkeys.open_dashboard,
                "stop_recording": self.hotkeys.stop_recording,
            },
            "captures_directory": self.captures_directory,
            "training_output_directory": self.training_output_directory,
            "dashboard_port": self.dashboard_port,
            "auto_launch_dashboard": self.auto_launch_dashboard,
            "auto_start_on_login": self.auto_start_on_login,
            "minimize_to_tray": self.minimize_to_tray,
            "show_notifications": self.show_notifications,
            "notification_duration_ms": self.notification_duration_ms,
            "default_record_audio": self.default_record_audio,
            "default_transcribe": self.default_transcribe,
            "stop_on_triple_ctrl": self.stop_on_triple_ctrl,
            "use_native_dialogs": self.use_native_dialogs,
        }

        path.write_text(json.dumps(data, indent=2))
```

#### 7. Notifications (`notifications.py`)

```python
import sys
from typing import Optional

class NotificationManager:
    """Cross-platform notification manager."""

    def __init__(self):
        self._backend = self._detect_backend()

    def _detect_backend(self) -> str:
        """Detect best notification backend for platform."""
        if sys.platform == "darwin":
            return "macos"
        elif sys.platform == "win32":
            return "windows"
        else:
            return "linux"

    def show(
        self,
        title: str,
        body: str,
        icon_path: Optional[str] = None,
        duration_ms: int = 5000,
    ):
        """Show a notification."""
        if self._backend == "macos":
            self._show_macos(title, body)
        elif self._backend == "windows":
            self._show_windows(title, body, icon_path, duration_ms)
        else:
            self._show_linux(title, body, icon_path)

    def _show_macos(self, title: str, body: str):
        """Show notification on macOS."""
        import subprocess
        script = f'''
        display notification "{body}" with title "{title}"
        '''
        subprocess.run(["osascript", "-e", script], capture_output=True)

    def _show_windows(self, title: str, body: str, icon_path: Optional[str], duration_ms: int):
        """Show notification on Windows using pystray's built-in notify."""
        # pystray handles this via icon.notify()
        pass

    def _show_linux(self, title: str, body: str, icon_path: Optional[str]):
        """Show notification on Linux."""
        try:
            import subprocess
            cmd = ["notify-send", title, body]
            if icon_path:
                cmd.extend(["-i", icon_path])
            subprocess.run(cmd, capture_output=True)
        except Exception:
            pass
```

### pyproject.toml

```toml
[project]
name = "openadapt-tray"
version = "0.1.0"
description = "System tray application for OpenAdapt"
readme = "README.md"
requires-python = ">=3.10"
license = "MIT"
authors = [
    {name = "MLDSAI Inc.", email = "richard@mldsai.com"}
]
keywords = ["gui", "system-tray", "menu-bar", "openadapt"]
classifiers = [
    "Development Status :: 3 - Alpha",
    "Intended Audience :: Developers",
    "License :: OSI Approved :: MIT License",
    "Operating System :: MacOS",
    "Operating System :: Microsoft :: Windows",
    "Operating System :: POSIX :: Linux",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.10",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
]

dependencies = [
    "pystray>=0.19.0",
    "Pillow>=9.0.0",
    "pynput>=1.7.0",
    "click>=8.0.0",
]

[project.optional-dependencies]
macos-native = [
    "rumps>=0.4.0",
    "pyobjc-framework-Cocoa>=9.0",
]
dev = [
    "pytest>=8.0.0",
    "pytest-mock>=3.10.0",
    "ruff>=0.1.0",
]
all = [
    "openadapt-tray[macos-native]",
]

[project.scripts]
openadapt-tray = "openadapt_tray.app:main"

[project.gui-scripts]
openadapt-tray-gui = "openadapt_tray.app:main"

[project.urls]
Homepage = "https://openadapt.ai"
Documentation = "https://docs.openadapt.ai"
Repository = "https://github.com/OpenAdaptAI/openadapt-tray"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/openadapt_tray"]

[tool.ruff]
line-length = 88
target-version = "py310"

[tool.pytest.ini_options]
testpaths = ["tests"]
```

## User Experience

### First-Run Experience

1. **Installation**: `pip install openadapt-tray`
2. **Launch**: `openadapt-tray` or via Applications menu
3. **First Run Dialog** (if no config exists):
   - Welcome message
   - Option to configure hotkeys
   - Option to enable auto-start
   - Link to documentation
4. **Tray Icon**: Appears in system tray/menu bar
5. **Dashboard**: Auto-opens (configurable)

### Menu Structure

```
[OpenAdapt Tray Icon]
├── Start Recording (Ctrl+Shift+R)
│   └── [When recording: "Stop Recording (task-name)"]
├── ─────────────
├── Recent Captures
│   ├── login-flow (2024-01-15 14:30)
│   │   ├── View
│   │   └── Delete
│   ├── checkout (2024-01-15 10:15)
│   │   ├── View
│   │   └── Delete
│   ├── ... (up to 10 items)
│   ├── ─────────────
│   └── View All...
├── Training
│   ├── Start Training...
│   └── View Last Results
│   └── [When training: "Training: 45% | View Progress | Stop"]
├── ─────────────
├── Open Dashboard (Ctrl+Shift+D)
├── Settings...
├── ─────────────
└── Quit
```

### Status Icons

| State | Icon Description | Color |
|-------|------------------|-------|
| Idle | OpenAdapt logo | Blue/Gray |
| Recording | Pulsing red dot overlay | Red |
| Recording Starting | Spinning indicator | Yellow |
| Training | Gear icon | Purple |
| Error | Exclamation mark | Red |

### Keyboard Shortcuts

| Action | Default Shortcut | Configurable |
|--------|------------------|--------------|
| Toggle Recording | `Ctrl+Shift+R` | Yes |
| Open Dashboard | `Ctrl+Shift+D` | Yes |
| Stop Recording | `Ctrl Ctrl Ctrl` (triple tap) | Yes |

### Notifications

| Event | Title | Body |
|-------|-------|------|
| Recording Started | "Recording Started" | "Capturing: {task-name}" |
| Recording Stopped | "Recording Stopped" | "Capture saved" |
| Training Started | "Training Started" | "Model training in progress" |
| Training Complete | "Training Complete" | "Model saved to {path}" |
| Error | "Error" | "{error message}" |

## Integration with Ecosystem

### CLI Integration

The tray app delegates to the `openadapt` CLI for all operations:

```python
# Starting a capture
subprocess.Popen(["openadapt", "capture", "start", "--name", name])

# Stopping a capture
subprocess.Popen(["openadapt", "capture", "stop"])

# Starting training
subprocess.Popen(["openadapt", "train", "start", "--capture", capture_path])

# Checking training status
result = subprocess.run(["openadapt", "train", "status"], capture_output=True)
```

### Direct API Integration (Alternative)

For tighter integration, the tray can import sub-packages directly:

```python
try:
    from openadapt_capture import CaptureSession

    session = CaptureSession(name=name, record_audio=True)
    session.start()
except ImportError:
    # Fall back to CLI
    subprocess.Popen(["openadapt", "capture", "start", "--name", name])
```

### Dashboard Integration

- Auto-launches the dashboard web server on startup (configurable)
- "Open Dashboard" opens browser to `http://localhost:8080`
- Settings page accessible via tray menu

## Future Enhancements

1. **Native macOS app** using `rumps` for a more native feel
2. **Electron wrapper** for consistent cross-platform UI
3. **Recording preview** - show recent screenshot in menu
4. **Quick actions** - right-click for immediate actions
5. **Status bar text** - show recording duration on macOS
6. **Multi-monitor support** - select which monitor to record
7. **Cloud sync** - sync captures and settings across devices
8. **Plugin system** - allow third-party menu extensions

## Migration from Legacy

### Compatibility

The new tray app maintains backward compatibility with:
- Legacy stop sequences (`oa.stop`, triple-ctrl)
- PostHog analytics events
- Configuration file locations

### Migration Path

1. Install `openadapt-tray` alongside legacy
2. Both can coexist (different process names)
3. Legacy can be deprecated when new tray is stable
4. Configuration migration script provided

---

*This design enables a lightweight, cross-platform system tray experience while maintaining integration with the OpenAdapt ecosystem's CLI-first architecture.*

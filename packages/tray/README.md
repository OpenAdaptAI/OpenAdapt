# openadapt-tray

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)

<!-- PyPI badges (uncomment once package is published)
[![PyPI version](https://img.shields.io/pypi/v/openadapt-tray.svg)](https://pypi.org/project/openadapt-tray/)
[![Downloads](https://img.shields.io/pypi/dm/openadapt-tray.svg)](https://pypi.org/project/openadapt-tray/)
-->

A cross-platform system tray application for [OpenAdapt](https://openadapt.ai), providing a graphical interface for controlling recording, monitoring training, and accessing settings without using the command line.

## Features

### System Tray Icon

Shows in the menu bar (macOS) or system tray (Windows/Linux) with visual state indicators.

![Tray Icon Idle](docs/screenshots/tray-icon-idle.png)
*Idle state: Blue/gray icon in menu bar*

![Tray Icon Recording](docs/screenshots/tray-icon-recording.png)
*Recording state: Red pulsing icon*

### Native Notifications

Modern notifications with desktop-notifier supporting callbacks and actions.

![Basic Notification](docs/screenshots/notification-basic.png)
*Example: Recording started notification*

![Critical Notification](docs/screenshots/notification-critical.png)
*Example: Error notification with critical urgency*

### Other Features

- **Status Indicators**: Icon changes color based on application state (idle, recording, training, error)
- **Start/Stop Recording**: Control capture sessions via menu or global hotkeys
- **Recent Captures**: Quick access to view or delete recent recordings
- **Training Control**: Start, monitor, and stop model training
- **Cross-Platform**: Works on macOS, Windows, and Linux
- **Lightweight**: Uses pystray (~50KB) instead of heavy Qt dependencies

## Installation

```bash
pip install openadapt-tray
```

For macOS native experience with enhanced menu bar features:

```bash
pip install openadapt-tray[macos-native]
```

## Quick Start

```bash
# Run the tray application
openadapt-tray

# Or run as a Python module
python -m openadapt_tray
```

## Keyboard Shortcuts

| Action | Default Shortcut |
|--------|------------------|
| Toggle Recording | `Ctrl+Shift+R` |
| Open Dashboard | `Ctrl+Shift+D` |
| Stop Recording | `Ctrl` (triple tap) |

Shortcuts are configurable via the settings file or dashboard.

## Menu Structure

![Menu Structure](docs/screenshots/menu-idle.png)
*Full menu in idle state*

```
[OpenAdapt Icon]
├── Start Recording (Ctrl+Shift+R)
│   └── [When recording: "Stop Recording (capture-name)"]
├── ─────────────
├── Recent Captures
│   ├── capture-name (2024-01-15 14:30)
│   │   ├── View
│   │   └── Delete
│   └── View All...
├── Training
│   ├── Start Training...
│   └── View Last Results
├── ─────────────
├── Open Dashboard
├── Settings...
├── ─────────────
└── Quit
```

## Configuration

Configuration is stored in:
- **macOS**: `~/Library/Application Support/openadapt/tray.json`
- **Windows**: `%APPDATA%/openadapt/tray.json`
- **Linux**: `~/.config/openadapt/tray.json`

Example configuration:

```json
{
  "hotkeys": {
    "toggle_recording": "<ctrl>+<shift>+r",
    "open_dashboard": "<ctrl>+<shift>+d",
    "stop_recording": "<ctrl>+<ctrl>+<ctrl>"
  },
  "dashboard_port": 8080,
  "show_notifications": true,
  "stop_on_triple_ctrl": true,
  "auto_start_on_login": false
}
```

## Development

### Setup

```bash
# Clone the repository
git clone https://github.com/OpenAdaptAI/openadapt-tray.git
cd openadapt-tray

# Install in development mode
pip install -e ".[dev]"
```

### Running Tests

```bash
pytest tests/
```

### Project Structure

```
openadapt-tray/
├── src/openadapt_tray/
│   ├── __init__.py           # Package exports
│   ├── __main__.py           # Entry point
│   ├── app.py                # Main TrayApplication class
│   ├── menu.py               # Menu construction
│   ├── icons.py              # Icon management
│   ├── notifications.py      # Cross-platform notifications
│   ├── shortcuts.py          # Global hotkey handling
│   ├── config.py             # Configuration management
│   ├── ipc.py                # Inter-process communication
│   ├── state.py              # Application state machine
│   └── platform/
│       ├── __init__.py       # Platform detection
│       ├── base.py           # Abstract base class
│       ├── macos.py          # macOS-specific
│       ├── windows.py        # Windows-specific
│       └── linux.py          # Linux-specific
├── assets/
│   ├── icons/                # State icons (idle, recording, etc.)
│   └── logo.ico              # Windows icon
├── tests/                    # Test suite
├── pyproject.toml
└── README.md
```

## Dependencies

- **pystray** (>=0.19.0): Cross-platform system tray
- **Pillow** (>=9.0.0): Icon handling
- **pynput** (>=1.7.0): Global hotkeys
- **click** (>=8.0.0): CLI integration
- **desktop-notifier** (>=6.2.0): Modern native notifications

## Integration

The tray application delegates to the `openadapt` CLI for all operations:

```bash
# Commands executed by the tray app
openadapt record <name>          # Start recording
openadapt visualize <path>       # View capture
openadapt train start            # Start training
openadapt train status           # Get training status
```

## Platform Notes

### macOS
- Hides from the Dock, appears only in the menu bar
- Native dialogs via AppleScript
- Auto-start via Launch Agents

### Windows
- Appears in the system tray
- Native dialogs via ctypes/tkinter
- Auto-start via Registry

### Linux
- Requires a desktop environment with system tray support
- Dialogs via zenity, kdialog, or tkinter
- Auto-start via XDG autostart

## License

MIT License - see [LICENSE](LICENSE) for details.

## Contributing

Contributions are welcome! Please see our [contributing guidelines](https://github.com/OpenAdaptAI/OpenAdapt/blob/main/CONTRIBUTING.md).

## Links

- [OpenAdapt Website](https://openadapt.ai)
- [OpenAdapt Documentation](https://docs.openadapt.ai)
- [GitHub Repository](https://github.com/OpenAdaptAI/openadapt-tray)

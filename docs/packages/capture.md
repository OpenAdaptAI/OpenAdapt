# openadapt-capture

GUI recording, event capture, and storage.

**Repository**: [OpenAdaptAI/openadapt-capture](https://github.com/OpenAdaptAI/openadapt-capture)

## Installation

```bash
pip install openadapt[capture]
# or
pip install openadapt-capture
```

## Overview

The capture package records user interactions with desktop and web GUIs, including:

- Screenshots at configurable intervals
- Mouse events (clicks, movement, scrolling)
- Keyboard events (key presses, text input)
- Window and application context
- Timing information

## CLI Commands

### Start Recording

```bash
openadapt capture start --name my-task
```

Options:

- `--name` - Name for the capture session (required)
- `--interval` - Screenshot interval in seconds (default: 0.1)
- `--no-screenshots` - Disable screenshot capture
- `--no-keyboard` - Disable keyboard capture

### Stop Recording

```bash
openadapt capture stop
```

Or press `Ctrl+C` in the recording terminal.

### List Captures

```bash
openadapt capture list
```

### View a Capture

```bash
openadapt capture view my-task
```

### Delete a Capture

```bash
openadapt capture delete my-task
```

## Python API

```python
from openadapt_capture import CaptureSession, Recorder

# Create a capture session
session = CaptureSession(name="my-task")

# Start recording
recorder = Recorder(session)
recorder.start()

# ... user performs actions ...

# Stop recording
recorder.stop()

# Access captured data
events = session.get_events()
screenshots = session.get_screenshots()
```

## Data Format

Captures are stored as JSON/Parquet files:

```
captures/
  my-task/
    metadata.json       # Session metadata
    events.parquet      # Event data
    screenshots/        # Screenshot images
      0001.png
      0002.png
      ...
```

### Event Schema

```python
{
    "timestamp": float,      # Unix timestamp
    "type": str,            # "mouse_click", "key_press", etc.
    "data": {
        # Event-specific data
    },
    "screenshot_id": int    # Reference to screenshot
}
```

## Key Exports

| Export | Description |
|--------|-------------|
| `CaptureSession` | Manages a capture session |
| `Recorder` | Records user interactions |
| `Action` | Represents a user action |
| `MouseEvent` | Mouse event data |
| `KeyboardEvent` | Keyboard event data |

## Platform Support

| Platform | Status |
|----------|--------|
| macOS | Full support (requires [permissions](../getting-started/permissions.md)) |
| Windows | Full support |
| Linux | Full support |

## Related Packages

- [openadapt-privacy](privacy.md) - Scrub PII/PHI from captures
- [openadapt-viewer](viewer.md) - Visualize capture data
- [openadapt-ml](ml.md) - Train models on captures

# Claude Code Instructions for openadapt-capture

## Overview

**openadapt-capture** is the data collection component of the OpenAdapt GUI automation ecosystem. It captures platform-agnostic GUI interaction streams (mouse, keyboard, screen) with time-aligned media for training ML models or replaying workflows.

Key responsibilities:
- Record human demonstrations with mouse, keyboard, and screen capture
- Time-align all events and media (video, audio)
- Process raw events into structured actions (clicks, drags, typing)
- Support privacy scrubbing of sensitive data

**Always use PRs, never push directly to main**

## Quick Commands

```bash
# Install the package
uv add openadapt-capture

# Install with audio support (large download)
uv add "openadapt-capture[audio]"

# Run tests
uv run pytest tests/ -v

# Record a GUI capture
uv run python -c "
from openadapt_capture import Recorder
with Recorder('./my_capture', task_description='Demo task') as recorder:
    input('Perform the task, then press Enter to stop recording...')
"

# Load and analyze a capture
uv run python -c "
from openadapt_capture import Capture
capture = Capture.load('./my_capture')
for action in capture.actions():
    print(f'{action.timestamp}: {action.type} at ({action.x}, {action.y})')
"
```

## Architecture

```
openadapt_capture/
  recorder.py      # Recorder context manager for GUI event capture
  capture.py       # Capture class for loading and iterating events/actions
  platform/        # Platform-specific implementations (Windows, macOS, Linux)
  storage/         # Data persistence (SQLite + media files)
  media/           # Audio/video capture and synchronization
  visualization/   # Demo GIF and HTML viewer generation
```

## Key Components

### Recorder
Main interface for capturing GUI interactions:
- `__enter__` / `__exit__` - Context manager lifecycle
- `record_events()` - Main capture loop
- `event_count` - Total captured events

### Capture
Load and query recorded captures:
- `Capture.load(path)` - Load from directory
- `capture.events()` - Iterator over raw events
- `capture.actions()` - Iterator over processed actions

### Event Types
- Raw: `mouse.move`, `mouse.down`, `mouse.up`, `key.down`, `key.up`, `screen.frame`, `audio.chunk`
- Processed: `click`, `double_click`, `drag`, `scroll`, `type`

## Testing

```bash
uv run pytest tests/ -v
```

## Related Projects

- [openadapt-ml](https://github.com/OpenAdaptAI/openadapt-ml) - Train models on captures
- [openadapt-privacy](https://github.com/OpenAdaptAI/openadapt-privacy) - PII scrubbing
- [openadapt-viewer](https://github.com/OpenAdaptAI/openadapt-viewer) - Visualization
- [openadapt-retrieval](https://github.com/OpenAdaptAI/openadapt-retrieval) - Demo retrieval

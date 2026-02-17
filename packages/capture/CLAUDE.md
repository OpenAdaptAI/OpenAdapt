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

# Run tests (exclude browser bridge tests which need websockets fixtures)
uv run pytest tests/ -v --ignore=tests/test_browser_bridge.py

# Run slow integration tests (requires accessibility permissions)
uv run pytest tests/ -v -m slow

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
  recorder.py      # Multi-process recorder (legacy OpenAdapt record.py architecture)
  capture.py       # CaptureSession class for loading and iterating events/actions
  events.py        # Pydantic event models (MouseMoveEvent, KeyDownEvent, etc.)
  processing.py    # Event merging pipeline (clicks, drags, typing)
  db/              # SQLAlchemy database layer
    __init__.py    # Engine, session factory, Base
    models.py      # Recording, ActionEvent, Screenshot, WindowEvent, PerformanceStat, MemoryStat
    crud.py        # Insert functions, batch writing, post-processing
  window/          # Platform-specific active window capture
  extensions/      # SynchronizedQueue (multiprocessing.Queue wrapper)
  utils.py         # Timestamps, screenshots, monitor dims
  config.py        # Recording config (RECORD_VIDEO, RECORD_AUDIO, etc.)
  video.py         # Video encoding (av/ffmpeg)
  audio.py         # Audio recording + transcription
  visualize/       # Demo GIF and HTML viewer generation
  share.py         # Magic Wormhole sharing
  browser_bridge.py # Browser extension integration
  cli.py           # CLI commands (capture record, capture info, capture share)
```

## Key Components

### Recorder
Multi-process recording system (copied from legacy OpenAdapt):
- `Recorder(capture_dir, task_description)` - Context manager
- Internally runs `record()` which spawns reader threads + writer processes
- Action-gated video capture (only encode frames when user acts)
- Stop via context manager exit or stop sequences (default: `llqq`)

### CaptureSession / Capture
Load and query recorded captures:
- `Capture.load(path)` - Load from capture directory (reads `recording.db`)
- `capture.raw_events()` - List of Pydantic events from SQLAlchemy DB
- `capture.actions()` - Iterator over processed actions (clicks, drags, typing)
- `action.screenshot` - PIL Image at time of action (extracted from video)
- `action.x`, `action.y`, `action.dx`, `action.dy`, `action.button`, `action.text`

### Storage
SQLAlchemy-based per-capture databases:
- Each capture gets its own `recording.db` in the capture directory
- Models: Recording, ActionEvent, Screenshot, WindowEvent, PerformanceStat, MemoryStat
- Writer processes get their own sessions via `get_session_for_path(db_path)`

### Event Types
- Raw: `mouse.move`, `mouse.down`, `mouse.up`, `mouse.scroll`, `key.down`, `key.up`
- Processed: `mouse.singleclick`, `mouse.doubleclick`, `mouse.drag`, `mouse.scroll`, `key.type`

## Testing

```bash
# Fast tests (unit + integration, no recording)
uv run pytest tests/ -v --ignore=tests/test_browser_bridge.py -m "not slow"

# Slow tests (full recording pipeline with pynput synthetic input)
uv run pytest tests/ -v -m slow

# All tests
uv run pytest tests/ -v --ignore=tests/test_browser_bridge.py
```

## Related Projects

- [openadapt-ml](https://github.com/OpenAdaptAI/openadapt-ml) - Train models on captures
- [openadapt-privacy](https://github.com/OpenAdaptAI/openadapt-privacy) - PII scrubbing
- [openadapt-evals](https://github.com/OpenAdaptAI/openadapt-evals) - Benchmark evaluation

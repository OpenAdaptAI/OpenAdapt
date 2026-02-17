# OpenAdapt Capture

[![Build Status](https://github.com/OpenAdaptAI/openadapt-capture/actions/workflows/test.yml/badge.svg)](https://github.com/OpenAdaptAI/openadapt-capture/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)

[![PyPI version](https://img.shields.io/pypi/v/openadapt-capture.svg)](https://pypi.org/project/openadapt-capture/)
[![Downloads](https://img.shields.io/pypi/dm/openadapt-capture.svg)](https://pypi.org/project/openadapt-capture/)

**OpenAdapt Capture** is the data collection component of the [OpenAdapt](https://github.com/OpenAdaptAI) GUI automation ecosystem.

Capture platform-agnostic GUI interaction streams with time-aligned screenshots and audio for training ML models or replaying workflows.

> **Status:** Pre-alpha.

---

## The OpenAdapt Ecosystem

```
                          OpenAdapt GUI Automation Pipeline
                          =================================

    +-----------------+          +------------------+          +------------------+
    |                 |          |                  |          |                  |
    | openadapt-      |  ------> | openadapt-ml     |  ------> |    Deploy        |
    | capture         |  Convert | (Train & Eval)   |  Export  |    (Inference)   |
    |                 |          |                  |          |                  |
    +-----------------+          +------------------+          +------------------+
          |                             |                             |
          v                             v                             v
    - Record GUI                  - Fine-tune VLMs              - Run trained
      interactions                - Evaluate on                   agent on new
    - Mouse, keyboard,              benchmarks (WAA)              tasks
      screen, audio               - Compare models              - Real-time
    - Privacy scrubbing           - Cloud GPU training            automation

```

| Component | Purpose | Repository |
|-----------|---------|------------|
| **openadapt-capture** | Record human demonstrations | [GitHub](https://github.com/OpenAdaptAI/openadapt-capture) |
| **openadapt-ml** | Train and evaluate GUI automation models | [GitHub](https://github.com/OpenAdaptAI/openadapt-ml) |
| **openadapt-privacy** | PII scrubbing for recordings | [GitHub](https://github.com/OpenAdaptAI/openadapt-privacy) |

---

## Installation

```bash
uv add openadapt-capture
```

This includes everything needed to capture and replay GUI interactions (mouse, keyboard, screen recording).

For audio capture with Whisper transcription (large download):

```bash
uv add "openadapt-capture[audio]"
```

## Quick Start

### Capture

```python
from openadapt_capture import Recorder

# Record GUI interactions
with Recorder("./my_capture", task_description="Demo task") as recorder:
    # Captures mouse, keyboard, and screen until context exits
    input("Press Enter to stop recording...")
```

### Replay / Analysis

```python
from openadapt_capture import Capture

# Load and iterate over time-aligned events
capture = Capture.load("./my_capture")

for action in capture.actions():
    # Each action has an associated screenshot
    print(f"{action.timestamp}: {action.type} at ({action.x}, {action.y})")
    screenshot = action.screenshot  # PIL Image at time of action
```

### Low-Level API

```python
from openadapt_capture.db import create_db, get_session_for_path
from openadapt_capture.db import crud
from openadapt_capture.db.models import Recording, ActionEvent

# Create a database
engine, Session = create_db("/path/to/recording.db")
session = Session()

# Insert a recording
recording = crud.insert_recording(session, {
    "timestamp": 1700000000.0,
    "monitor_width": 1920,
    "monitor_height": 1080,
    "platform": "win32",
    "task_description": "My task",
})

# Insert events
crud.insert_action_event(session, recording, 1700000001.0, {
    "name": "click",
    "mouse_x": 100.0,
    "mouse_y": 200.0,
    "mouse_button_name": "left",
    "mouse_pressed": True,
})

# Query events back
from openadapt_capture.capture import CaptureSession
capture = CaptureSession.load("/path/to/capture_dir")
actions = list(capture.actions())
```

## Event Types

**Raw events** (captured):
- `mouse.move`, `mouse.down`, `mouse.up`, `mouse.scroll`
- `key.down`, `key.up`

**Actions** (processed):
- `mouse.singleclick`, `mouse.doubleclick`, `mouse.drag`
- `key.type` (merged keystrokes into text)

## Architecture

The recorder uses a multi-process architecture copied from legacy OpenAdapt:

- **Reader threads**: Capture mouse, keyboard, screen, and window events into a central queue
- **Processor thread**: Routes events to type-specific write queues
- **Writer processes**: Persist events to SQLAlchemy DB (one process per event type)
- **Action-gated video**: Only encodes video frames when user actions occur

```
capture_directory/
├── recording.db           # SQLite: events, screenshots, window events, perf stats
├── oa_recording-{ts}.mp4  # Screen recording (action-gated)
└── audio.flac             # Audio (optional)
```

## Performance Testing

Run a performance test with synthetic input:

```bash
uv run python scripts/perf_test.py
```

This records for 10 seconds using pynput Controllers, then reports:
- Wall/CPU time and memory usage
- Event counts and action types
- Output file sizes
- Memory usage plot (saved to capture directory)

Run integration tests (requires accessibility permissions):

```bash
uv run pytest tests/test_performance.py -v -m slow
```

## Visualization

Generate animated demos and interactive viewers from recordings:

### Animated GIF Demo

```python
from openadapt_capture import Capture, create_demo

capture = Capture.load("./my_capture")
create_demo(capture, output="demo.gif", fps=10, max_duration=15)
```

### Interactive HTML Viewer

```python
from openadapt_capture import Capture, create_html

capture = Capture.load("./my_capture")
create_html(capture, output="viewer.html", include_audio=True)
```

## Sharing Recordings

Share recordings between machines using [Magic Wormhole](https://magic-wormhole.readthedocs.io/):

```bash
# On the sending machine
capture share send ./my_capture
# Shows a code like: 7-guitarist-revenge

# On the receiving machine
capture share receive 7-guitarist-revenge
```

The `share` command compresses the recording, sends it via Magic Wormhole, and extracts it on the receiving end. No account or setup required - just share the code.

## Optional Extras

| Extra | Features |
|-------|----------|
| `audio` | Audio capture + Whisper transcription |
| `privacy` | PII scrubbing ([openadapt-privacy](https://github.com/OpenAdaptAI/openadapt-privacy)) |
| `share` | Recording sharing via Magic Wormhole |
| `all` | Everything |

---

## Development

```bash
uv sync --dev
uv run pytest tests/ -v --ignore=tests/test_browser_bridge.py

# Run slow integration tests (requires accessibility permissions)
uv run pytest tests/ -v -m slow
```

## Related Projects

- [openadapt-ml](https://github.com/OpenAdaptAI/openadapt-ml) - Train and evaluate GUI automation models
- [openadapt-privacy](https://github.com/OpenAdaptAI/openadapt-privacy) - PII detection and scrubbing for recordings
- [openadapt-evals](https://github.com/OpenAdaptAI/openadapt-evals) - Benchmark evaluation for GUI agents
- [Windows Agent Arena](https://github.com/microsoft/WindowsAgentArena) - Benchmark for Windows GUI agents

## License

MIT

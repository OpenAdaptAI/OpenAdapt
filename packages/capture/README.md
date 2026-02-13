# OpenAdapt Capture

[![Build Status](https://github.com/OpenAdaptAI/openadapt-capture/actions/workflows/test.yml/badge.svg)](https://github.com/OpenAdaptAI/openadapt-capture/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)

[![PyPI version](https://img.shields.io/pypi/v/openadapt-capture.svg)](https://pypi.org/project/openadapt-capture/)
[![Downloads](https://img.shields.io/pypi/dm/openadapt-capture.svg)](https://pypi.org/project/openadapt-capture/)

**OpenAdapt Capture** is the data collection component of the [OpenAdapt](https://github.com/OpenAdaptAI) GUI automation ecosystem.

Capture platform-agnostic GUI interaction streams with time-aligned screenshots and audio for training ML models or replaying workflows.

> **Status:** Pre-alpha. See [docs/DESIGN.md](docs/DESIGN.md) for architecture discussion.

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

print(f"Captured {recorder.event_count} events")
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
from openadapt_capture import (
    create_capture, process_events,
    MouseDownEvent, MouseButton,
)

# Create storage (platform and screen size auto-detected)
capture, storage = create_capture("./my_capture")

# Write raw events
storage.write_event(MouseDownEvent(timestamp=1.0, x=100, y=200, button=MouseButton.LEFT))

# Query and process
raw_events = storage.get_events()
actions = process_events(raw_events)  # Merges clicks, drags, typed text
```

## Event Types

**Raw events** (captured):
- `mouse.move`, `mouse.down`, `mouse.up`, `mouse.scroll`
- `key.down`, `key.up`
- `screen.frame`, `audio.chunk`

**Actions** (processed):
- `mouse.singleclick`, `mouse.doubleclick`, `mouse.drag`
- `key.type` (merged keystrokes → text)

## Architecture

```
capture_directory/
├── capture.db      # SQLite: events, metadata
├── video.mp4       # Screen recording
└── audio.flac      # Audio (optional)
```

## Performance Statistics

Track event write latency and analyze capture performance:

```python
from openadapt_capture import Recorder

with Recorder("./my_capture") as recorder:
    input("Press Enter to stop...")

# Access performance statistics
summary = recorder.stats.summary()
print(f"Mean latency: {summary['mean_latency_ms']:.1f}ms")

# Generate performance plot
recorder.stats.plot(output_path="performance.png")
```

![Performance Statistics](docs/images/performance_stats.png)

## Frame Extraction Verification

Compare extracted video frames against original images to verify lossless capture:

```python
from openadapt_capture import compare_video_to_images, plot_comparison

# Compare frames
report = compare_video_to_images(
    "capture/video.mp4",
    [(timestamp, image) for timestamp, image in captured_frames],
)

print(f"Mean diff: {report.mean_diff_overall:.2f}")
print(f"Lossless: {report.is_lossless}")

# Visualize comparison
plot_comparison(report, output_path="comparison.png")
```

![Frame Comparison](docs/images/frame_comparison.png)

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

The HTML viewer includes:
- Timeline scrubber with event markers
- Frame-by-frame navigation
- Synchronized audio playback
- Event list with details panel
- Keyboard shortcuts (Space, arrows, Home/End)

![Capture Viewer](docs/images/viewer_screenshot.png)

### Generate Demo from Command Line

```bash
uv run python scripts/generate_readme_demo.py --duration 10
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
uv run pytest
```

## Related Projects

- [openadapt-ml](https://github.com/OpenAdaptAI/openadapt-ml) - Train and evaluate GUI automation models
- [openadapt-privacy](https://github.com/OpenAdaptAI/openadapt-privacy) - PII detection and scrubbing for recordings
- [openadapt-evals](https://github.com/OpenAdaptAI/openadapt-evals) - Benchmark evaluation for GUI agents
- [Windows Agent Arena](https://github.com/microsoft/WindowsAgentArena) - Benchmark for Windows GUI agents

## License

MIT

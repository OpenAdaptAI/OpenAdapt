# openadapt-capture

GUI interaction capture - platform-agnostic event streams with time-aligned media.

> **Status:** Pre-alpha. See [docs/DESIGN.md](docs/DESIGN.md) for architecture discussion.

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

### Generate Demo from Command Line

```bash
uv run python scripts/generate_readme_demo.py --duration 10
```

## Optional Extras

| Extra | Features |
|-------|----------|
| `audio` | Audio capture + Whisper transcription |
| `privacy` | PII scrubbing (openadapt-privacy) |
| `all` | Everything |

## Development

```bash
uv sync --dev
uv run pytest
```

## License

MIT

# openadapt-capture

Demonstration collection, observation-action capture, and storage.

**Repository**: [OpenAdaptAI/openadapt-capture](https://github.com/OpenAdaptAI/openadapt-capture)

## Installation

```bash
pip install openadapt[capture]
# or
pip install openadapt-capture
```

## Overview

The capture package collects human demonstrations from desktop and web GUIs, including:

- Observations (screenshots) at configurable intervals
- Actions: mouse events (clicks, movement, scrolling)
- Actions: keyboard events (key presses, text input)
- Window and application context
- Timing information for trajectory reconstruction

## CLI Commands

### Start Demonstration Collection

```bash
openadapt capture start --name my-task
```

Options:

- `--name` - Name for the capture session (required)
- `--interval` - Screenshot interval in seconds (default: 0.1)
- `--no-screenshots` - Disable screenshot capture
- `--no-keyboard` - Disable keyboard capture

### Stop Demonstration Collection

```bash
openadapt capture stop
```

Or press `Ctrl+C` in the capture terminal.

### List Demonstrations

```bash
openadapt capture list
```

### View a Demonstration Trajectory

```bash
openadapt capture view my-task
```

### Delete a Demonstration

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

# ... user demonstrates the task ...

# Stop recording
recorder.stop()

# Access captured trajectory data
actions = session.get_actions()
observations = session.get_observations()  # screenshots
```

## Data Format

Demonstrations are stored as JSON/Parquet files:

```
demonstrations/
  my-task/
    metadata.json       # Session metadata
    actions.parquet     # Action data (observation-action pairs)
    observations/       # Screenshot images (observations)
      0001.png
      0002.png
      ...
```

### Action Schema

```python
{
    "timestamp": float,        # Unix timestamp
    "action_type": str,        # "click", "type", "scroll", etc.
    "data": {
        # Action-specific data
    },
    "observation_id": int      # Reference to observation (screenshot)
}
```

## Key Exports

| Export | Description |
|--------|-------------|
| `CaptureSession` | Manages a demonstration collection session |
| `Recorder` | Captures observation-action pairs |
| `Action` | Represents a user action |
| `Observation` | Represents an observation (screenshot) |
| `Trajectory` | Sequence of observation-action pairs |

## Platform Support

| Platform | Status |
|----------|--------|
| macOS | Full support (requires [permissions](../getting-started/permissions.md)) |
| Windows | Full support |
| Linux | Full support |

## Related Packages

- [openadapt-privacy](privacy.md) - Scrub PII/PHI from demonstrations
- [openadapt-viewer](viewer.md) - Visualize trajectories
- [openadapt-ml](ml.md) - Learn policies from demonstrations

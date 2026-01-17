# openadapt-viewer

Trajectory visualization components for demonstration data.

**Repository**: [OpenAdaptAI/openadapt-viewer](https://github.com/OpenAdaptAI/openadapt-viewer)

## Installation

```bash
pip install openadapt[viewer]
# or
pip install openadapt-viewer
```

## Overview

The viewer package provides:

- HTML-based visualization of demonstration trajectories
- Interactive trajectory viewer
- Action timeline display
- Observation galleries

## CLI Commands

### View a Demonstration Trajectory

```bash
openadapt capture view my-task
```

This opens an HTML viewer in your default browser.

Options:

- `--port` - Server port (default: 8080)
- `--no-browser` - Don't open browser automatically

### Start Dashboard Server

```bash
openadapt serve --port 8080
```

Access the dashboard at `http://localhost:8080`.

## Python API

```python
from openadapt_viewer import PageBuilder, HTMLBuilder

# Build a viewer page for a demonstration
builder = PageBuilder(demonstration="my-task")
html = builder.build()

# Save to file
with open("viewer.html", "w") as f:
    f.write(html)

# Or use HTMLBuilder for custom visualizations
html_builder = HTMLBuilder()
html_builder.add_observation(screenshot_path, actions)
html_builder.add_timeline(actions)
html = html_builder.render()
```

## Viewer Features

### Observation Gallery

Browse all captured observations (screenshots) with navigation controls.

### Action Timeline

Interactive timeline showing:

- Mouse actions (clicks, movement)
- Keyboard actions (key presses)
- Observation timestamps
- Action metadata

### Trajectory Playback Controls

- Play/pause trajectory playback
- Speed controls (0.5x, 1x, 2x)
- Step forward/backward
- Jump to specific time

### Export Options

- Export as HTML (static)
- Export as video (MP4)
- Export trajectory log (JSON)

## Key Exports

| Export | Description |
|--------|-------------|
| `PageBuilder` | Builds trajectory viewer pages |
| `HTMLBuilder` | Low-level HTML construction |
| `TimelineWidget` | Action timeline visualization |
| `ObservationGallery` | Observation browser |

## Customization

### Custom Themes

```python
from openadapt_viewer import PageBuilder, Theme

builder = PageBuilder(
    demonstration="my-task",
    theme=Theme.DARK  # or Theme.LIGHT
)
```

### Custom Action Rendering

```python
from openadapt_viewer import PageBuilder, ActionRenderer

class CustomRenderer(ActionRenderer):
    def render_click(self, action):
        return f"<div class='custom-click'>{action}</div>"

builder = PageBuilder(
    demonstration="my-task",
    renderer=CustomRenderer()
)
```

## Related Packages

- [openadapt-capture](capture.md) - Collect demonstrations to visualize
- [openadapt-privacy](privacy.md) - Scrub sensitive data before viewing

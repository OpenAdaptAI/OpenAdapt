# openadapt-viewer

Reusable component library for OpenAdapt visualization, providing building blocks and high-level builders for creating standalone HTML viewers.

**Repository**: [OpenAdaptAI/openadapt-viewer](https://github.com/OpenAdaptAI/openadapt-viewer)

## Installation

```bash
pip install openadapt[viewer]
# or
pip install openadapt-viewer
```

## Overview

The viewer package provides a comprehensive visualization system with:

- **Reusable Components**: Modular UI building blocks (screenshots, playback controls, timelines, metrics)
- **Page Builder**: High-level API for building complete viewer pages
- **Ready-to-Use Viewers**: Benchmark, capture, segmentation, and retrieval viewers
- **Episode Segmentation**: Interactive library of automatically detected task episodes
- **Recording Catalog**: Automatic discovery and selection of recordings
- **Advanced Search**: Token-based search with flexible matching (case-insensitive, partial, order-independent)

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

## Episode Segmentation Viewer

**NEW (January 2026)**: Interactive viewer for automatically segmented task episodes.

### Features

- **Automatic Episode Detection**: ML-powered segmentation identifies distinct tasks within recordings
- **Visual Library**: Thumbnail grid showing all detected episodes
- **Key Frame Gallery**: Important frames from each episode
- **Recording Filtering**: Filter episodes by source recording
- **Advanced Search**: Find episodes by name, description, or steps
- **Auto-Discovery**: Automatically finds and loads the latest episode data

### Usage

```bash
# Generate segmentation viewer with catalog integration
cd openadapt-viewer
python scripts/generate_segmentation_viewer.py --output viewer.html --open
```

### Python API

```python
from openadapt_viewer import generate_segmentation_viewer

# Generate viewer with auto-discovery
viewer_path = generate_segmentation_viewer(
    output_path="segmentation_viewer.html",
    include_catalog=True,  # Enable auto-discovery
)
```

### Episode Data Format

Episodes are stored in JSON files with this structure:

```json
{
  "episodes": [
    {
      "episode_id": "turn-off-nightshift_001",
      "name": "Disable Night Shift",
      "description": "Navigate to settings and disable Night Shift",
      "start_frame": 0,
      "end_frame": 45,
      "key_frames": [0, 15, 30, 45],
      "steps": [
        "Open System Settings",
        "Navigate to Displays",
        "Disable Night Shift"
      ],
      "recording_name": "turn-off-nightshift"
    }
  ]
}
```

## Recording Catalog System

**NEW (January 2026)**: Automatic discovery and indexing of recordings and segmentation results.

### Features

- **Automatic Scanning**: Discovers recordings in openadapt-capture directories
- **SQLite Database**: Indexed at `~/.openadapt/catalog.db`
- **Recording Metadata**: Frame counts, timestamps, file paths
- **Segmentation Results**: Tracks episode files per recording
- **CLI Integration**: Query and list recordings

### Usage

```bash
# Scan for recordings and segmentation results
openadapt-viewer catalog scan

# List all recordings
openadapt-viewer catalog list

# Show statistics
openadapt-viewer catalog stats
```

### Python API

```python
from openadapt_viewer import get_catalog, scan_and_update_catalog

# Scan and index recordings
counts = scan_and_update_catalog()
print(f"Indexed {counts['recordings']} recordings")

# Query catalog
catalog = get_catalog()
recordings = catalog.get_all_recordings()
for rec in recordings:
    print(f"{rec.name}: {rec.frame_count} frames")

# Get segmentation results
seg_results = catalog.get_segmentation_results("turn-off-nightshift")
```

## Advanced Search

**NEW (January 2026)**: Intelligent token-based search algorithm.

### Features

- **Case-Insensitive**: "NightShift" finds "night shift"
- **Token-Based**: "nightshift" finds "Disable night shift" (normalizes spaces)
- **Token Order Independent**: "shift night" finds "night shift"
- **Partial Matching**: "nightsh" finds "nightshift"
- **Multi-Field**: Searches across names, descriptions, steps

### Example

```javascript
// Search episodes
const results = advancedSearch(episodes, "nightshift", ['name', 'description', 'steps']);

// Results include:
// - "Disable Night Shift"
// - "Configure nightshift settings"
// - "Turn off automatic night mode"
```

## Component Library

### Available Components

| Component | Description |
|-----------|-------------|
| `screenshot_display` | Screenshot with overlays (clicks, bounding boxes) |
| `playback_controls` | Play/pause/speed controls |
| `timeline` | Step progress bar |
| `action_display` | Format actions (click, type, scroll) |
| `metrics_grid` | Statistics cards and grids |
| `filter_bar` | Filter dropdowns |
| `selectable_list` | Selectable list component |
| `badge` | Status badges |

### Component Usage

```python
from openadapt_viewer.components import (
    screenshot_display,
    metrics_grid,
    playback_controls,
)

# Screenshot with overlays
html = screenshot_display(
    image_path="screenshot.png",
    overlays=[
        {"type": "click", "x": 0.5, "y": 0.3, "label": "Human"},
        {"type": "click", "x": 0.6, "y": 0.4, "label": "AI", "variant": "predicted"},
    ],
)

# Metrics cards
html = metrics_grid([
    {"label": "Total", "value": 100},
    {"label": "Passed", "value": 75, "color": "success"},
    {"label": "Failed", "value": 25, "color": "error"},
])
```

## Screenshot Automation

**NEW (January 2026)**: Automated screenshot generation for documentation.

### Features

- **Automated Capture**: Single command generates all screenshots
- **Comprehensive Coverage**: All major UI states
- **Consistent Quality**: Same test data and viewports
- **Fast**: Desktop screenshots in ~30 seconds
- **Metadata**: Optional JSON with screenshot details

### Usage

```bash
# Install Playwright (one-time setup)
pip install playwright
playwright install chromium

# Generate all screenshots
openadapt-viewer screenshots segmentation --output screenshots/

# Desktop only (faster)
openadapt-viewer screenshots segmentation --skip-responsive

# With metadata
openadapt-viewer screenshots segmentation --save-metadata
```

## Related Packages

- [openadapt-capture](capture.md) - Collect demonstrations to visualize
- [openadapt-ml](ml.md) - Episode segmentation and training
- [openadapt-privacy](privacy.md) - Scrub sensitive data before viewing
- [openadapt-retrieval](retrieval.md) - Demo search and retrieval

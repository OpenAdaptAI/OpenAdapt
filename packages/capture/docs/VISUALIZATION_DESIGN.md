# Visualization Design

## Goals

1. **README Demo**: Automated GIF/video generation showing a recording with visual event overlays
2. **Interactive Explorer**: Lightweight HTML tool for detailed inspection

## 1. Animated Demo Generator (`visualize.create_demo`)

Generate a GIF or MP4 that visually demonstrates a recording:

```python
from openadapt_capture import Capture
from openadapt_capture.visualize import create_demo

capture = Capture.load("./my_capture")
create_demo(capture, output="demo.gif", fps=10, max_duration=10)
```

### Features
- Extracts frames from video at action timestamps
- Overlays visual indicators:
  - **Clicks**: Red circle with ripple animation
  - **Drags**: Arrow from start to end
  - **Keystrokes**: Text bubble showing typed characters
  - **Scrolls**: Arrow indicating direction
- Adds timestamp and event type labels
- Outputs GIF (for README) or MP4 (for detailed review)

### Implementation
- Use PIL/Pillow for frame manipulation and drawing
- Use `imageio` for GIF generation (lightweight)
- No heavy dependencies (no OpenCV, no moviepy)

## 2. Interactive HTML Viewer (`visualize.create_html`)

Generate a self-contained HTML file for detailed inspection:

```python
from openadapt_capture.visualize import create_html

create_html("./my_capture", output="recording.html")
```

### Features
- **Timeline scrubber**: Drag to navigate through recording
- **Frame viewer**: Shows screenshot at current time
- **Event overlay**: Visual markers on the frame
- **Event list**: Scrollable list of events, highlights current
- **Event details**: Click an event to see full metadata
- **Keyboard shortcuts**: Arrow keys to step through events

### UI Layout
```
+------------------------------------------+
|  Recording: abc123  |  Duration: 45.2s   |
+------------------------------------------+
|                                          |
|           [Screenshot Frame]             |
|              with overlays               |
|                                          |
+------------------------------------------+
|  🔊 [volume]  [|<] [<] [▶] [>] [>|]  10.5s |
|  [==========|========================]    |
+------------------------------------------+
| Events                 | Event Details   |
| ○ 0.0s mouse.move     | timestamp: 10.5 |
| ○ 1.2s key.type "h"   | type: click     |
| ● 10.5s click (150,200)| x: 150, y: 200  |  <- current
| ○ 12.0s drag          | button: left    |
+------------------------------------------+
```

### Audio Integration
- Audio file embedded as base64 data URI (or linked if too large)
- Synced playback with timeline scrubber
- Play/pause controls
- Volume control
- Audio waveform visualization on timeline (optional)
- When scrubbing, audio seeks to match position

### Implementation
- Pure HTML + CSS + vanilla JavaScript
- No external dependencies (all inline)
- Images embedded as base64 data URIs
- Single self-contained HTML file

### Why not Bokeh?
- Bokeh requires Python runtime for some features
- Large JavaScript bundle (~3MB)
- Overkill for simple timeline navigation
- Our needs are simpler: timeline + image + list

## 3. Integration with README

Automated pipeline for README demo:

```bash
# Generate demo from a real capture
uv run python scripts/generate_readme_demo.py --duration 10 --output docs/images/demo.gif
```

The script:
1. Performs a real capture (with accessibility permissions)
2. Processes events to get actions
3. Generates annotated GIF
4. Outputs to docs/images/ for README embedding

README would show:
```markdown
## Demo

![Recording Demo](docs/images/demo.gif)
```

## File Structure

```
openadapt_capture/
├── visualize/
│   ├── __init__.py      # Exports create_demo, create_html
│   ├── demo.py          # GIF/video generation
│   ├── html.py          # HTML viewer generation
│   ├── overlays.py      # Drawing utilities (circles, arrows, text)
│   └── template.html    # HTML template (inlined in html.py)
```

## Dependencies

- **Required**: Pillow (already have)
- **Optional for GIF**: imageio (lightweight, pure Python)
- **No new heavy deps**: No OpenCV, moviepy, Bokeh

## API Summary

```python
from openadapt_capture.visualize import create_demo, create_html

# Generate animated demo
create_demo(
    capture_or_path,
    output="demo.gif",      # or .mp4
    fps=10,
    max_duration=30,        # seconds
    show_events=True,       # overlay markers
    show_labels=True,       # event type labels
)

# Generate interactive HTML
create_html(
    capture_or_path,
    output="recording.html",
    include_all_frames=False,  # Only frames at events
    max_events=100,
)
```

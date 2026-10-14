# OpenAdapt Viewer Component Library Plan

## Executive Summary

This document outlines the refactoring of `openadapt-viewer` from a benchmark-specific viewer into a **reusable component library** that ALL openadapt packages can use. The goal is to provide:

1. **Shared building blocks** (screenshot display, playback controls, metrics panels)
2. **Consistent visual design** across all OpenAdapt tools
3. **Simple integration** - just import and use
4. **Standalone HTML output** - no server required

## Research Summary

### Current State Analysis

**openadapt-viewer** (current):
- Good foundation with Jinja2 templates, Pydantic models
- Only supports benchmark viewing
- Has useful components buried in inline templates (playback, filters, stats)

**openadapt-ml viewers** (~15K lines across 6 files):
- `trainer.py`: Training dashboard with loss curves, eval samples, cost tracking
- `viewer.py`: Capture playback with predictions, transcript sync
- `benchmark_viewer.py`: Complex panels (tasks, live eval, logs, Azure jobs, VMs)
- `shared_ui.py`: Header CSS/HTML shared between viewers (good pattern!)

**Key insight from `viewer_architecture_survey.md`:**
> "Jinja2 templates + Tailwind CSS (CDN) + Alpine.js (CDN) for lightweight interactivity"

### Use Case Requirements

| Use Case | Key Components Needed |
|----------|----------------------|
| **Captures** | Screenshot display with click overlays, playback controls, timeline, transcript sync |
| **Training** | Loss curves (Chart.js), eval sample gallery, metrics cards, cost panel |
| **Benchmarks** | Task list with filters, pass/fail badges, domain stats, step replay |
| **Retrieval** | Similarity scores display, ranked results list, demo preview cards |

### Common Patterns Identified

1. **Screenshot with overlays** - Used in captures, benchmarks, training evals
2. **Playback controls** - Used in captures and benchmarks
3. **Step timeline/progress bar** - Used everywhere
4. **Action display** - Format click/type/scroll actions consistently
5. **Metrics/stats cards** - Used in training and benchmarks
6. **Filter controls** - Domain/status dropdowns, search
7. **List with selection** - Task list, step list, demo list
8. **Dark mode toggle** - All viewers

---

## Target Architecture

```
openadapt-viewer/
├── src/openadapt_viewer/
│   ├── __init__.py               # Package exports
│   ├── cli.py                    # CLI commands
│   │
│   ├── components/               # NEW: Reusable building blocks
│   │   ├── __init__.py           # Export all components
│   │   ├── screenshot.py         # ScreenshotDisplay with overlays
│   │   ├── playback.py           # PlaybackControls (play/pause/speed)
│   │   ├── timeline.py           # Timeline progress bar
│   │   ├── action_display.py     # ActionDisplay (format actions)
│   │   ├── metrics.py            # MetricsCard, MetricsGrid
│   │   ├── filters.py            # FilterDropdown, FilterBar
│   │   ├── list_view.py          # SelectableList, ListItem
│   │   └── charts.py             # LossChart, BarChart (Chart.js wrappers)
│   │
│   ├── templates/                # Jinja2 templates
│   │   ├── base.html             # Base template with CDN imports
│   │   └── components/           # Component-specific templates
│   │       ├── screenshot.html
│   │       ├── playback.html
│   │       ├── timeline.html
│   │       ├── action.html
│   │       ├── metrics.html
│   │       ├── filters.html
│   │       ├── list_view.html
│   │       └── charts.html
│   │
│   ├── styles/                   # NEW: Shared styles
│   │   └── core.css              # CSS variables, base styles
│   │
│   ├── core/                     # Existing utilities
│   │   ├── __init__.py
│   │   ├── types.py              # Pydantic models
│   │   ├── data_loader.py        # Data loading utilities
│   │   └── html_builder.py       # HTMLBuilder class
│   │
│   ├── builders/                 # NEW: High-level page builders
│   │   ├── __init__.py
│   │   ├── page_builder.py       # PageBuilder class
│   │   └── layout.py             # Layout helpers
│   │
│   ├── viewers/                  # Full viewer implementations
│   │   ├── __init__.py
│   │   └── benchmark/            # Existing benchmark viewer
│   │       ├── __init__.py
│   │       ├── data.py
│   │       └── generator.py
│   │
│   └── examples/                 # NEW: Reference implementations
│       ├── __init__.py
│       ├── benchmark_example.py  # How openadapt-evals uses components
│       ├── training_example.py   # How openadapt-ml would use components
│       ├── capture_example.py    # How openadapt-capture would use components
│       └── retrieval_example.py  # How openadapt-retrieval would use components
│
├── tests/
│   ├── test_components/          # NEW: Component tests
│   │   ├── test_screenshot.py
│   │   ├── test_playback.py
│   │   └── ...
│   └── ...
│
└── docs/
    └── component_library_plan.md # This document
```

---

## Component API Design

### Design Principles

1. **Function-based API** - Each component is a function that returns an HTML string
2. **Composable** - Components can be nested and combined
3. **Minimal dependencies** - Only Jinja2 and PIL required
4. **Type-safe** - Pydantic models for data, type hints throughout
5. **Template-based** - HTML in Jinja2 templates, not Python strings

### Component Interface Pattern

```python
# components/screenshot.py
from pathlib import Path
from typing import Optional
from openadapt_viewer.core.html_builder import HTMLBuilder

def screenshot_display(
    image_path: str | Path,
    width: int = 800,
    height: int = 450,
    overlays: list[dict] | None = None,  # [{type: "click", x: 0.5, y: 0.3, label: "H"}]
    caption: str | None = None,
    embed_image: bool = False,
) -> str:
    """Render a screenshot with optional overlays.

    Args:
        image_path: Path to screenshot image
        width: Display width in pixels
        height: Display height in pixels
        overlays: List of overlay markers (clicks, highlights, etc.)
        caption: Optional caption text
        embed_image: If True, embed as base64 data URI

    Returns:
        HTML string for the screenshot display
    """
    builder = HTMLBuilder()
    return builder.render_template(
        "components/screenshot.html",
        image_path=str(image_path),
        width=width,
        height=height,
        overlays=overlays or [],
        caption=caption,
        embed_image=embed_image,
    )
```

### All Components

#### 1. ScreenshotDisplay
```python
screenshot_display(
    image_path: str,
    width: int = 800,
    height: int = 450,
    overlays: list[dict] | None = None,
    caption: str | None = None,
    embed_image: bool = False,
) -> str
```

#### 2. PlaybackControls
```python
playback_controls(
    step_count: int,
    initial_step: int = 0,
    speeds: list[float] = [0.5, 1, 2, 4],
    default_speed: float = 1.0,
    show_step_counter: bool = True,
    alpine_data_name: str = "playback",  # x-data binding name
) -> str
```

#### 3. Timeline
```python
timeline(
    step_count: int,
    current_step: int = 0,
    step_labels: list[str] | None = None,
    clickable: bool = True,
    alpine_data_name: str = "playback",
) -> str
```

#### 4. ActionDisplay
```python
action_display(
    action_type: str,  # "click", "type", "scroll", "key"
    action_details: dict,  # {x: 0.5, y: 0.3} or {text: "hello"}
    show_badge: bool = True,
    show_details: bool = True,
) -> str
```

#### 5. MetricsCard / MetricsGrid
```python
metrics_card(
    label: str,
    value: str | int | float,
    change: float | None = None,  # +5% or -3%
    color: str = "default",  # "success", "error", "warning"
    icon: str | None = None,  # SVG icon name
) -> str

metrics_grid(
    cards: list[dict],  # [{label, value, change, color}]
    columns: int = 4,
) -> str
```

#### 6. FilterBar
```python
filter_bar(
    filters: list[dict],  # [{id, label, options: [{value, label}]}]
    search_placeholder: str | None = None,
    alpine_data_name: str = "filters",
) -> str
```

#### 7. SelectableList
```python
selectable_list(
    items: list[dict],  # [{id, title, subtitle, badge, badge_color}]
    max_height: str = "600px",
    alpine_data_name: str = "list",
) -> str
```

#### 8. LossChart
```python
loss_chart(
    losses: list[dict],  # [{epoch, step, loss}]
    title: str = "Training Loss",
    height: int = 300,
    show_legend: bool = True,
) -> str
```

---

## Page Builder API

For creating complete pages, use the `PageBuilder` class:

```python
from openadapt_viewer.builders import PageBuilder
from openadapt_viewer.components import (
    screenshot_display,
    playback_controls,
    timeline,
    metrics_grid,
)

# Build a page
builder = PageBuilder(title="My Viewer")

# Add header with navigation
builder.add_header(
    title="Benchmark Results",
    subtitle="Model: gpt-5.1",
    nav_tabs=[
        {"href": "training.html", "label": "Training"},
        {"href": "viewer.html", "label": "Viewer", "active": True},
        {"href": "benchmark.html", "label": "Benchmarks"},
    ],
)

# Add content
builder.add_section(
    metrics_grid([
        {"label": "Total Tasks", "value": 100},
        {"label": "Passed", "value": 75, "color": "success"},
        {"label": "Failed", "value": 25, "color": "error"},
        {"label": "Success Rate", "value": "75%"},
    ])
)

builder.add_section(
    f'''
    <div class="grid grid-cols-3 gap-4">
        <div class="col-span-1">{selectable_list(tasks)}</div>
        <div class="col-span-2">
            {screenshot_display(current_screenshot, overlays=overlays)}
            {playback_controls(step_count=len(steps))}
            {timeline(step_count=len(steps))}
        </div>
    </div>
    '''
)

# Generate HTML
html = builder.render()
```

---

## Migration Path

### Phase 1: Extract Components (This PR)

1. Create `components/` directory with Python modules
2. Create `templates/components/` with Jinja2 templates
3. Create `styles/core.css` with shared CSS variables
4. Refactor existing benchmark viewer to use new components
5. Add component tests
6. Update exports in `__init__.py`

### Phase 2: Add Examples

1. Create `examples/` directory
2. Add `benchmark_example.py` showing current benchmark usage
3. Add `training_example.py` showing how openadapt-ml would use components
4. Add `capture_example.py` showing capture playback usage
5. Add `retrieval_example.py` showing retrieval results display

### Phase 3: Integration with Other Packages

**openadapt-ml** can replace embedded HTML with:
```python
from openadapt_viewer.components import (
    screenshot_display,
    playback_controls,
    metrics_grid,
    loss_chart,
)
from openadapt_viewer.builders import PageBuilder
```

**openadapt-evals** can use for benchmark results:
```python
from openadapt_viewer import generate_benchmark_html, BenchmarkRun
```

**openadapt-retrieval** can use for search results:
```python
from openadapt_viewer.components import selectable_list, screenshot_display
```

---

## Implementation Details

### Shared Styles (`styles/core.css`)

```css
/* CSS Variables - consistent with openadapt-ml */
:root {
    --bg-primary: #0a0a0f;
    --bg-secondary: #12121a;
    --bg-tertiary: #1a1a24;
    --border-color: rgba(255, 255, 255, 0.06);
    --text-primary: #f0f0f0;
    --text-secondary: #888;
    --text-muted: #555;
    --accent: #00d4aa;
    --accent-dim: rgba(0, 212, 170, 0.15);
    --success: #34d399;
    --error: #ff5f5f;
    --warning: #f59e0b;
    --info: #3b82f6;
}

/* Light mode overrides */
.light {
    --bg-primary: #ffffff;
    --bg-secondary: #f3f4f6;
    --bg-tertiary: #e5e7eb;
    --border-color: rgba(0, 0, 0, 0.1);
    --text-primary: #111827;
    --text-secondary: #6b7280;
    --text-muted: #9ca3af;
}

/* Component base styles */
.oa-screenshot-container {
    position: relative;
    background: var(--bg-tertiary);
    border-radius: 8px;
    overflow: hidden;
}

.oa-playback-controls {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 12px;
    background: var(--bg-secondary);
    border-radius: 8px;
}

/* ... more component styles ... */
```

### Template Example (`templates/components/screenshot.html`)

```html
{# Screenshot display component #}
<div class="oa-screenshot-container" style="width: {{ width }}px; height: {{ height }}px;">
    {% if embed_image %}
    <img src="data:image/png;base64,{{ image_base64 }}" alt="Screenshot" class="oa-screenshot-image">
    {% else %}
    <img src="{{ image_path }}" alt="Screenshot" class="oa-screenshot-image">
    {% endif %}

    {% for overlay in overlays %}
    <div class="oa-overlay oa-overlay-{{ overlay.type }}"
         style="left: {{ overlay.x * 100 }}%; top: {{ overlay.y * 100 }}%;">
        {% if overlay.label %}
        <span class="oa-overlay-label">{{ overlay.label }}</span>
        {% endif %}
    </div>
    {% endfor %}

    {% if caption %}
    <div class="oa-screenshot-caption">{{ caption }}</div>
    {% endif %}
</div>
```

---

## Backward Compatibility

The existing `generate_benchmark_html()` function will continue to work unchanged:

```python
# This still works exactly as before
from openadapt_viewer.viewers.benchmark import generate_benchmark_html

html_path = generate_benchmark_html(
    data_path="benchmark_results/run_001/",
    output_path="viewer.html",
    standalone=True,
)
```

The refactoring only adds new functionality; it does not break existing APIs.

---

## Testing Strategy

### Component Tests

Each component gets its own test file:

```python
# tests/test_components/test_screenshot.py
import pytest
from openadapt_viewer.components import screenshot_display

def test_screenshot_display_basic():
    html = screenshot_display("test.png")
    assert '<img' in html
    assert 'test.png' in html

def test_screenshot_display_with_overlays():
    html = screenshot_display(
        "test.png",
        overlays=[{"type": "click", "x": 0.5, "y": 0.5, "label": "H"}]
    )
    assert 'oa-overlay' in html
    assert 'H' in html

def test_screenshot_display_embed():
    # Test base64 embedding
    html = screenshot_display("test.png", embed_image=True)
    assert 'data:image' in html
```

### Integration Tests

Test that full pages render correctly:

```python
# tests/test_integration.py
def test_benchmark_viewer_uses_components():
    """Verify benchmark viewer uses component library."""
    from openadapt_viewer.viewers.benchmark import generate_benchmark_html
    from openadapt_viewer.viewers.benchmark.data import create_sample_data

    run = create_sample_data(num_tasks=3)
    html = generate_benchmark_html(run_data=run, output_path="test.html")

    # Should use component classes
    assert 'oa-screenshot' in Path(html).read_text()
    assert 'oa-playback' in Path(html).read_text()
```

---

## Timeline

| Phase | Tasks | Estimate |
|-------|-------|----------|
| **Phase 1** | Extract components, refactor benchmark viewer | 4-6 hours |
| **Phase 2** | Add examples for all use cases | 2-3 hours |
| **Phase 3** | Integration docs, openadapt-ml migration guide | 1-2 hours |

**Total: ~7-11 hours**

---

## Success Criteria

1. All existing tests pass
2. `generate_benchmark_html()` produces identical output
3. Components are importable: `from openadapt_viewer.components import screenshot_display`
4. Examples run and produce valid HTML
5. Documentation is updated (CLAUDE.md, README.md)

---

## Appendix: Component CSS Class Naming

All component CSS classes use the `oa-` prefix (OpenAdapt) to avoid conflicts:

| Component | CSS Classes |
|-----------|-------------|
| Screenshot | `oa-screenshot-container`, `oa-screenshot-image`, `oa-overlay`, `oa-overlay-click` |
| Playback | `oa-playback-controls`, `oa-playback-btn`, `oa-playback-speed` |
| Timeline | `oa-timeline`, `oa-timeline-track`, `oa-timeline-progress` |
| Action | `oa-action`, `oa-action-badge`, `oa-action-details` |
| Metrics | `oa-metrics-card`, `oa-metrics-grid`, `oa-metrics-value` |
| Filters | `oa-filter-bar`, `oa-filter-dropdown`, `oa-filter-search` |
| List | `oa-list`, `oa-list-item`, `oa-list-item-selected` |
| Charts | `oa-chart`, `oa-chart-container`, `oa-chart-legend` |

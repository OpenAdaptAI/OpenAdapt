# Canonical Viewer Pattern

This document defines the canonical pattern for building viewers in openadapt-viewer. All new viewers should follow this pattern for consistency, maintainability, and ease of development.

## Overview

The canonical viewer pattern is **component-based** using the `PageBuilder` API with reusable components. This approach eliminates inline Jinja2 templates and hard-coded HTML in favor of composable, tested building blocks.

**Key Principle:** Viewers are assembled from components, not written from scratch.

## Pattern Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Canonical Pattern                       │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  1. Data Loading (data.py)                                  │
│     └─> Pydantic models + load functions                   │
│                                                             │
│  2. HTML Generation (generator.py)                          │
│     └─> PageBuilder + component composition                │
│                                                             │
│  3. Components (from openadapt_viewer.components)           │
│     └─> Reusable UI building blocks                        │
│                                                             │
│  4. Output (standalone HTML file)                           │
│     └─> Works offline, no server required                  │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

## File Structure

Every viewer follows this structure:

```
viewers/
└── my_viewer/
    ├── __init__.py           # Exports
    ├── data.py               # Data models and loading
    └── generator.py          # HTML generation
```

### data.py

Defines Pydantic models and data loading functions:

```python
"""Data models and loading for my_viewer."""

from pathlib import Path
from pydantic import BaseModel
from typing import Optional


class MyViewerData(BaseModel):
    """Data model for my viewer."""
    name: str
    items: list[dict]
    metadata: dict


def load_data(data_path: Path) -> MyViewerData:
    """Load viewer data from path.

    Args:
        data_path: Path to data directory or file

    Returns:
        Loaded and validated data
    """
    # Load from JSON/pickle/database
    # Validate with Pydantic
    # Return structured data
    pass


def create_sample_data() -> MyViewerData:
    """Create sample data for demo/testing.

    Returns:
        Sample data instance
    """
    return MyViewerData(
        name="Demo",
        items=[...],
        metadata={...}
    )
```

### generator.py

Uses PageBuilder and components to generate HTML:

```python
"""HTML generator for my_viewer."""

from pathlib import Path
from typing import Optional

from openadapt_viewer.builders import PageBuilder
from openadapt_viewer.components import (
    metrics_grid,
    filter_bar,
    selectable_list,
    screenshot_display,
)
from .data import load_data, create_sample_data, MyViewerData


def generate_viewer_html(
    data_path: Optional[Path] = None,
    output_path: Path = Path("my_viewer.html"),
    viewer_data: Optional[MyViewerData] = None,
) -> str:
    """Generate standalone HTML viewer.

    Args:
        data_path: Path to data (optional if viewer_data provided)
        output_path: Where to write HTML file
        viewer_data: Pre-loaded data (optional)

    Returns:
        Path to generated HTML file
    """
    # Load data
    if viewer_data is not None:
        data = viewer_data
    elif data_path is not None:
        data = load_data(data_path)
    else:
        data = create_sample_data()

    # Build page
    builder = PageBuilder(
        title=f"Viewer - {data.name}",
        include_alpine=True,  # For interactivity
    )

    # Header
    builder.add_header(
        title=data.name,
        subtitle="Viewer subtitle",
        nav_tabs=[
            {"href": "index.html", "label": "Home"},
            {"href": "viewer.html", "label": "Viewer", "active": True},
        ],
    )

    # Summary metrics
    builder.add_section(
        metrics_grid([
            {"label": "Total Items", "value": len(data.items)},
            {"label": "Status", "value": "Active", "color": "success"},
        ]),
        title="Summary",
    )

    # Filters
    builder.add_section(
        filter_bar(
            filters=[
                {"id": "type", "label": "Type", "options": [...]},
                {"id": "status", "label": "Status", "options": [...]},
            ],
            search_placeholder="Search items...",
        ),
    )

    # Main content (use components!)
    builder.add_section(
        selectable_list(
            items=[...],
            title="Items",
        ),
    )

    # Write to file
    return str(builder.render_to_file(output_path))
```

## Complete Example: Benchmark Viewer

This is a real example from the codebase showing the canonical pattern in action.

### Step 1: Data Model (data.py)

```python
from pydantic import BaseModel

class BenchmarkTask(BaseModel):
    task_id: str
    instruction: str
    domain: str
    success: bool
    steps: list[dict]

class BenchmarkRun(BaseModel):
    benchmark_name: str
    model_id: str
    tasks: list[BenchmarkTask]

    @property
    def total_tasks(self) -> int:
        return len(self.tasks)

    @property
    def passed_tasks(self) -> int:
        return sum(1 for t in self.tasks if t.success)

    @property
    def success_rate(self) -> float:
        return self.passed_tasks / self.total_tasks if self.total_tasks > 0 else 0
```

### Step 2: HTML Generation (generator.py)

```python
from openadapt_viewer.builders import PageBuilder
from openadapt_viewer.components import metrics_grid, filter_bar, selectable_list

def generate_benchmark_html(run_data: BenchmarkRun, output_path: Path) -> str:
    builder = PageBuilder(title="Benchmark Results", include_alpine=True)

    # Header
    builder.add_header(
        title=run_data.benchmark_name,
        subtitle=f"Model: {run_data.model_id}",
    )

    # Summary metrics
    builder.add_section(
        metrics_grid([
            {"label": "Total Tasks", "value": run_data.total_tasks},
            {"label": "Passed", "value": run_data.passed_tasks, "color": "success"},
            {"label": "Failed", "value": run_data.failed_tasks, "color": "error"},
            {"label": "Success Rate", "value": f"{run_data.success_rate*100:.1f}%", "color": "accent"},
        ]),
        title="Summary",
    )

    # Filters
    domains = list(set(t.domain for t in run_data.tasks))
    builder.add_section(
        filter_bar(
            filters=[
                {"id": "domain", "label": "Domain", "options": domains},
                {"id": "status", "label": "Status", "options": ["passed", "failed"]},
            ],
            search_placeholder="Search tasks...",
        ),
    )

    # Task list
    task_items = [
        {
            "id": t.task_id,
            "title": t.task_id,
            "subtitle": t.instruction[:60] + "...",
            "badge": "Pass" if t.success else "Fail",
            "badge_color": "success" if t.success else "error",
        }
        for t in run_data.tasks
    ]

    builder.add_section(
        selectable_list(task_items, title="Tasks"),
    )

    return str(builder.render_to_file(output_path))
```

### Step 3: Usage

```python
from openadapt_viewer.viewers.benchmark import generate_benchmark_html, BenchmarkRun

# Load or create data
run = BenchmarkRun(
    benchmark_name="Windows Agent Arena",
    model_id="gpt-5.1",
    tasks=[...],
)

# Generate viewer
output = generate_benchmark_html(run_data=run, output_path="viewer.html")
print(f"Generated: {output}")
```

## Available Components

The component library provides 14+ reusable building blocks:

| Component | Purpose | Example Usage |
|-----------|---------|---------------|
| `screenshot_display` | Screenshot with overlays | Capture frames, demo screenshots |
| `playback_controls` | Play/pause/speed controls | Step-through playback |
| `timeline` | Progress bar | Navigation within sequence |
| `action_display` | Format action types | Display click/type/scroll |
| `metrics_card` | Single metric card | Individual stat |
| `metrics_grid` | Grid of metrics | Summary dashboard |
| `filter_bar` | Filter dropdowns + search | Filter and search data |
| `filter_dropdown` | Single dropdown filter | Domain/status filters |
| `selectable_list` | List with selection | Task list, file list |
| `list_item` | Individual list item | Custom list entries |
| `badge` | Status badges | Pass/Fail, Active/Inactive |
| `video_playback` | Video from screenshots | Smooth playback |
| `action_timeline` | Timeline with actions | Action sequence view |
| `comparison_view` | Side-by-side comparison | Before/after, A/B test |
| `failure_analysis_panel` | Failure analysis | Benchmark failures |

### Component Usage Examples

```python
from openadapt_viewer.components import (
    screenshot_display,
    metrics_grid,
    filter_bar,
    selectable_list,
    badge,
)

# Screenshot with click overlays
html = screenshot_display(
    image_path="screenshot.png",
    overlays=[
        {"type": "click", "x": 0.5, "y": 0.3, "label": "H", "variant": "human"},
        {"type": "click", "x": 0.6, "y": 0.4, "label": "AI", "variant": "predicted"},
    ],
    caption="Step 5: Click Submit",
)

# Metrics grid
html = metrics_grid([
    {"label": "Total", "value": 100},
    {"label": "Passed", "value": 75, "color": "success"},
    {"label": "Failed", "value": 25, "color": "error"},
    {"label": "Success Rate", "value": "75%", "color": "accent"},
])

# Filter bar with search
html = filter_bar(
    filters=[
        {"id": "domain", "label": "Domain", "options": ["office", "browser", "system"]},
        {"id": "status", "label": "Status", "options": ["passed", "failed"]},
    ],
    search_placeholder="Search tasks...",
)

# Selectable list
html = selectable_list(
    items=[
        {"id": "task1", "title": "Task 1", "subtitle": "Description...", "badge": "Pass"},
        {"id": "task2", "title": "Task 2", "subtitle": "Description...", "badge": "Fail"},
    ],
    title="Tasks",
    subtitle="Showing 2 items",
)

# Status badge
html = badge("Pass", color="success")
html = badge("Fail", color="error")
```

## Common Patterns

### Pattern 1: Catalog Integration

Use the catalog system for automatic discovery of recordings/data:

```python
from openadapt_viewer import get_catalog

catalog = get_catalog()
recordings = catalog.get_all_recordings()

# Build dropdown of available recordings
recording_options = [
    {"value": r.name, "label": f"{r.name} ({r.frame_count} frames)"}
    for r in recordings
]

builder.add_section(
    filter_bar(
        filters=[
            {"id": "recording", "label": "Recording", "options": recording_options},
        ],
    ),
)
```

### Pattern 2: Search Functionality

Use advanced token-based search (case-insensitive, order-independent):

```javascript
// Inline in HTML (for standalone viewers)
function advancedSearch(items, query, fields = ['name', 'description']) {
    const queryTokens = query
        .toLowerCase()
        .replace(/[^a-z0-9\s]/g, ' ')
        .replace(/\s+/g, ' ')
        .trim()
        .split(' ')
        .filter(t => t.length > 0);

    return items.filter(item => {
        const searchText = fields
            .map(field => item[field] || '')
            .join(' ')
            .toLowerCase()
            .replace(/[^a-z0-9\s]/g, ' ')
            .replace(/\s+/g, ' ');

        return queryTokens.every(queryToken => {
            const searchTokens = searchText.split(' ');
            return searchTokens.some(searchToken =>
                searchToken.includes(queryToken) || queryToken.includes(searchToken)
            );
        });
    });
}
```

See `SEARCH_FUNCTIONALITY.md` for full details.

### Pattern 3: Playback Controls

Use Alpine.js for interactive playback:

```python
builder.add_script('''
function playbackState() {
    return {
        currentStep: 0,
        isPlaying: false,
        playbackSpeed: 1,
        playbackInterval: null,
        steps: [], // Populated with data

        togglePlayback() {
            this.isPlaying ? this.stopPlayback() : this.startPlayback();
        },

        startPlayback() {
            this.isPlaying = true;
            this.playbackInterval = setInterval(() => {
                if (this.currentStep < this.steps.length - 1) {
                    this.currentStep++;
                } else {
                    this.stopPlayback();
                }
            }, 1000 / this.playbackSpeed);
        },

        stopPlayback() {
            this.isPlaying = false;
            if (this.playbackInterval) {
                clearInterval(this.playbackInterval);
                this.playbackInterval = null;
            }
        }
    }
}
''')
```

### Pattern 4: Domain Stats

Display statistics grouped by domain/category:

```python
from openadapt_viewer.components.metrics import domain_stats_grid

# Calculate domain stats
domain_stats = {}
for item in data.items:
    domain = item.domain
    if domain not in domain_stats:
        domain_stats[domain] = {"passed": 0, "failed": 0, "total": 0}
    domain_stats[domain]["total"] += 1
    if item.success:
        domain_stats[domain]["passed"] += 1
    else:
        domain_stats[domain]["failed"] += 1

# Display as grid
builder.add_section(
    domain_stats_grid(domain_stats),
    title="Results by Domain",
)
```

## Anti-Patterns

### ❌ Inline Jinja2 Templates

**DON'T** write inline templates with embedded HTML:

```python
# ❌ BAD: Inline template with hard-coded HTML
template = '''
<!DOCTYPE html>
<html>
<body>
    <div class="metrics">
        {% for metric in metrics %}
        <div class="card">{{ metric.value }}</div>
        {% endfor %}
    </div>
</body>
</html>
'''
html = render_template(template, metrics=metrics)
```

**DO** use PageBuilder and components:

```python
# ✓ GOOD: Component-based
builder = PageBuilder(title="My Viewer")
builder.add_section(
    metrics_grid(metrics),
    title="Summary",
)
html = builder.render()
```

### ❌ Duplicate JavaScript Implementations

**DON'T** reimplement the same JavaScript in every viewer:

```python
# ❌ BAD: Duplicate search implementation
viewer1_js = "function search(query) { /* implementation */ }"
viewer2_js = "function search(query) { /* same implementation */ }"
viewer3_js = "function search(query) { /* same implementation again */ }"
```

**DO** use shared JavaScript modules or inline the canonical implementation:

```python
# ✓ GOOD: Import from shared module
from openadapt_viewer.components.search import get_search_script
builder.add_script(get_search_script())

# OR: Use the advanced search pattern from SEARCH_FUNCTIONALITY.md
```

### ❌ Hard-coded HTML Strings

**DON'T** concatenate HTML strings manually:

```python
# ❌ BAD: Manual HTML construction
html = f'''
<div class="metric">
    <span class="label">{label}</span>
    <span class="value">{value}</span>
</div>
'''
```

**DO** use components that handle HTML generation:

```python
# ✓ GOOD: Component handles HTML
from openadapt_viewer.components import metrics_card
html = metrics_card(label=label, value=value)
```

### ❌ Mixing Data Loading and HTML Generation

**DON'T** load data in the same function that generates HTML:

```python
# ❌ BAD: Mixed concerns
def generate_viewer(data_path):
    # Loading data...
    data = json.load(open(data_path))

    # Generating HTML...
    html = f"<div>{data['name']}</div>"
    return html
```

**DO** separate data loading (data.py) and HTML generation (generator.py):

```python
# ✓ GOOD: Separated concerns

# data.py
def load_data(data_path):
    return json.load(open(data_path))

# generator.py
def generate_viewer(data):
    builder = PageBuilder(title=data['name'])
    # ... use components
    return builder.render()
```

## Testing Your Viewer

Always test your viewer before releasing:

```bash
# Generate with sample data
python -m openadapt_viewer.viewers.my_viewer

# Generate with real data
python -c "
from openadapt_viewer.viewers.my_viewer import generate_viewer_html
generate_viewer_html(data_path='path/to/data', output_path='viewer.html')
"

# Open in browser
open viewer.html

# Check for:
# - All components render correctly
# - Filters and search work
# - Playback controls function (if applicable)
# - Dark/light mode toggle works
# - Responsive design on mobile
```

## Best Practices

1. **Use Pydantic models** for all data structures - enables validation and type hints
2. **Keep files under 500 lines** - split into multiple files if needed
3. **Document with docstrings** - explain intent, not mechanics
4. **Provide sample data** - include `create_sample_data()` for testing
5. **Use type hints** - helps with IDE autocomplete and catches errors
6. **Follow naming conventions** - `snake_case` for files/functions, `PascalCase` for classes
7. **Add tests** - at least one test that generates the viewer with sample data
8. **Include CLI command** - add to `cli.py` for easy access
9. **Update documentation** - add to README.md and ARCHITECTURE.md

## Checklist for New Viewers

- [ ] Created `viewers/my_viewer/` directory
- [ ] Created `data.py` with Pydantic models
- [ ] Created `generator.py` using PageBuilder
- [ ] Created `create_sample_data()` function
- [ ] Used components (no inline HTML)
- [ ] Added type hints throughout
- [ ] Added docstrings to all functions
- [ ] Generated test output with sample data
- [ ] Verified all features work in browser
- [ ] Added CLI command to `cli.py`
- [ ] Updated `README.md` with usage example
- [ ] Updated `ARCHITECTURE.md` if needed

## Migration from Inline HTML

If you have an existing viewer with inline Jinja2 templates, see `MIGRATION_GUIDE.md` for step-by-step instructions on converting to the canonical pattern.

## Related Documentation

- `MIGRATION_GUIDE.md` - Converting inline viewers to component-based
- `ARCHITECTURE.md` - Overall system architecture
- `README.md` - Component usage examples
- `CATALOG_SYSTEM.md` - Using the catalog for data discovery
- `SEARCH_FUNCTIONALITY.md` - Implementing search
- `EPISODE_TIMELINE_QUICKSTART.md` - Adding episode timelines

## Questions?

See existing viewers for reference:
- `viewers/benchmark/` - Complete benchmark viewer implementation
- `examples/benchmark_example.py` - Simplified example
- `examples/capture_example.py` - Capture viewer example
- `examples/training_example.py` - Training dashboard example

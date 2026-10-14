# Migration Guide: Inline HTML to Component-Based Viewers

This guide provides step-by-step instructions for converting existing viewers with inline Jinja2 templates to the canonical component-based pattern.

## Table of Contents

1. [Why Migrate?](#why-migrate)
2. [Migration Overview](#migration-overview)
3. [Step-by-Step Process](#step-by-step-process)
4. [Real Example: Benchmark Viewer](#real-example-benchmark-viewer)
5. [Common Conversions](#common-conversions)
6. [Troubleshooting](#troubleshooting)

## Why Migrate?

**Problem with inline templates:**
- **Duplication**: Same HTML patterns copied across viewers
- **Hard to maintain**: Changes require updating multiple files
- **No reusability**: Can't share components between viewers
- **Testing difficulty**: Can't test components in isolation
- **Inconsistent styling**: Each viewer implements its own CSS

**Benefits of component-based:**
- **Reusability**: Write once, use everywhere
- **Consistency**: All viewers look and behave the same
- **Maintainability**: Fix once, fixes everywhere
- **Testability**: Test components independently
- **Composability**: Build complex UIs from simple blocks

## Migration Overview

```
Before (Inline Template)                  After (Component-Based)
────────────────────────────────────      ────────────────────────────────────

generator.py (500+ lines)                 data.py (100 lines)
├─ load_data()                            ├─ Pydantic models
├─ _generate_html()                       └─ load_data()
└─ _get_template() ← 400 lines HTML
                                          generator.py (100 lines)
                                          ├─ generate_html()
                                          ├─ PageBuilder
                                          └─ Components
```

**Result**: ~500 lines → ~200 lines, more maintainable, more testable.

## Step-by-Step Process

### Step 1: Analyze Current Structure

Identify what your viewer does:

```python
# Current inline template viewer
def generate_viewer(data_path, output_path):
    # 1. Load data
    data = json.load(open(data_path))

    # 2. Process data
    stats = calculate_stats(data)

    # 3. Render template
    template = '''
    <!DOCTYPE html>
    <html>
    ...400 lines of HTML...
    </html>
    '''
    html = render_template(template, data=data, stats=stats)

    # 4. Write output
    with open(output_path, 'w') as f:
        f.write(html)
```

**Identify:**
- What data is loaded?
- What stats/metrics are calculated?
- What UI sections exist? (header, metrics, filters, list, detail view)
- What interactivity is needed? (playback, filtering, search)

### Step 2: Extract Data Models

Create `data.py` with Pydantic models:

```python
# viewers/my_viewer/data.py
"""Data models and loading for my_viewer."""

from pathlib import Path
from pydantic import BaseModel
from typing import Optional


class MyItem(BaseModel):
    """Individual item in the viewer."""
    id: str
    name: str
    status: str
    value: float


class MyViewerData(BaseModel):
    """Complete viewer data."""
    name: str
    items: list[MyItem]

    @property
    def total_items(self) -> int:
        return len(self.items)

    @property
    def success_count(self) -> int:
        return sum(1 for item in self.items if item.status == "success")


def load_data(data_path: Path) -> MyViewerData:
    """Load viewer data from path."""
    import json
    with open(data_path) as f:
        raw_data = json.load(f)

    return MyViewerData(
        name=raw_data["name"],
        items=[MyItem(**item) for item in raw_data["items"]],
    )


def create_sample_data() -> MyViewerData:
    """Create sample data for testing."""
    return MyViewerData(
        name="Sample Data",
        items=[
            MyItem(id="1", name="Item 1", status="success", value=100),
            MyItem(id="2", name="Item 2", status="failed", value=50),
        ],
    )
```

### Step 3: Map HTML to Components

Identify which HTML sections map to which components:

| HTML Section | Component | Notes |
|--------------|-----------|-------|
| `<div class="metrics">...</div>` | `metrics_grid()` | Summary statistics |
| `<select>...</select>` | `filter_bar()` | Filters and search |
| `<ul class="tasks">...</ul>` | `selectable_list()` | Item list |
| `<div class="screenshot">...</div>` | `screenshot_display()` | Images with overlays |
| `<div class="controls">...</div>` | `playback_controls()` | Play/pause buttons |
| `<div class="timeline">...</div>` | `timeline()` | Progress bar |

### Step 4: Create Component-Based Generator

Create `generator.py` using PageBuilder:

```python
# viewers/my_viewer/generator.py
"""HTML generator for my_viewer."""

from pathlib import Path
from typing import Optional

from openadapt_viewer.builders import PageBuilder
from openadapt_viewer.components import (
    metrics_grid,
    filter_bar,
    selectable_list,
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
        include_alpine=True,
    )

    # Add sections
    _add_header(builder, data)
    _add_summary(builder, data)
    _add_filters(builder, data)
    _add_content(builder, data)

    # Write to file
    return str(builder.render_to_file(output_path))


def _add_header(builder: PageBuilder, data: MyViewerData) -> None:
    """Add header section."""
    builder.add_header(
        title=data.name,
        subtitle=f"Total items: {data.total_items}",
    )


def _add_summary(builder: PageBuilder, data: MyViewerData) -> None:
    """Add summary metrics."""
    builder.add_section(
        metrics_grid([
            {"label": "Total", "value": data.total_items},
            {"label": "Success", "value": data.success_count, "color": "success"},
        ]),
        title="Summary",
    )


def _add_filters(builder: PageBuilder, data: MyViewerData) -> None:
    """Add filter bar."""
    statuses = list(set(item.status for item in data.items))
    builder.add_section(
        filter_bar(
            filters=[
                {"id": "status", "label": "Status", "options": statuses},
            ],
            search_placeholder="Search items...",
        ),
    )


def _add_content(builder: PageBuilder, data: MyViewerData) -> None:
    """Add main content."""
    items = [
        {
            "id": item.id,
            "title": item.name,
            "subtitle": f"Status: {item.status}",
            "badge": item.status,
        }
        for item in data.items
    ]

    builder.add_section(
        selectable_list(items, title="Items"),
    )
```

### Step 5: Update __init__.py

Export the new functions:

```python
# viewers/my_viewer/__init__.py
"""My viewer module."""

from .data import MyViewerData, load_data, create_sample_data
from .generator import generate_viewer_html

__all__ = [
    "MyViewerData",
    "load_data",
    "create_sample_data",
    "generate_viewer_html",
]
```

### Step 6: Test Migration

Test with sample data:

```bash
# Test with sample data
python -c "
from openadapt_viewer.viewers.my_viewer import generate_viewer_html
output = generate_viewer_html()
print(f'Generated: {output}')
"

# Open in browser
open my_viewer.html

# Verify:
# - Header appears correctly
# - Metrics display properly
# - Filters work
# - List shows items
# - Dark mode toggle works
```

### Step 7: Clean Up

Remove old inline template code:

```bash
# Before migration
viewers/my_viewer/generator.py (500 lines with inline template)

# After migration
viewers/my_viewer/data.py (100 lines)
viewers/my_viewer/generator.py (100 lines)
viewers/my_viewer/__init__.py (10 lines)
```

## Real Example: Benchmark Viewer

This shows the actual benchmark viewer migration that was completed.

### Before: Inline Template (430 lines)

```python
# OLD: generator.py with inline template
def generate_benchmark_html(data_path, output_path):
    run = load_benchmark_data(data_path)

    template = '''<!DOCTYPE html>
    <html lang="en" x-data="benchmarkViewer()">
    <head>
        <meta charset="UTF-8">
        <title>Benchmark Viewer</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>
    </head>
    <body class="bg-gray-50 dark:bg-gray-900">
        <!-- 400+ lines of inline HTML -->
        <header>...</header>
        <main>
            <div class="grid grid-cols-1 md:grid-cols-4 gap-4">
                <div class="bg-white rounded-lg shadow p-4">
                    <div class="text-sm text-gray-500">Total Tasks</div>
                    <div class="text-2xl font-bold">{{ run.total_tasks }}</div>
                </div>
                <!-- More metrics cards... -->
            </div>

            <!-- Domain stats... -->
            <!-- Filters... -->
            <!-- Task list... -->
            <!-- Step viewer... -->
        </main>
        <script>
            function benchmarkViewer() {
                return {
                    // Alpine.js state...
                }
            }
        </script>
    </body>
    </html>'''

    html = render_template(template, run=run, ...)
    with open(output_path, 'w') as f:
        f.write(html)
```

### After: Component-Based (100 lines)

The actual migration was already completed! Here's the current structure:

```python
# NEW: viewers/benchmark/data.py (100 lines)
class BenchmarkTask(BaseModel):
    task_id: str
    instruction: str
    domain: str
    success: bool

class BenchmarkRun(BaseModel):
    benchmark_name: str
    model_id: str
    tasks: list[BenchmarkTask]

    @property
    def total_tasks(self) -> int:
        return len(self.tasks)

    @property
    def success_rate(self) -> float:
        passed = sum(1 for t in self.tasks if t.success)
        return passed / self.total_tasks if self.total_tasks > 0 else 0

def load_benchmark_data(data_path: Path) -> BenchmarkRun:
    # Load and validate data
    ...
```

```python
# NEW: viewers/benchmark/generator.py (100 lines)
from openadapt_viewer.builders import PageBuilder
from openadapt_viewer.components import metrics_grid, filter_bar, selectable_list

def generate_benchmark_html(run_data: BenchmarkRun, output_path: Path) -> str:
    builder = PageBuilder(title="Benchmark Results", include_alpine=True)

    # Header
    builder.add_header(
        title=run_data.benchmark_name,
        subtitle=f"Model: {run_data.model_id}",
    )

    # Summary
    builder.add_section(
        metrics_grid([
            {"label": "Total Tasks", "value": run_data.total_tasks},
            {"label": "Passed", "value": run_data.passed_tasks, "color": "success"},
            {"label": "Success Rate", "value": f"{run_data.success_rate*100:.1f}%"},
        ]),
        title="Summary",
    )

    # Filters
    builder.add_section(
        filter_bar(
            filters=[
                {"id": "domain", "label": "Domain", "options": [...]},
                {"id": "status", "label": "Status", "options": ["passed", "failed"]},
            ],
        ),
    )

    # Task list
    builder.add_section(
        selectable_list([...], title="Tasks"),
    )

    return str(builder.render_to_file(output_path))
```

**Result:**
- **430 lines → 200 lines** (53% reduction)
- **Better separation of concerns** (data vs. HTML)
- **Reusable components** (can be used in other viewers)
- **Easier to test** (can test data loading separately)
- **Consistent styling** (uses shared CSS variables)

## Common Conversions

### Metrics Cards

**Before (inline HTML):**
```html
<div class="grid grid-cols-4 gap-4">
    <div class="bg-white rounded-lg shadow p-4">
        <div class="text-sm text-gray-500">Total Tasks</div>
        <div class="text-2xl font-bold">{{ total }}</div>
    </div>
    <div class="bg-white rounded-lg shadow p-4">
        <div class="text-sm text-gray-500">Passed</div>
        <div class="text-2xl font-bold text-green-600">{{ passed }}</div>
    </div>
    <!-- More cards... -->
</div>
```

**After (component):**
```python
from openadapt_viewer.components import metrics_grid

builder.add_section(
    metrics_grid([
        {"label": "Total Tasks", "value": total},
        {"label": "Passed", "value": passed, "color": "success"},
    ]),
)
```

### Filter Dropdowns

**Before (inline HTML):**
```html
<div class="flex gap-4">
    <div>
        <label>Domain</label>
        <select x-model="filterDomain">
            <option value="">All</option>
            {% for domain in domains %}
            <option value="{{ domain }}">{{ domain }}</option>
            {% endfor %}
        </select>
    </div>
    <div>
        <label>Status</label>
        <select x-model="filterStatus">
            <option value="">All</option>
            <option value="passed">Passed</option>
            <option value="failed">Failed</option>
        </select>
    </div>
</div>
```

**After (component):**
```python
from openadapt_viewer.components import filter_bar

builder.add_section(
    filter_bar(
        filters=[
            {"id": "domain", "label": "Domain", "options": domains},
            {"id": "status", "label": "Status", "options": ["passed", "failed"]},
        ],
        search_placeholder="Search...",
    ),
)
```

### Task/Item List

**Before (inline HTML):**
```html
<div class="space-y-2">
    <template x-for="task in tasks">
        <div @click="selectTask(task)"
             class="p-3 border rounded cursor-pointer hover:bg-gray-50">
            <div class="font-medium" x-text="task.name"></div>
            <div class="text-sm text-gray-500" x-text="task.description"></div>
            <span class="px-2 py-1 text-xs rounded"
                  :class="task.success ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'"
                  x-text="task.success ? 'Pass' : 'Fail'"></span>
        </div>
    </template>
</div>
```

**After (component):**
```python
from openadapt_viewer.components import selectable_list

items = [
    {
        "id": task.id,
        "title": task.name,
        "subtitle": task.description,
        "badge": "Pass" if task.success else "Fail",
        "badge_color": "success" if task.success else "error",
    }
    for task in tasks
]

builder.add_section(
    selectable_list(items, title="Tasks"),
)
```

### Playback Controls

**Before (inline HTML):**
```html
<div class="flex items-center gap-2">
    <button @click="prevStep()" :disabled="currentStep === 0">Prev</button>
    <button @click="togglePlayback()">
        <span x-text="isPlaying ? 'Pause' : 'Play'"></span>
    </button>
    <button @click="nextStep()" :disabled="currentStep >= totalSteps - 1">Next</button>
    <select x-model="playbackSpeed">
        <option value="0.5">0.5x</option>
        <option value="1">1x</option>
        <option value="2">2x</option>
    </select>
</div>
```

**After (component):**
```python
from openadapt_viewer.components import playback_controls

builder.add_section(
    playback_controls(step_count=len(steps), initial_step=0),
)
```

### Screenshot Display

**Before (inline HTML):**
```html
<div class="relative">
    <img :src="currentStep.screenshot" alt="Screenshot">
    <template x-if="currentStep.action?.type === 'click'">
        <div class="absolute w-6 h-6 border-2 border-green-500 rounded-full"
             :style="'left: ' + (currentStep.action.x * 100) + '%; top: ' + (currentStep.action.y * 100) + '%'">
        </div>
    </template>
</div>
```

**After (component):**
```python
from openadapt_viewer.components import screenshot_display

builder.add_section(
    screenshot_display(
        image_path=step.screenshot,
        overlays=[
            {
                "type": "click",
                "x": step.action.x,
                "y": step.action.y,
                "label": "H",
                "variant": "human",
            }
        ],
    ),
)
```

## Troubleshooting

### Issue: "Component doesn't support my use case"

**Solution:** Compose components or use inline HTML for custom sections:

```python
# Combine components
custom_html = f'''
<div style="display: grid; grid-template-columns: 1fr 2fr; gap: 16px;">
    <div>{selectable_list(items)}</div>
    <div>{screenshot_display(image_path)}</div>
</div>
'''

builder.add_section(custom_html)
```

### Issue: "Lost Alpine.js interactivity"

**Solution:** Add custom scripts with `builder.add_script()`:

```python
builder.add_script('''
function myViewerState() {
    return {
        selectedItem: null,
        currentStep: 0,

        selectItem(item) {
            this.selectedItem = item;
        },

        // ... more Alpine.js methods
    }
}
''')
```

### Issue: "Styling doesn't match old viewer"

**Solution:** Add custom CSS with `builder.add_css()`:

```python
builder.add_css('''
.my-custom-class {
    background: var(--oa-accent);
    padding: 16px;
}
''')
```

### Issue: "Need access to raw HTML template"

**Solution:** For complex cases, you can still use inline templates but try to use components within them:

```python
from openadapt_viewer.components import metrics_grid

# Generate component HTML
metrics_html = metrics_grid([...])

# Embed in custom template
template = f'''
<!DOCTYPE html>
<html>
<body>
    <div class="custom-layout">
        {metrics_html}
        <div class="custom-section">...</div>
    </div>
</body>
</html>
'''
```

## Checklist

Use this checklist to track your migration progress:

- [ ] Analyzed current viewer structure
- [ ] Created `data.py` with Pydantic models
- [ ] Created `load_data()` function
- [ ] Created `create_sample_data()` function
- [ ] Identified HTML sections → component mapping
- [ ] Created `generator.py` with PageBuilder
- [ ] Migrated header section
- [ ] Migrated metrics/summary section
- [ ] Migrated filters section
- [ ] Migrated main content section
- [ ] Migrated interactivity (Alpine.js/JavaScript)
- [ ] Updated `__init__.py` exports
- [ ] Generated test output with sample data
- [ ] Verified all features work in browser
- [ ] Removed old inline template code
- [ ] Updated tests (if applicable)
- [ ] Updated documentation

## Next Steps

After migration:

1. **Test thoroughly** - Verify all features work as before
2. **Update tests** - Add tests for data loading and HTML generation
3. **Update documentation** - Update README and examples
4. **Get feedback** - Have others review the new code
5. **Monitor usage** - Watch for issues in production

## Getting Help

- See `VIEWER_PATTERNS.md` for the canonical pattern
- See existing migrated viewers: `viewers/benchmark/`
- See examples: `examples/benchmark_example.py`
- Check component docs in `README.md`
- Ask in #openadapt-viewer on Slack (if available)

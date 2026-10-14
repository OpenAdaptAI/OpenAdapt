# OpenAdapt Viewer Architecture

This document describes the architecture of the openadapt-viewer package for LLM assistants and developers.

## Overview

openadapt-viewer generates standalone HTML files for visualizing ML training results, benchmark evaluations, and capture recordings. The architecture prioritizes:

1. **Maintainability** - Small, focused files that fit in LLM context windows
2. **Separation of concerns** - Data loading, processing, and presentation are separate
3. **Standalone capability** - Generated HTML files work offline without a server
4. **No build step** - CDN-loaded libraries, no webpack/vite required
5. **Component-based** - Reusable UI building blocks composed with PageBuilder

## Technology Stack

| Layer | Technology | Rationale |
|-------|------------|-----------|
| Data Processing | Pure Python + Pydantic | Type-safe, testable |
| HTML Generation | PageBuilder + Components | Composable, reusable |
| Visualization | Plotly | Best standalone export support |
| Styling | CSS Variables (custom) | Consistent theming, no build step |
| Interactivity | Alpine.js (CDN) | Lightweight (~15KB), declarative |

**Note on Templates:** While we still use Jinja2 for some internal rendering, new viewers should use the PageBuilder API with components rather than writing inline Jinja2 templates.

## Directory Structure

```
openadapt-viewer/
├── pyproject.toml
├── README.md
├── ARCHITECTURE.md              # This file
├── VIEWER_PATTERNS.md           # Canonical viewer pattern guide
├── MIGRATION_GUIDE.md           # Migration from inline HTML
├── src/
│   └── openadapt_viewer/
│       ├── __init__.py          # Package exports
│       ├── cli.py               # CLI entry point
│       │
│       ├── core/                # Shared utilities
│       │   ├── __init__.py
│       │   ├── types.py         # Pydantic models, type definitions
│       │   ├── data_loader.py   # Common data loading utilities
│       │   └── html_builder.py  # Jinja2 environment setup
│       │
│       ├── components/          # Reusable UI components (14+)
│       │   ├── __init__.py
│       │   ├── screenshot.py    # Screenshot with overlays
│       │   ├── playback.py      # Playback controls
│       │   ├── timeline.py      # Progress bar
│       │   ├── action_display.py # Action formatting
│       │   ├── metrics.py       # Stats cards
│       │   ├── filters.py       # Filter dropdowns
│       │   ├── list_view.py     # Selectable lists
│       │   ├── badge.py         # Status badges
│       │   └── ...              # More components
│       │
│       ├── builders/            # High-level page builders
│       │   └── page_builder.py  # PageBuilder class
│       │
│       ├── styles/              # Shared CSS
│       │   └── core.css         # CSS variables and base styles
│       │
│       ├── templates/           # Jinja2 templates (legacy)
│       │   └── base.html        # Base template with CDN imports
│       │
│       ├── viewers/             # Full viewer implementations
│       │   ├── __init__.py
│       │   └── benchmark/       # Benchmark viewer (component-based)
│       │       ├── __init__.py
│       │       ├── data.py      # Data models and loading
│       │       └── generator.py # HTML generation with PageBuilder
│       │
│       └── examples/            # Reference implementations
│           ├── benchmark_example.py
│           ├── capture_example.py
│           ├── training_example.py
│           └── retrieval_example.py
```

## Key Design Patterns

### 1. Component-Based Architecture (Canonical)

**All new viewers use this pattern.** Viewers are built by composing reusable components:

```python
from openadapt_viewer.builders import PageBuilder
from openadapt_viewer.components import metrics_grid, filter_bar

builder = PageBuilder(title="My Viewer")
builder.add_section(metrics_grid([...]))
builder.add_section(filter_bar([...]))
html = builder.render()
```

**Benefits:**
- Reusable components across all viewers
- Consistent styling and behavior
- Easier to test and maintain
- Composable - build complex UIs from simple blocks

See `VIEWER_PATTERNS.md` for complete details.

### 2. Vertical Slice Architecture

Each viewer type (benchmark, training, recording) is a self-contained module with:
- **data.py** - Pydantic models and data loading functions
- **generator.py** - HTML generation using PageBuilder + components

This allows LLMs to understand and modify one viewer without loading the entire codebase.

### 3. PageBuilder API

The PageBuilder class provides a fluent API for constructing HTML pages:

```python
builder = PageBuilder(title="My Viewer", include_alpine=True)

builder.add_header(
    title="Results",
    subtitle="Model: gpt-5.1",
    nav_tabs=[{"href": "index.html", "label": "Home"}],
)

builder.add_section(
    metrics_grid([{"label": "Total", "value": 100}]),
    title="Summary",
)

html = builder.render()
```

**Features:**
- Automatic CDN imports (Alpine.js, Chart.js, Plotly)
- Consistent styling with CSS variables
- Dark/light mode toggle
- Custom scripts and CSS

### 4. Data/Presentation Separation

Data loading and HTML generation are strictly separated:

```python
# data.py - Pure data operations
from pydantic import BaseModel

class BenchmarkTask(BaseModel):
    task_id: str
    status: str
    metrics: dict

def load_benchmark_data(path: str) -> list[BenchmarkTask]:
    # Load and validate data, no HTML concerns
    ...
```

```python
# generator.py - HTML generation using PageBuilder
from openadapt_viewer.builders import PageBuilder
from openadapt_viewer.components import metrics_grid
from .data import load_benchmark_data

def generate_benchmark_html(data_path: str, output_path: str) -> None:
    tasks = load_benchmark_data(data_path)

    builder = PageBuilder(title="Benchmark Results")
    builder.add_section(metrics_grid([...]))
    builder.render_to_file(output_path)
```

### 5. Standalone HTML Generation

Generated HTML files are fully self-contained:
- Plotly.js can be embedded or loaded from CDN
- Data is embedded as inline JSON
- No external dependencies required for viewing

```python
# CDN mode (smaller file, requires internet)
fig.to_html(include_plotlyjs='cdn')

# Standalone mode (larger file, works offline)
fig.to_html(include_plotlyjs=True)
```

## File Size Guidelines

To maintain LLM-friendliness:
- Keep files under **500 lines**
- One responsibility per file
- Use type hints throughout
- Add docstrings explaining intent, not mechanics

## Migration from Inline HTML

**Historical Context:** Earlier versions of openadapt-viewer used inline Jinja2 templates with hard-coded HTML. These have been refactored to use the component-based pattern.

**Current Status:**
- ✅ **Benchmark viewer** - Migrated to PageBuilder + components (January 2026)
- ✅ **Capture viewer** - Uses component-based approach
- ✅ **Examples** - All examples use PageBuilder API
- 📦 **Standalone viewers** - Some standalone HTML files (e.g., `segmentation_viewer.html`) embed components inline

**Why Migrate?**

| Before (Inline Templates) | After (Components) |
|---------------------------|-------------------|
| 430+ lines per viewer | ~200 lines per viewer |
| Duplicated HTML patterns | Shared components |
| Hard to maintain | Easy to update |
| Inconsistent styling | Consistent theming |
| Can't test components | Testable building blocks |

**How to Migrate:**

See `MIGRATION_GUIDE.md` for complete step-by-step instructions. The process:

1. Extract data models to `data.py`
2. Map HTML sections to components
3. Rebuild with PageBuilder + components
4. Test with sample data
5. Remove old inline template code

**Example:**

```python
# BEFORE: Inline template (430 lines)
template = '''<!DOCTYPE html>
<html>
...400 lines of hard-coded HTML...
</html>'''

# AFTER: Component-based (100 lines)
builder = PageBuilder(title="Results")
builder.add_section(metrics_grid([...]))
builder.add_section(filter_bar([...]))
html = builder.render()
```

Result: **53% code reduction**, better maintainability, consistent styling.

## Adding a New Viewer

**Use the canonical pattern from `VIEWER_PATTERNS.md`:**

1. Create a new directory under `viewers/`:
   ```
   viewers/myviewer/
   ├── __init__.py
   ├── data.py      # Pydantic models + load_data()
   └── generator.py # PageBuilder + components
   ```

2. Define Pydantic models in `data.py`

3. Implement generation using PageBuilder in `generator.py`:
   ```python
   from openadapt_viewer.builders import PageBuilder
   from openadapt_viewer.components import metrics_grid, filter_bar

   def generate_viewer_html(data, output_path):
       builder = PageBuilder(title="My Viewer")
       builder.add_section(metrics_grid([...]))
       return builder.render_to_file(output_path)
   ```

4. Add CLI command in `cli.py`

5. Update this document and `README.md`

**DO NOT** write inline Jinja2 templates. Use components.

## CDN Resources

The following CDN resources are loaded in `base.html`:

| Library | CDN URL | Version | Size |
|---------|---------|---------|------|
| Tailwind CSS | cdn.tailwindcss.com | 3.x | ~100KB (JIT) |
| Alpine.js | cdn.jsdelivr.net/npm/alpinejs | 3.x | ~15KB |
| Plotly.js | cdn.plot.ly/plotly-2.32.0.min.js | 2.32.0 | ~3.5MB |

For offline/standalone mode, Plotly.js is embedded directly in the HTML.

## Testing

Run tests with:
```bash
uv run pytest
```

Test files should mirror the source structure:
```
tests/
├── test_core/
│   ├── test_types.py
│   ├── test_data_loader.py
│   └── test_html_builder.py
└── test_viewers/
    └── test_benchmark/
        ├── test_data.py
        └── test_generator.py
```

## Related Projects

- **openadapt-ml** - ML training pipeline (source of training data)
- **openadapt-evals** - Benchmark evaluation infrastructure
- **openadapt-capture** - Recording capture tool

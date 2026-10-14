# Claude Code Instructions for openadapt-viewer

## Project Status & Priorities

**IMPORTANT**: Before starting work, always check the project-wide status document:
- **Location**: `/path/to/STATUS.md`
- **Purpose**: Tracks P0 priorities, active background tasks, blockers, and strategic decisions
- **Action**: Read this file at the start of every session to understand current priorities

This ensures continuity between Claude Code sessions and context compactions.

---

## Architectural Decisions

**episodes.json vs capture.db** (2026-01-17):
- **Decision**: Keep episodes.json as separate JSON file (NOT in capture.db)
- **Rationale**: Separation of concerns (raw events vs ML semantics), zero performance benefit, high migration cost
- **Documents**: [EPISODES_DB_DECISION.md](EPISODES_DB_DECISION.md) (summary), [EPISODES_DB_ANALYSIS.md](EPISODES_DB_ANALYSIS.md) (full analysis)
- **Reference**: [EPISODES_DB_SCHEMA_REFERENCE.sql](EPISODES_DB_SCHEMA_REFERENCE.sql) (if requirements change)

---

## Overview

**Reusable component library** for OpenAdapt visualization. Provides building blocks (components) and high-level builders for creating standalone HTML viewers.

**Migration Status (January 2026)**:
- Phase 1 foundation work complete in openadapt-ml
- Adapter module (`viewer_components.py`) established for ML-specific use cases
- Progressive migration from inline HTML generation to component-based approach

Used by:
- **openadapt-ml**: Training dashboards (migrating to components)
- **openadapt-evals**: Benchmark result viewers
- **openadapt-capture**: Capture playback viewers
- **openadapt-retrieval**: Demo search result viewers

## Quick Start

```bash
# Install
cd /path/to/openadapt-viewer
uv sync

# Run tests (IMPORTANT: Run before making changes!)
uv run pytest tests/ -v

# Run tests with coverage
uv run pytest tests/ --cov=openadapt_viewer --cov-report=html

# Generate demo benchmark viewer
uv run openadapt-viewer demo --tasks 5 --output viewer.html

# Generate screenshots for documentation
uv run openadapt-viewer screenshots segmentation --output screenshots/

# Run examples
uv run python -m openadapt_viewer.examples.benchmark_example
uv run python -m openadapt_viewer.examples.training_example
uv run python -m openadapt_viewer.examples.capture_example
uv run python -m openadapt_viewer.examples.retrieval_example
```

## Architecture

```
openadapt_viewer/
├── components/               # Reusable UI building blocks
│   ├── screenshot.py         # Screenshot with overlays
│   ├── playback.py           # Play/pause/speed controls
│   ├── timeline.py           # Step progress bar
│   ├── episode_timeline.js   # Episode timeline with navigation (JS)
│   ├── action_display.py     # Format actions (click, type, etc.)
│   ├── metrics.py            # Stats cards and grids
│   ├── filters.py            # Filter dropdowns
│   ├── list_view.py          # Selectable list component
│   └── badge.py              # Status badges
│
├── builders/                 # High-level page builders
│   └── page_builder.py       # PageBuilder class
│
├── styles/                   # Shared CSS
│   └── core.css              # CSS variables and base styles
│
├── core/                     # Core utilities
│   ├── types.py              # Pydantic data models
│   ├── data_loader.py        # Data loading utilities
│   └── html_builder.py       # Jinja2 HTMLBuilder
│
├── viewers/                  # Full viewer implementations
│   └── benchmark/            # Benchmark results viewer
│       ├── generator.py      # generate_benchmark_html()
│       └── data.py           # Data loading
│
├── examples/                 # Reference implementations
│   ├── benchmark_example.py  # openadapt-evals usage
│   ├── training_example.py   # openadapt-ml usage
│   ├── capture_example.py    # openadapt-capture usage
│   └── retrieval_example.py  # openadapt-retrieval usage
│
├── templates/                # Jinja2 templates
│   └── base.html             # Base HTML template
│
└── __init__.py               # Package exports
```

## Component Usage

### Individual Components

Each component returns an HTML string:

```python
from openadapt_viewer.components import (
    screenshot_display,
    playback_controls,
    metrics_grid,
    filter_bar,
    selectable_list,
    badge,
)

# Screenshot with overlays
html = screenshot_display(
    image_path="screenshot.png",
    overlays=[
        {"type": "click", "x": 0.5, "y": 0.3, "label": "H", "variant": "human"},
        {"type": "click", "x": 0.6, "y": 0.4, "label": "AI", "variant": "predicted"},
    ],
    caption="Step 5",
)

# Metrics cards
html = metrics_grid([
    {"label": "Total", "value": 100},
    {"label": "Passed", "value": 75, "color": "success"},
    {"label": "Failed", "value": 25, "color": "error"},
])

# Playback controls (requires Alpine.js)
html = playback_controls(step_count=20, initial_step=0)

# Filter bar
html = filter_bar(
    filters=[
        {"id": "domain", "label": "Domain", "options": ["office", "browser"]},
        {"id": "status", "label": "Status", "options": ["passed", "failed"]},
    ],
    search_placeholder="Search tasks...",
)
```

### Page Builder

Build complete pages from components:

```python
from openadapt_viewer.builders import PageBuilder
from openadapt_viewer.components import metrics_grid, screenshot_display

builder = PageBuilder(title="My Viewer", include_alpine=True)

builder.add_header(
    title="Results",
    subtitle="Model: gpt-5.1",
    nav_tabs=[
        {"href": "dashboard.html", "label": "Training"},
        {"href": "viewer.html", "label": "Viewer", "active": True},
    ],
)

builder.add_section(
    metrics_grid([...]),
    title="Summary",
)

builder.add_section(
    screenshot_display("screenshot.png"),
)

# Render to string
html = builder.render()

# Or write to file
path = builder.render_to_file("output.html")
```

### Ready-to-Use Viewers

```python
# Benchmark viewer
from openadapt_viewer.viewers.benchmark import generate_benchmark_html
generate_benchmark_html(data_path="results/", output_path="viewer.html")

# Or with data
from openadapt_viewer import BenchmarkRun, generate_benchmark_html
run = BenchmarkRun(...)
generate_benchmark_html(run_data=run, output_path="viewer.html")
```

## CSS Classes

All component classes use the `oa-` prefix:

| Component | Classes |
|-----------|---------|
| Screenshot | `oa-screenshot-container`, `oa-overlay`, `oa-overlay-click` |
| Playback | `oa-playback-controls`, `oa-playback-btn`, `oa-playback-speed` |
| Timeline | `oa-timeline`, `oa-timeline-track`, `oa-timeline-progress` |
| Metrics | `oa-metrics-card`, `oa-metrics-grid`, `oa-metrics-value` |
| Filters | `oa-filter-bar`, `oa-filter-dropdown` |
| List | `oa-list`, `oa-list-item`, `oa-list-item-selected` |
| Badge | `oa-badge`, `oa-badge-success`, `oa-badge-error` |

## CSS Variables

Core CSS variables in `styles/core.css`:

```css
:root {
    --oa-bg-primary: #0a0a0f;
    --oa-bg-secondary: #12121a;
    --oa-text-primary: #f0f0f0;
    --oa-accent: #00d4aa;
    --oa-success: #34d399;
    --oa-error: #ff5f5f;
    /* ... */
}
```

## Testing

```bash
# Run all tests
uv run pytest tests/ -v

# Run component tests
uv run pytest tests/test_components/ -v

# Check imports work
uv run python -c "from openadapt_viewer import screenshot_display, PageBuilder; print('OK')"
```

## Integration Examples

### openadapt-ml (Training Dashboard)

```python
from openadapt_viewer.builders import PageBuilder
from openadapt_viewer.components import metrics_grid, screenshot_display

def generate_training_dashboard(state, config):
    builder = PageBuilder(title="Training", include_chartjs=True)
    builder.add_header(title="Training Dashboard", subtitle=f"Model: {state.model_name}")
    builder.add_section(metrics_grid([
        {"label": "Epoch", "value": state.epoch},
        {"label": "Loss", "value": f"{state.loss:.4f}"},
    ]))
    return builder.render()
```

### openadapt-evals (Benchmark Viewer)

```python
from openadapt_viewer import generate_benchmark_html, BenchmarkRun

run = BenchmarkRun.from_directory("results/run_001/")
generate_benchmark_html(run_data=run, output_path="benchmark.html")
```

### openadapt-retrieval (Search Results)

```python
from openadapt_viewer.builders import PageBuilder
from openadapt_viewer.components import screenshot_display, selectable_list

def generate_retrieval_viewer(results):
    builder = PageBuilder(title="Search Results")
    for result in results:
        builder.add_section(f'''
            {screenshot_display(result.screenshot)}
            <div>Similarity: {result.score:.3f}</div>
        ''')
    return builder.render()
```

## Testing

**CRITICAL**: Always run tests before making changes to verify nothing is broken, and after changes to ensure no regressions.

### Quick Testing Commands

```bash
# Run all tests
uv run pytest tests/ -v

# Run specific category
uv run pytest tests/unit/ -v           # Fast unit tests
uv run pytest tests/component/ -v      # Component tests
uv run pytest tests/integration/ -v    # Integration tests

# Run specific file
uv run pytest tests/component/test_screenshot.py -v

# Run with coverage
uv run pytest tests/ --cov=openadapt_viewer --cov-report=html
open htmlcov/index.html
```

### Writing Tests for New Features

**Always write tests BEFORE or alongside implementing features** (TDD approach):

```python
# tests/component/test_new_feature.py
def test_new_feature_basic_rendering(page):
    """Test that new feature renders correctly."""
    from openadapt_viewer.components import new_feature

    html = new_feature(data="test")
    page.set_content(html)

    assert page.locator(".oa-new-feature").is_visible()
    assert page.locator(".oa-new-feature").text_content() == "test"
```

### Testing Philosophy

1. **Test at the lowest level possible**: Unit tests for logic, component tests for rendering, integration tests for workflows
2. **Use semantic selectors**: `page.get_by_role("button", name="Submit")` not `#submit-btn-123`
3. **Test user behavior**: What the user sees/does, not implementation details
4. **Keep tests independent**: Each test should work in isolation
5. **Use fixtures**: Reuse common setup with pytest fixtures

### For Claude Code

**When a test fails**:
1. Read the error message to understand what failed
2. Look at the line marked with `>` (the failing assertion)
3. Check the component code that generates the HTML
4. Fix the component to make the test pass
5. Run tests again to verify

**When adding a new feature**:
1. Write a test that describes what the user should see/do
2. Run the test (it will fail - "Red")
3. Implement the feature to make the test pass ("Green")
4. Refactor if needed, tests should still pass ("Refactor")

### Documentation

- [TESTING_STRATEGY.md](TESTING_STRATEGY.md) - Comprehensive testing strategy and architecture
- [docs/TESTING_GUIDE.md](docs/TESTING_GUIDE.md) - Practical guide for writing and running tests
- [pytest-playwright docs](https://playwright.dev/python/docs/intro) - Playwright Python API

## Screenshot Generation

**NEW (January 2026)**: Automated screenshot generation system for capturing viewer UI states systematically.

### Quick Start

```bash
# Install Playwright (one-time setup)
uv pip install playwright
uv run playwright install chromium

# Generate all screenshots (desktop + responsive)
uv run openadapt-viewer screenshots segmentation --output screenshots/

# Desktop only (faster, ~30 seconds)
uv run openadapt-viewer screenshots segmentation --skip-responsive

# With metadata JSON
uv run openadapt-viewer screenshots segmentation --save-metadata
```

### What Gets Generated

The system automatically captures 13+ screenshots:

**Desktop (1920x1080)**:
1. Initial empty state
2. Episodes loaded with thumbnails
3. Episode details expanded
4. Key frames gallery
5. Search functionality
6. Filter controls
7. Full page view

**Responsive**:
8-9. Tablet views (768x1024)
10-11. Mobile views (375x667)

### Use Cases

1. **Documentation**: Generate README screenshots automatically
2. **Testing**: Visual regression testing for UI changes
3. **Quality Assurance**: Verify all features render correctly
4. **Feature Demos**: Consistent screenshots for presentations

### Features

- **Automated**: Single command generates all screenshots
- **Comprehensive**: Captures all major UI states
- **Consistent**: Same test data and viewports every time
- **Fast**: Desktop screenshots in ~30 seconds
- **Integrated**: Works with existing Playwright test infrastructure
- **Metadata**: Optional JSON with screenshot details

### Python API

```python
from pathlib import Path
from scripts.generate_segmentation_screenshots import SegmentationScreenshotGenerator

# Create generator
generator = SegmentationScreenshotGenerator(
    output_dir=Path("screenshots/segmentation"),
    viewer_path=Path("segmentation_viewer.html"),
    test_data_path=Path("test_episodes.json"),
)

# Generate all screenshots
screenshots = generator.generate_all_screenshots(skip_responsive=False)
print(f"Generated {len(screenshots)} screenshots")

# Generate metadata
metadata = generator.generate_metadata()
```

### Adding Custom Screenshots

Edit `scripts/generate_segmentation_screenshots.py`:

```python
# Add new scenario
scenarios.append(
    ScreenshotScenario(
        name="10_custom_feature",
        description="Custom feature screenshot",
        viewport_width=1920,
        viewport_height=1080,
        interact=self._custom_interaction,
        wait_for_selector=".custom-element",
    )
)

# Add interaction helper
def _custom_interaction(self, page):
    self._load_test_data(page)
    page.click(".custom-button")
```

### Testing

```bash
# Run screenshot generation tests
uv run pytest tests/test_segmentation_screenshots.py -v

# Fast tests only (no Playwright required)
uv run pytest tests/test_segmentation_screenshots.py -m "not slow" -v

# Integration tests (requires Playwright)
uv run pytest tests/test_segmentation_screenshots.py -m playwright -v
```

### CI/CD Integration

Screenshots can be generated automatically in GitHub Actions:

```yaml
- name: Generate screenshots
  run: uv run openadapt-viewer screenshots segmentation --save-metadata

- name: Upload artifacts
  uses: actions/upload-artifact@v4
  with:
    name: screenshots
    path: screenshots/segmentation/
```

### Key Files

- `scripts/generate_segmentation_screenshots.py` - Screenshot generation script
- `src/openadapt_viewer/cli.py` - CLI integration
- `tests/test_segmentation_screenshots.py` - Automated tests
- `test_episodes.json` - Test data for consistent screenshots
- `docs/SCREENSHOT_GENERATION.md` - Complete documentation

### Documentation

See [docs/SCREENSHOT_GENERATION.md](docs/SCREENSHOT_GENERATION.md) for:
- Complete usage guide
- All screenshot scenarios
- Troubleshooting
- CI/CD setup
- Visual regression testing
- Performance benchmarks

## Search Functionality

**UPDATED (January 2026)**: All viewers now use advanced token-based search with flexible matching.

### Overview

OpenAdapt viewers implement an intelligent search algorithm that is forgiving and user-friendly:

- **Case-insensitive**: "NightShift" finds "night shift"
- **Token-based**: "nightshift" finds "Disable night shift" (normalizes spaces)
- **Token order independent**: "shift night" finds "night shift"
- **Partial matching**: "nightsh" finds "nightshift"
- **Multi-field**: Searches across name, description, steps, etc.

### Key Problem Solved

The original search was too strict:
```javascript
// OLD: Failed to find "Disable night shift" when searching "nightshift"
text.toLowerCase().includes(query.toLowerCase())
```

The new search tokenizes and normalizes:
```javascript
// NEW: Finds "Disable night shift" when searching "nightshift"
advancedSearch(items, "nightshift", ['name', 'description', 'steps'])
```

### Implementation

**Standalone HTML Viewers:**
Search function is inlined in the HTML (see `segmentation_viewer.html` or `synthetic_demo_viewer.html`):

```javascript
function advancedSearch(items, query, fields = ['name', 'description']) {
    // Tokenize query: "nightshift" -> ["nightshift"]
    const queryTokens = query
        .toLowerCase()
        .replace(/[^a-z0-9\s]/g, ' ')
        .replace(/\s+/g, ' ')
        .trim()
        .split(' ')
        .filter(t => t.length > 0);

    return items.filter(item => {
        // Build searchable text from fields
        const searchText = fields
            .map(field => item[field] || '')
            .join(' ')
            .toLowerCase()
            .replace(/[^a-z0-9\s]/g, ' ')
            .replace(/\s+/g, ' ');

        // All query tokens must match
        return queryTokens.every(queryToken => {
            const searchTokens = searchText.split(' ');
            return searchTokens.some(searchToken =>
                searchToken.includes(queryToken) || queryToken.includes(searchToken)
            );
        });
    });
}
```

**Module-Based (Python/JS):**
Use the reusable `search.js` module:

```javascript
import { searchItems } from './src/openadapt_viewer/search.js';

const results = searchItems(episodes, "nightshift", {
    fields: ['name', 'description', 'steps'],
    rankResults: true,      // Optional: sort by relevance
    fuzzyThreshold: 2       // Optional: allow typos
});
```

### Usage in Viewers

| Viewer | Search Fields | Features |
|--------|---------------|----------|
| Segmentation | name, description, steps | Real-time filtering, combines with recording filter |
| Synthetic Demo | task, domain | Real-time filtering, shows match count, combines with domain filter |

### Testing

Run the interactive test page:
```bash
open /path/to/openadapt-viewer/test_search.html
```

Test cases include:
- "nightshift" → finds "Disable night shift" ✓
- "night shift" → finds "Configure nightshift" ✓
- "shift night" → finds "night shift" (any order) ✓
- "nightsh" → finds "nightshift" (partial) ✓

### Documentation

See [docs/SEARCH_FUNCTIONALITY.md](docs/SEARCH_FUNCTIONALITY.md) for:
- Complete algorithm explanation
- Implementation guide
- Test cases
- Performance considerations
- Comparison with reference implementation (llm-council PR #139)

### Adding Search to New Viewers

1. Copy `advancedSearch` function from existing viewer
2. Update field names to match your data structure
3. Add search input to HTML
4. Connect input handler to filter function
5. Test with various queries

Example:
```html
<input type="text" id="search-input" placeholder="Search..." oninput="handleSearch()">

<script>
function handleSearch() {
    const query = document.getElementById('search-input').value;
    const results = advancedSearch(allItems, query, ['name', 'description']);
    renderResults(results);
}
</script>
```

## Recording Catalog System

**NEW (January 2026)**: Automatic discovery and indexing of recordings and segmentation results.

### Overview

The catalog system eliminates manual file selection by:
- Automatically scanning for recordings in openadapt-capture
- Indexing segmentation results from openadapt-ml
- Providing a SQLite database at `~/.openadapt/catalog.db`
- Generating viewers with automatic recording dropdowns

### Quick Start

```bash
# Scan for recordings and segmentation results
uv run openadapt-viewer catalog scan

# List all recordings
uv run openadapt-viewer catalog list

# Show statistics
uv run openadapt-viewer catalog stats

# Generate segmentation viewer with catalog integration (NEW)
cd /path/to/openadapt-viewer
python scripts/generate_segmentation_viewer.py --output viewer.html --open
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

### Segmentation Viewer Auto-Discovery

**NEW (January 2026)**: The segmentation viewer now supports automatic discovery and selection of episode files.

#### Features

1. **Automatic Discovery**: Scans for all `episode_library.json` and `*_episodes.json` files
2. **Auto-Select Latest**: Automatically selects and loads the most recent episode file
3. **Dropdown Selection**: Shows all available files with metadata (name, date, episode count)
4. **Fallback**: Manual file input still available if catalog is not available

#### How It Works

```
1. Generate viewer with embedded catalog:
   python scripts/generate_segmentation_viewer.py --output viewer.html

2. Catalog scans segmentation_output directories:
   - /path/to/openadapt-ml/segmentation_output/
   - ~/.openadapt/segmentation_output/
   - ./segmentation_output/

3. Finds episode files:
   - episode_library.json (consolidated episodes)
   - *_episodes.json (per-recording episodes)

4. Embeds catalog data in HTML:
   window.SEGMENTATION_CATALOG = {
     files: [
       {file_path, recording_name, created_at, episode_count, ...},
       ...
     ]
   }

5. On page load:
   - Populates dropdown with available files
   - Auto-selects latest file (marked with ★)
   - Auto-loads latest file via fetch()
   - Falls back to manual selection if fetch fails
```

#### UI Components

**Dropdown**:
```
┌─────────────────────────────────────────────────────────────┐
│ Available Episode Files:                                     │
│ ┌─────────────────────────────────────────────────────┐    │
│ │ Test Recording (3 episodes) - 2026-01-17 11:40:47 ★│    │
│ │ Turn Off Nightshift (0 episodes) - 2026-01-17 10:48│    │
│ │ Demo New (0 episodes) - 2026-01-17 10:47:21        │    │
│ └─────────────────────────────────────────────────────┘    │
│ [Load Selected] [Refresh]                                    │
│ Latest file auto-selected. Click "Load Selected" or choose. │
└─────────────────────────────────────────────────────────────┘
```

**Status Messages**:
- Info: "Auto-loading latest: Test Recording (2026-01-17 11:40:47)"
- Success: "Successfully loaded: test_recording_episodes.json"
- Error: "Error loading file: ... Try manual file selection below."

#### Regenerating Catalog

The catalog is embedded at generation time. To pick up new episode files:

```bash
# Option 1: Regenerate viewer
python scripts/generate_segmentation_viewer.py --output viewer.html

# Option 2: Click "Refresh" in viewer (reloads page to pick up changes)
```

#### Key Files

- `src/openadapt_viewer/segmentation_catalog.py` - Catalog discovery and JavaScript generation
- `scripts/generate_segmentation_viewer.py` - CLI for generating viewer with catalog
- `segmentation_viewer.html` - Template with catalog integration
- HTML sections:
  - `<script id="catalog-data">` - Placeholder for embedded catalog
  - `#auto-discovery-section` - Dropdown UI (shown when catalog available)
  - `#manual-selection-section` - Fallback file input (hidden when catalog works)

#### Architecture

```
segmentation_viewer.html (template)
        ↓
generate_segmentation_viewer.py
        ↓ scans
openadapt-ml/segmentation_output/*.json
        ↓ generates
window.SEGMENTATION_CATALOG = {...}
        ↓ embeds into
viewer_with_catalog.html (standalone)
        ↓ opens in browser
Auto-loads latest episode file
```

### Architecture

```
openadapt-capture/ (recordings)
        ↓ scan
~/.openadapt/catalog.db (index)
        ↓ query
Viewers (automatic dropdown)
```

### Key Files

- `src/openadapt_viewer/catalog.py` - Core catalog database and API
- `src/openadapt_viewer/scanner.py` - Recording discovery and indexing
- `src/openadapt_viewer/catalog_api.py` - JavaScript embedding for viewers
- `src/openadapt_viewer/segmentation_catalog.py` - Segmentation file discovery
- `scripts/generate_segmentation_viewer.py` - Viewer generation with catalog
- `CATALOG_SYSTEM.md` - Full documentation

See [CATALOG_SYSTEM.md](CATALOG_SYSTEM.md) for complete documentation.

## Episode Timeline Component

**NEW (January 2026)**: Interactive episode timeline for capture and segmentation viewers with visual navigation and keyboard shortcuts.

### Overview

The Episode Timeline component provides a visual, interactive timeline that displays episode boundaries, labels, and enables quick navigation within recordings. It's designed for recordings that have been segmented into logical episodes by the ML pipeline.

### Features

- **Episode Labels**: Visual labels above timeline showing episode names
- **Episode Boundaries**: Clear vertical dividers between episodes
- **Click Navigation**: Click any episode label to jump to its start
- **Current Episode**: Always shows which episode you're viewing
- **Keyboard Shortcuts**: ←/→ for prev/next episode, 1-9 for direct access
- **Hover Tooltips**: Detailed episode info on hover
- **Auto-Update**: Syncs with playback automatically
- **Mobile Responsive**: Works on tablet and mobile devices

### Quick Integration

```html
<!-- 1. Add CSS and JS -->
<link rel="stylesheet" href="src/openadapt_viewer/styles/episode_timeline.css">
<script src="src/openadapt_viewer/components/episode_timeline.js"></script>

<!-- 2. Add container -->
<div id="episode-timeline-container"></div>

<!-- 3. Initialize in JavaScript -->
<script>
// Load episodes from JSON
const response = await fetch('test_episodes.json');
const data = await response.json();

// Create timeline
const timeline = new EpisodeTimeline({
    container: document.getElementById('episode-timeline-container'),
    episodes: data.episodes,
    currentTime: 0,
    totalDuration: 6.7,
    onSeek: (time) => {
        // Your seek logic
        player.seek(time);
    },
    onEpisodeChange: (episode) => {
        console.log('Now in:', episode.name);
    }
});

// Update on playback
function onTimeUpdate(currentTime) {
    timeline.update({ currentTime });
}
</script>
```

### Episode Data Format

Episodes should follow this JSON structure:

```json
{
  "episodes": [
    {
      "episode_id": "episode_001",
      "name": "Navigate to System Settings",
      "description": "User opens System Settings...",
      "start_time": 0.0,
      "end_time": 3.5,
      "duration": 3.5,
      "steps": ["Click Settings icon", "Wait for window", "Click Displays"],
      "boundary_confidence": 0.92
    }
  ]
}
```

### Keyboard Shortcuts

When the viewer has focus:

- `←` / `→`: Previous/Next episode
- `Home` / `End`: First/Last episode
- `1-9`: Jump to episode by number
- `Space`: Play/Pause (existing behavior)

### Configuration Options

```javascript
const timeline = new EpisodeTimeline({
    // ... required options
    config: {
        showLabels: true,           // Show episode labels
        showBoundaries: true,        // Show boundary markers
        enableClickNavigation: true, // Allow clicking to jump
        enableAutoAdvance: false,    // Don't auto-advance episodes
        labelTruncate: 30,          // Max label length
        colorScheme: 'auto'         // Color palette
    }
});
```

### CSS Classes

All classes use the `oa-` prefix:

| Component | Classes |
|-----------|---------|
| Timeline Container | `oa-episode-timeline` |
| Episode Labels | `oa-episode-label`, `oa-episode-current`, `oa-episode-past`, `oa-episode-future` |
| Timeline Track | `oa-timeline-track`, `oa-episode-segment`, `oa-episode-boundary` |
| Current Marker | `oa-current-marker` |
| Navigation | `oa-episode-controls`, `oa-episode-nav-btn` |
| Tooltip | `oa-episode-tooltip` |

### Episode Colors

The component uses a 5-color rotating palette:

```css
:root {
  --episode-1-bg: linear-gradient(135deg, #3b82f6 0%, #2563eb 100%); /* Blue */
  --episode-2-bg: linear-gradient(135deg, #8b5cf6 0%, #7c3aed 100%); /* Purple */
  --episode-3-bg: linear-gradient(135deg, #ec4899 0%, #db2777 100%); /* Pink */
  --episode-4-bg: linear-gradient(135deg, #f59e0b 0%, #d97706 100%); /* Orange */
  --episode-5-bg: linear-gradient(135deg, #10b981 0%, #059669 100%); /* Green */
}
```

Override these variables to customize colors.

### Integration Examples

**With Alpine.js (Capture Viewer)**:

```javascript
x-data="{
    episodes: [],
    episodeTimeline: null,

    async init() {
        this.episodes = await this.loadEpisodes();
        this.$nextTick(() => this.initializeEpisodeTimeline());
    },

    initializeEpisodeTimeline() {
        this.episodeTimeline = new EpisodeTimeline({
            container: document.getElementById('episode-timeline-container'),
            episodes: this.episodes,
            currentTime: this.getCurrentTime(),
            totalDuration: this.getTotalDuration(),
            onSeek: (time) => this.seekToTime(time)
        });
    }
}"
x-effect="if (episodeTimeline) episodeTimeline.update({ currentTime: getCurrentTime() })"
```

### Testing

```bash
# Run episode timeline tests
uv run pytest tests/test_episode_timeline.py -v

# Run interactive test page
python3 -m http.server 8080
open http://localhost:8080/test_episode_timeline.html
```

### Key Files

- `src/openadapt_viewer/components/episode_timeline.js` - Main component
- `src/openadapt_viewer/styles/episode_timeline.css` - Component styles
- `test_episode_timeline.html` - Interactive test/demo page
- `tests/test_episode_timeline.py` - Automated tests
- `test_episodes.json` - Example episode data

### Documentation

See comprehensive design documentation:
- `EPISODE_TIMELINE_DESIGN.md` - Architecture and visual design
- `EPISODE_TIMELINE_DESIGN_PART2.md` - Implementation details and user flows
- `EPISODE_TIMELINE_QUICKSTART.md` - Quick start integration guide

### Accessibility

The component follows WCAG 2.1 AA standards:

- Keyboard navigation support
- ARIA labels and roles
- Focus indicators
- Screen reader compatible
- High contrast colors

### Mobile Support

Fully responsive with:

- Touch interactions (tap to navigate)
- Swipe gestures (left/right for prev/next)
- Long-press for tooltips
- Adaptive layout (labels stack on small screens)

### Performance

Optimized for:

- 1-20 episodes: Smooth rendering and interaction
- Efficient updates: Only re-renders on state change
- Lazy tooltip rendering: Only shows on hover
- CSS animations: Hardware-accelerated transitions

### Common Issues

**Episodes not showing:**
1. Check episode JSON structure matches expected format
2. Verify `container` element exists in DOM
3. Ensure `totalDuration` matches episode end times
4. Check browser console for errors

**Timeline not updating:**
1. Call `timeline.update({ currentTime })` on each time change
2. Ensure `currentTime` is in seconds (not milliseconds)
3. Verify onSeek callback is provided and working

**Styling looks wrong:**
1. Ensure CSS file is loaded before component initializes
2. Check for CSS conflicts (inspect element)
3. Verify CSS variables are defined (check `:root`)

## Related Projects

- [openadapt-ml](../openadapt-ml/) - ML engine, uses this for dashboards
- [openadapt-evals](../openadapt-evals/) - Benchmark evaluation, uses this for viewers
- [openadapt-capture](../openadapt-capture/) - Recording capture
- [openadapt-retrieval](../openadapt-retrieval/) - Demo retrieval

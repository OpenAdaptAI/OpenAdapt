# Automated Screenshot Generation System

This document describes the automated screenshot generation system for the openadapt-viewer README.

## Overview

The screenshot generation system provides:

1. **Automated HTML Generation**: Converts real captures into interactive HTML viewers
2. **Automated Screenshots**: Uses Playwright to capture screenshots of the viewers
3. **CI Integration**: Automatically generates screenshots on code changes
4. **Regression Testing**: Catches viewer breakage before it reaches production

## Architecture

```
openadapt-viewer/
├── scripts/
│   └── generate_readme_screenshots.py   # Main generation script
├── tests/
│   └── test_screenshot_generation.py    # Comprehensive test suite
├── docs/
│   ├── images/                          # Generated screenshots
│   └── SCREENSHOT_SYSTEM.md            # This document
└── .github/
    └── workflows/
        └── screenshots.yml              # CI workflow
```

## Components

### 1. Generation Script (`scripts/generate_readme_screenshots.py`)

**Purpose**: Generate HTML viewers and take screenshots from real captures.

**Features**:
- Loads captures from openadapt-capture
- Generates interactive HTML using openadapt-viewer
- Takes multiple screenshots per capture (full view, controls, events)
- Handles errors gracefully with clear messages
- Provides progress feedback

**Usage**:
```bash
# Basic usage
uv run python scripts/generate_readme_screenshots.py

# Custom options
uv run python scripts/generate_readme_screenshots.py \
  --capture-dir /path/to/captures \
  --max-events 50 \
  --output-dir docs/images
```

**Error Handling**:
- Missing dependencies → Installation instructions
- Missing captures → Clear path information
- HTML generation fails → Capture details and error
- Screenshot fails → Browser and rendering details

### 2. Test Suite (`tests/test_screenshot_generation.py`)

**Purpose**: Verify the screenshot generation system works correctly.

**Test Coverage**:

| Test | Description | Speed |
|------|-------------|-------|
| `test_script_exists` | Script file exists | Fast |
| `test_script_has_shebang` | Proper shebang line | Fast |
| `test_script_imports` | Can import without errors | Fast |
| `test_dependency_check` | Dependency checking works | Fast |
| `test_captures_exist` | Required captures available | Fast |
| `test_help_message` | Help output correct | Fast |
| `test_html_generation_only` | HTML generation works | Slow |
| `test_full_screenshot_generation` | Full Playwright screenshots | Slow, Playwright |
| `test_error_handling_invalid_capture` | Graceful error handling | Fast |
| `test_command_line_options` | CLI options work | Medium |

**Running Tests**:
```bash
# All tests
uv run pytest tests/test_screenshot_generation.py -v

# Fast tests only (no screenshot generation)
uv run pytest tests/test_screenshot_generation.py -v -m "not slow"

# Only Playwright integration tests
uv run pytest tests/test_screenshot_generation.py -v -m playwright

# Skip Playwright tests
uv run pytest tests/test_screenshot_generation.py -v -m "not playwright"
```

### 3. CI Workflow (`.github/workflows/screenshots.yml`)

**Purpose**: Automatically generate screenshots on code changes.

**Triggers**:
- Push to main branch (with screenshot-related changes)
- Pull requests (with screenshot-related changes)
- Manual workflow dispatch

**Steps**:
1. Checkout openadapt-viewer and openadapt-capture repos
2. Install dependencies (Python, uv, packages)
3. Install Playwright browsers
4. Check if captures exist
5. Generate screenshots
6. Upload as artifacts
7. (Optional) Create PR with updated screenshots
8. (Optional) Comment on PR with preview

**Artifacts**:
- `readme-screenshots`: All generated PNG files
- Retention: 30 days

**Features**:
- Runs on macOS for consistent rendering
- Continues on capture not found (with warnings)
- Provides detailed summary in GitHub Actions
- Can automatically create PRs with updated screenshots

## Captures Used

### turn-off-nightshift
- **Source**: `/path/to/openadapt-capture/turn-off-nightshift`
- **Screenshots**: 22 frames
- **Description**: Demonstrates turning off Night Shift in macOS System Settings
- **Use case**: Shows complex UI navigation workflow

**Generated Screenshots**:
- `turn-off-nightshift_full.png`: Complete viewer interface
- `turn-off-nightshift_controls.png`: Playback controls focused view
- `turn-off-nightshift_events.png`: Event list sidebar

### demo_new
- **Source**: `/path/to/openadapt-capture/demo_new`
- **Screenshots**: 14 frames
- **Description**: Demo workflow example
- **Use case**: Shorter capture for quick examples

**Generated Screenshots**:
- `demo_new_full.png`: Complete viewer interface
- `demo_new_controls.png`: Playback controls focused view
- `demo_new_events.png`: Event list sidebar

## Screenshot Scenarios

Each capture generates 3 screenshots with different views:

### 1. Full Viewer (`*_full.png`)
- **Viewport**: 1400x900
- **Purpose**: Show complete interface
- **Content**: Screenshot display, controls, timeline, events, details
- **Use in README**: Main demonstration of viewer capabilities

### 2. Controls Focus (`*_controls.png`)
- **Viewport**: 1400x600
- **Purpose**: Highlight playback controls and timeline
- **Content**: Screenshot display, playback buttons, timeline, overlay toggle
- **Use in README**: Demonstrate playback features

### 3. Events Sidebar (`*_events.png`)
- **Viewport**: 800x900
- **Purpose**: Show event list and details panel
- **Content**: Event list, selected event details, action types
- **Use in README**: Demonstrate event browsing

## Dependencies

### Required
- **openadapt-capture**: Provides captures and HTML generation
- **Python 3.10+**: Runtime environment

### Optional
- **playwright**: For screenshot generation
- **chromium browser**: Playwright browser backend

### Installation

```bash
# Install openadapt-capture (required)
cd ../openadapt-capture
uv pip install -e .

# Install openadapt-viewer with screenshot support (optional)
cd ../openadapt-viewer
uv pip install -e ".[screenshots]"

# Install Playwright browsers (optional)
uv run playwright install chromium
```

## Workflow

### Local Development

1. **Make changes** to viewer components
2. **Test locally**:
   ```bash
   # Quick test - just HTML
   uv run python scripts/generate_readme_screenshots.py --skip-screenshots

   # Full test - HTML + screenshots
   uv run python scripts/generate_readme_screenshots.py
   ```
3. **Review screenshots** in `docs/images/`
4. **Run tests**:
   ```bash
   uv run pytest tests/test_screenshot_generation.py -v
   ```
5. **Commit** changes including updated screenshots

### CI Process

1. **Push or PR** triggers workflow
2. **CI checks** for captures in openadapt-capture
3. **Generates** HTML and screenshots
4. **Uploads** artifacts
5. **Comments** on PR with preview (if PR)
6. **Creates PR** with updated screenshots (if main push and changes detected)

### Manual Regeneration

```bash
# Trigger workflow manually
# Go to: Actions → Generate Screenshots → Run workflow

# Or locally:
uv run python scripts/generate_readme_screenshots.py \
  --capture-dir ../openadapt-capture \
  --output-dir docs/images \
  --max-events 50
```

## Error Handling

The system is designed to fail gracefully with clear error messages:

### Dependency Errors
```
Error: openadapt_capture is required but not installed
Install with: cd ../openadapt-capture && uv pip install -e .
```

### Capture Errors
```
Error: Capture not found: /path/to/capture
FileNotFoundError: [Errno 2] No such file or directory: '/path/to/capture/recording.db'
```

### HTML Generation Errors
```
Error: Failed to generate HTML: [detailed error]
  - Capture ID: [id]
  - Duration: [duration]s
  - Platform: [platform]
```

### Screenshot Errors
```
Error: Playwright not installed: No module named 'playwright'
Install with: uv pip install playwright && uv run playwright install chromium
```

## Troubleshooting

### Screenshots not generating

**Check dependencies**:
```bash
uv run python scripts/generate_readme_screenshots.py --check-deps
```

**Expected output**:
```
Dependency Status:
  openadapt_capture: ✓ Available
  playwright: ✓ Available
```

### HTML files created but screenshots fail

**Install Playwright browsers**:
```bash
uv run playwright install chromium
```

### Captures not found in CI

**Check the workflow**:
- Ensure openadapt-capture checkout step succeeds
- Verify captures are committed to the repo
- Check capture paths in script match repo structure

### Screenshots look wrong

**Adjust viewport size**:
Edit `scripts/generate_readme_screenshots.py` and modify:
```python
scenarios = [
    {
        "suffix": "_full",
        "viewport_width": 1600,  # Increase for larger screenshots
        "viewport_height": 1000,
        ...
    },
]
```

## Performance

**HTML Generation**:
- ~5-10 seconds per capture
- Depends on capture size and max_events

**Screenshot Generation**:
- ~2-3 seconds per screenshot
- Includes browser launch, page load, and rendering
- Total: ~18 seconds for 6 screenshots (2 captures × 3 scenarios)

**CI Runtime**:
- Full workflow: ~2-3 minutes
- Includes checkout, dependency install, browser install, generation, upload

## Future Enhancements

Potential improvements:

1. **Animated GIFs**: Generate short animated demos
2. **Video Clips**: Create MP4 clips of playback
3. **Comparison Screenshots**: Side-by-side before/after for PRs
4. **Thumbnail Generation**: Smaller preview images
5. **Mobile Viewport**: Additional screenshots for responsive design
6. **Dark Mode**: Screenshots with dark theme
7. **Interactive Demos**: Embedded viewers in GitHub Pages
8. **Performance Metrics**: Track viewer load times

## Maintenance

### Adding New Captures

1. Record capture in openadapt-capture
2. Add to `captures` list in script:
   ```python
   captures = [
       {
           "path": args.capture_dir / "new-capture",
           "name": "new-capture",
           "description": "Description",
       },
   ]
   ```
3. Update README with new screenshots
4. Commit and push

### Modifying Screenshot Scenarios

Edit the `scenarios` list in `take_multiple_screenshots()` calls:
```python
scenarios = [
    {
        "suffix": "_custom",
        "description": "Custom view",
        "viewport_width": 1200,
        "viewport_height": 800,
        "full_page": False,
        "interact": lambda page: page.click("#some-button"),
    },
]
```

### Updating CI Workflow

1. Edit `.github/workflows/screenshots.yml`
2. Test locally with act: `act -j generate-screenshots`
3. Push and verify in GitHub Actions

## References

- [Playwright Documentation](https://playwright.dev/python/)
- [GitHub Actions Artifacts](https://docs.github.com/en/actions/using-workflows/storing-workflow-data-as-artifacts)
- [openadapt-capture](https://github.com/OpenAdaptAI/openadapt-capture)
- [openadapt-viewer](https://github.com/OpenAdaptAI/openadapt-viewer)

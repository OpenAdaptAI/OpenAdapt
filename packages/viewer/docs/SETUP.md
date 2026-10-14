# Screenshot Generation Setup Guide

Quick setup guide for generating README screenshots.

## Prerequisites

1. **openadapt-capture** repository with captures
2. **openadapt-viewer** repository (this repo)
3. Python 3.10 or higher
4. uv package manager

## Installation

### 1. Install openadapt-capture

```bash
cd /path/to/openadapt-capture
uv pip install -e .
```

### 2. Install openadapt-viewer with screenshots support

```bash
cd /path/to/openadapt-viewer
uv pip install -e ".[screenshots]"
```

### 3. Install Playwright browsers (one-time)

```bash
uv run playwright install chromium
```

This downloads the Chromium browser (~150MB). You only need to do this once.

## Verification

### Check dependencies

```bash
uv run python scripts/generate_readme_screenshots.py --check-deps
```

**Expected output**:
```
Dependency Status:
  openadapt_capture: ✓ Available
  playwright: ✓ Available
```

### Run basic tests

```bash
# Fast tests only (no screenshots)
uv run pytest tests/test_screenshot_generation.py -v -m "not slow"
```

## Generate Screenshots

### Full generation (HTML + screenshots)

```bash
uv run python scripts/generate_readme_screenshots.py
```

**Expected output**:
```
================================================================================
STEP 1: Generate HTML Viewers
================================================================================
Loading capture from: /path/to/openadapt-capture/turn-off-nightshift
  - Capture ID: ...
  - Duration: ...s
  - Platform: darwin
  - Total actions: ...
Generating HTML viewer: ...
  - Generated: ... (... MB)

[Similar for demo_new]

================================================================================
STEP 2: Take Screenshots
================================================================================
Taking 3 screenshots from: turn-off-nightshift_viewer.html
  [1/3] Full viewer interface
      Viewport: 1400x900, Full page: False
      Saved: turn-off-nightshift_full.png (... KB)
  [2/3] Playback controls and timeline
      Viewport: 1400x600, Full page: False
      Saved: turn-off-nightshift_controls.png (... KB)
  [3/3] Event list and details panel
      Viewport: 800x900, Full page: False
      Saved: turn-off-nightshift_events.png (... KB)

[Similar for demo_new]

================================================================================
SUMMARY
================================================================================
Generated HTML files: 2
  - .../turn-off-nightshift_viewer.html
  - .../demo_new_viewer.html

Generated screenshots: 6
  - .../turn-off-nightshift_full.png
  - .../turn-off-nightshift_controls.png
  - .../turn-off-nightshift_events.png
  - .../demo_new_full.png
  - .../demo_new_controls.png
  - .../demo_new_events.png

✓ Screenshot generation completed successfully!
```

### HTML only (faster, for testing)

```bash
uv run python scripts/generate_readme_screenshots.py --skip-screenshots
```

### Screenshots only (reuse existing HTML)

```bash
uv run python scripts/generate_readme_screenshots.py --skip-html
```

## Output Location

**Temporary HTML files**: `temp/`
- `turn-off-nightshift_viewer.html`
- `demo_new_viewer.html`

**Final screenshots**: `docs/images/`
- `turn-off-nightshift_full.png`
- `turn-off-nightshift_controls.png`
- `turn-off-nightshift_events.png`
- `demo_new_full.png`
- `demo_new_controls.png`
- `demo_new_events.png`

## Troubleshooting

### "openadapt_capture not available"

**Solution**: Install openadapt-capture
```bash
cd ../openadapt-capture
uv pip install -e .
```

### "playwright not available"

**Solution**: Install playwright and browsers
```bash
uv pip install "openadapt-viewer[screenshots]"
uv run playwright install chromium
```

### "Capture not found"

**Solution**: Verify capture paths
```bash
# Check captures exist
ls -la /path/to/openadapt-capture/examples/captures/turn-off-nightshift/
ls -la /path/to/openadapt-capture/examples/captures/demo_new/

# Look for recording.db
ls -la /path/to/openadapt-capture/examples/captures/turn-off-nightshift/recording.db
```

**Or use custom path**:
```bash
uv run python scripts/generate_readme_screenshots.py \
  --capture-dir /path/to/your/captures
```

### Screenshot generation is slow

**Reduce events**:
```bash
uv run python scripts/generate_readme_screenshots.py --max-events 20
```

This limits the number of events included in the viewer, making HTML smaller and faster to render.

### Test failures

**Run with verbose output**:
```bash
uv run pytest tests/test_screenshot_generation.py -v -s
```

**Run specific test**:
```bash
uv run pytest tests/test_screenshot_generation.py::test_dependency_check -v
```

**Skip slow tests**:
```bash
uv run pytest tests/test_screenshot_generation.py -v -m "not slow"
```

## CI Integration

The screenshot generation runs automatically in CI via `.github/workflows/screenshots.yml`.

**Triggers**:
- Push to main (with related file changes)
- Pull requests (with related file changes)
- Manual workflow dispatch

**Artifacts**:
- Screenshots are uploaded as workflow artifacts
- Retention: 30 days

**Manual trigger**:
1. Go to Actions tab
2. Select "Generate Screenshots" workflow
3. Click "Run workflow"

## Next Steps

After generating screenshots:

1. **Review** screenshots in `docs/images/`
2. **Verify** they look correct by opening in image viewer
3. **Commit** changes:
   ```bash
   git add docs/images/*.png
   git commit -m "docs: update README screenshots"
   ```
4. **Push** to GitHub

The screenshots will appear in the README automatically.

## Resources

- [Screenshot System Documentation](SCREENSHOT_SYSTEM.md)
- [Script README](../scripts/README.md)
- [Test Suite](../tests/test_screenshot_generation.py)
- [CI Workflow](../.github/workflows/screenshots.yml)

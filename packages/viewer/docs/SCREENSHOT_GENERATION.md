# Screenshot Generation System

This document describes the automated screenshot generation system for OpenAdapt viewers, with a focus on the segmentation viewer.

## Overview

The screenshot generation system automatically captures UI states of viewers for:
- Documentation (README, guides)
- Testing (visual regression)
- Quality assurance
- Feature demos

### Key Features

- **Automated**: Run a single command to generate all screenshots
- **Comprehensive**: Captures all major UI states and features
- **Responsive**: Desktop, tablet, and mobile viewports
- **Consistent**: Same test data and viewport sizes every time
- **Fast**: Desktop-only screenshots in ~30 seconds
- **Integrated**: Works with existing Playwright test infrastructure

## Quick Start

### Installation

```bash
# Install dependencies
cd /path/to/openadapt-viewer
uv sync

# Install Playwright browsers (one-time setup)
uv pip install playwright
uv run playwright install chromium
```

### Generate Screenshots

```bash
# Generate all screenshots (desktop + responsive)
uv run python scripts/generate_segmentation_screenshots.py

# Desktop only (faster)
uv run python scripts/generate_segmentation_screenshots.py --skip-responsive

# Custom output directory
uv run python scripts/generate_segmentation_screenshots.py --output docs/images/

# Via CLI
uv run openadapt-viewer screenshots segmentation --output screenshots/
```

### View Results

Screenshots are saved to `screenshots/segmentation/` by default:

```
screenshots/segmentation/
├── 01_initial_empty.png
├── 02_episodes_loaded.png
├── 03_episode_thumbnails.png
├── 04_episode_details_expanded.png
├── 05_key_frames_gallery.png
├── 06_search_empty.png
├── 07_search_filtered.png
├── 08_recording_filter.png
├── 09_full_page.png
├── 10_tablet_list.png
├── 11_tablet_details.png
├── 12_mobile_list.png
└── 13_mobile_details.png
```

## Architecture

### Components

1. **Screenshot Generator Script** (`scripts/generate_segmentation_screenshots.py`)
   - Core logic for generating screenshots
   - Playwright automation
   - Scenario definitions
   - Metadata generation

2. **CLI Integration** (`src/openadapt_viewer/cli.py`)
   - `screenshots segmentation` command
   - Argument parsing and validation
   - Script execution wrapper

3. **Test Suite** (`tests/test_segmentation_screenshots.py`)
   - Automated tests for screenshot generation
   - Validation of output
   - CI/CD integration

4. **Test Data** (`test_episodes.json`)
   - Sample segmentation results
   - Consistent data for reproducible screenshots

### Screenshot Scenarios

Each screenshot captures a specific UI state:

| Screenshot | Description | Viewport | Interaction |
|------------|-------------|----------|-------------|
| `01_initial_empty` | Empty viewer before loading data | 1920x1080 | None |
| `02_episodes_loaded` | Episode list with all episodes | 1920x1080 | Load test data |
| `03_episode_thumbnails` | Episode cards with thumbnails | 1920x1080 | Load test data |
| `04_episode_details_expanded` | First episode expanded | 1920x1080 | Click first episode |
| `05_key_frames_gallery` | Key frames gallery view | 1920x1080 | Expand episode |
| `06_search_empty` | Search input focused | 1920x1080 | Focus search |
| `07_search_filtered` | Search results for "nightshift" | 1920x1080 | Type search query |
| `08_recording_filter` | Recording filter dropdown | 1920x1080 | Click filter |
| `09_full_page` | Full page scroll capture | 1920x1080 | Full page mode |
| `10_tablet_list` | Tablet - Episode list | 768x1024 | Load test data |
| `11_tablet_details` | Tablet - Episode details | 768x1024 | Expand episode |
| `12_mobile_list` | Mobile - Episode list | 375x667 | Load test data |
| `13_mobile_details` | Mobile - Episode details | 375x667 | Expand episode |

## Usage

### Command-Line Interface

#### Basic Usage

```bash
# Generate all screenshots
uv run openadapt-viewer screenshots segmentation

# Custom output directory
uv run openadapt-viewer screenshots segmentation --output screenshots/

# Skip responsive viewports (faster)
uv run openadapt-viewer screenshots segmentation --skip-responsive

# Save metadata JSON
uv run openadapt-viewer screenshots segmentation --save-metadata
```

#### Advanced Options

```bash
# Custom viewer HTML
uv run openadapt-viewer screenshots segmentation \
    --viewer custom_viewer.html \
    --output screenshots/

# Custom test data
uv run openadapt-viewer screenshots segmentation \
    --test-data custom_episodes.json \
    --output screenshots/

# Full configuration
uv run openadapt-viewer screenshots segmentation \
    --viewer segmentation_viewer.html \
    --test-data test_episodes.json \
    --output docs/images/ \
    --save-metadata \
    --skip-responsive
```

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

# Generate metadata
metadata = generator.generate_metadata()
print(f"Generated {metadata['screenshot_count']} screenshots")
```

### Adding New Scenarios

To add a new screenshot scenario, edit `generate_segmentation_screenshots.py`:

```python
# In _generate_desktop_screenshots method
scenarios.append(
    ScreenshotScenario(
        name="10_new_feature",
        description="Description of new feature",
        viewport_width=1920,
        viewport_height=1080,
        interact=self._custom_interaction,
        wait_for_selector=".new-feature",
        wait_timeout=1000,
    )
)

# Add interaction method
def _custom_interaction(self, page):
    """Custom interaction for new feature."""
    self._load_test_data(page)
    page.wait_for_timeout(500)
    page.click(".new-feature-button")
```

## Testing

### Run Tests

```bash
# Run all screenshot tests
uv run pytest tests/test_segmentation_screenshots.py -v

# Run fast tests only (desktop, skip responsive)
uv run pytest tests/test_segmentation_screenshots.py -m "not slow" -v

# Run with Playwright markers
uv run pytest tests/test_segmentation_screenshots.py -m playwright -v
```

### Test Categories

1. **Basic Tests** (fast, no Playwright required)
   - Script exists
   - Script imports
   - Help message
   - Dependency check

2. **Integration Tests** (slow, requires Playwright)
   - Desktop screenshot generation
   - Responsive screenshot generation
   - Metadata generation
   - CLI integration

3. **Error Handling Tests**
   - Missing viewer
   - Missing test data
   - Invalid paths

### Test Markers

```python
@pytest.mark.slow  # Long-running tests
@pytest.mark.playwright  # Requires Playwright and browsers
```

## CI/CD Integration

### GitHub Actions

Create `.github/workflows/screenshots.yml`:

```yaml
name: Generate Screenshots

on:
  push:
    paths:
      - 'segmentation_viewer.html'
      - 'scripts/generate_segmentation_screenshots.py'
      - 'test_episodes.json'
  workflow_dispatch:

jobs:
  screenshots:
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install uv
        run: curl -LsSf https://astral.sh/uv/install.sh | sh

      - name: Install dependencies
        run: |
          cd /path/to/openadapt-viewer
          uv sync
          uv pip install playwright
          uv run playwright install chromium --with-deps

      - name: Generate screenshots
        run: |
          cd /path/to/openadapt-viewer
          uv run python scripts/generate_segmentation_screenshots.py \
            --output screenshots/segmentation \
            --save-metadata

      - name: Upload screenshots
        uses: actions/upload-artifact@v4
        with:
          name: segmentation-screenshots
          path: screenshots/segmentation/
          retention-days: 30

      - name: Upload to docs (optional)
        if: github.ref == 'refs/heads/main'
        run: |
          # Copy screenshots to docs directory
          mkdir -p docs/images
          cp screenshots/segmentation/*.png docs/images/
          # Commit and push if changes
```

### Visual Regression Testing

Use screenshots for visual regression testing:

```bash
# Generate baseline screenshots
uv run pytest tests/test_segmentation_screenshots.py --snapshot-update

# Run visual diff tests
uv run pytest tests/test_segmentation_screenshots.py
```

## Screenshot Metadata

When using `--save-metadata`, a `metadata.json` file is generated:

```json
{
  "generated_at": "2026-01-17T10:30:00.000000",
  "viewer_path": "/path/to/segmentation_viewer.html",
  "test_data_path": "/path/to/test_episodes.json",
  "output_dir": "/path/to/screenshots/segmentation",
  "screenshot_count": 13,
  "screenshots": [
    {
      "path": "/path/to/screenshots/segmentation/01_initial_empty.png",
      "filename": "01_initial_empty.png",
      "size_bytes": 45632
    }
  ]
}
```

## Troubleshooting

### Playwright Not Installed

```
Error: Playwright not installed
```

**Solution:**
```bash
uv pip install playwright
uv run playwright install chromium
```

### Chromium Binary Not Found

```
Error: Executable doesn't exist at ...
```

**Solution:**
```bash
uv run playwright install chromium --with-deps
```

### Screenshots Are Blank

**Possible causes:**
1. Viewer HTML not loading correctly
2. Test data not loading
3. Selectors not matching

**Solution:**
- Check viewer HTML path
- Verify test data JSON is valid
- Inspect browser console logs

### Timeout Errors

```
Error: Timeout 5000ms exceeded
```

**Solution:**
- Increase `wait_timeout` in scenario
- Check if selector exists in HTML
- Verify interaction logic

### Permission Denied

```
Error: Permission denied: /path/to/screenshots/
```

**Solution:**
```bash
mkdir -p screenshots/segmentation
chmod 755 screenshots/segmentation
```

## Performance

### Benchmarks

| Configuration | Time | Screenshots |
|--------------|------|-------------|
| Desktop only | ~30s | 9 screenshots |
| Desktop + Responsive | ~60s | 13 screenshots |
| With metadata | +2s | +1 JSON file |

### Optimization Tips

1. **Skip Responsive**: Use `--skip-responsive` for faster generation
2. **Parallel Generation**: Run multiple instances for different viewports
3. **Cache Test Data**: Keep test data small and focused
4. **Use SSD**: Faster disk I/O improves screenshot save time

## Best Practices

### Screenshot Quality

1. **Consistent Resolution**: Always use same viewport sizes
2. **PNG Format**: Use lossless PNG for documentation
3. **Appropriate Size**: Balance quality vs. file size
4. **Descriptive Names**: Use clear, numbered filenames

### Test Data

1. **Representative**: Use realistic, production-like data
2. **Stable**: Don't change test data unnecessarily
3. **Minimal**: Include only what's needed for screenshots
4. **Version Control**: Commit test data to git

### Documentation

1. **Update README**: Include screenshots in README.md
2. **Add Captions**: Describe what each screenshot shows
3. **Group by Feature**: Organize screenshots logically
4. **Keep Current**: Regenerate when UI changes

### Maintenance

1. **Regular Updates**: Regenerate screenshots after UI changes
2. **Review Changes**: Check diffs before committing
3. **Automate**: Use CI/CD to catch missing updates
4. **Clean Old**: Remove outdated screenshots

## Integration with Other Viewers

The screenshot system can be extended to other viewers:

### Benchmark Viewer

```python
# Create benchmark screenshot generator
generator = BenchmarkScreenshotGenerator(
    output_dir=Path("screenshots/benchmark"),
    viewer_path=Path("benchmark_viewer.html"),
    test_data_path=Path("test_benchmark.json"),
)
```

### Capture Viewer

```python
# Create capture screenshot generator
generator = CaptureScreenshotGenerator(
    output_dir=Path("screenshots/capture"),
    viewer_path=Path("capture_viewer.html"),
    test_data_path=Path("test_capture.json"),
)
```

## FAQ

### Q: How often should I regenerate screenshots?

**A:** Regenerate whenever:
- UI changes are made
- New features are added
- Before major releases
- When test data changes

### Q: Should I commit screenshots to git?

**A:** Yes for documentation, no for CI artifacts:
- **Commit**: README screenshots, feature demos
- **Don't commit**: CI-generated screenshots, large files

### Q: Can I use screenshots in presentations?

**A:** Yes! Screenshots are high-quality and consistent:
- Use PNG format for best quality
- Desktop viewport (1920x1080) for slides
- Mobile viewport (375x667) for mobile demos

### Q: How do I debug screenshot generation?

**A:** Use headed mode and slow-mo:

```python
browser = p.chromium.launch(headless=False, slow_mo=1000)
```

### Q: Can I generate screenshots in parallel?

**A:** Yes, but be careful with resource usage:

```bash
# Generate desktop and responsive in parallel
uv run python scripts/generate_segmentation_screenshots.py --skip-responsive &
uv run python scripts/generate_segmentation_screenshots.py --output screenshots/responsive/
```

## See Also

- [TESTING_STRATEGY.md](TESTING_STRATEGY.md) - Overall testing strategy
- [TESTING_GUIDE.md](TESTING_GUIDE.md) - Writing and running tests
- [Playwright Documentation](https://playwright.dev/python/) - Playwright Python API
- [pytest Documentation](https://docs.pytest.org/) - pytest testing framework

## Changelog

### 2026-01-17 - Initial Implementation

- Created screenshot generation system
- Added CLI integration
- Implemented comprehensive test suite
- Added documentation
- Support for desktop and responsive viewports
- Metadata generation

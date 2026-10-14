# Scripts

## generate_readme_screenshots.py

Automated screenshot generation for the README documentation.

### Purpose

This script generates visual proof that the openadapt-viewer works correctly by:

1. Loading real captures from openadapt-capture
2. Generating interactive HTML viewers
3. Taking screenshots using Playwright
4. Saving screenshots to docs/images/

### Usage

#### Basic Usage

```bash
# Generate all screenshots with defaults
uv run python scripts/generate_readme_screenshots.py
```

#### Custom Options

```bash
# Use custom capture directory
uv run python scripts/generate_readme_screenshots.py \
  --capture-dir /path/to/captures \
  --output-dir docs/images \
  --max-events 50

# Generate only HTML (skip screenshots)
uv run python scripts/generate_readme_screenshots.py --skip-screenshots

# Use existing HTML (only generate screenshots)
uv run python scripts/generate_readme_screenshots.py --skip-html

# Check dependencies
uv run python scripts/generate_readme_screenshots.py --check-deps
```

### Requirements

**Required:**
- openadapt-capture installed and available
- Capture directories with valid capture.db files

**For Screenshots:**
- playwright (`uv pip install "openadapt-viewer[screenshots]"`)
- Chromium browser (`uv run playwright install chromium`)

### Captures Used

The script expects these captures in the openadapt-capture directory:

1. **turn-off-nightshift** (22 screenshots)
   - Demonstrates turning off Night Shift in macOS System Settings
   - Shows complex UI navigation workflow

2. **demo_new** (14 screenshots)
   - Demo workflow example
   - Shorter capture for quick testing

### Output

The script generates:

**HTML Files (in temp/):**
- `turn-off-nightshift_viewer.html`
- `demo_new_viewer.html`

**Screenshots (in docs/images/):**
- `turn-off-nightshift_full.png` - Full viewer interface
- `turn-off-nightshift_controls.png` - Playback controls and timeline
- `turn-off-nightshift_events.png` - Event list and details
- `demo_new_full.png` - Demo workflow viewer
- `demo_new_controls.png` - Demo playback controls
- `demo_new_events.png` - Demo event list

### Error Handling

The script handles common failures gracefully:

- **Missing captures**: Clear error message with path
- **openadapt-capture not installed**: Installation instructions
- **Playwright not available**: Installation instructions
- **HTML generation fails**: Detailed error with capture info
- **Screenshot fails**: Error with HTML path and browser details

All errors include actionable fix instructions.

### Testing

The script has comprehensive tests in `tests/test_screenshot_generation.py`:

```bash
# Run all tests
uv run pytest tests/test_screenshot_generation.py -v

# Run only fast tests (no screenshot generation)
uv run pytest tests/test_screenshot_generation.py -v -m "not slow"

# Run full integration test with Playwright
uv run pytest tests/test_screenshot_generation.py -v -m playwright
```

### CI Integration

The script is integrated into GitHub Actions via `.github/workflows/screenshots.yml`:

- Runs on push to main or PR changes
- Checks for capture availability
- Generates screenshots automatically
- Uploads as artifacts
- Can create PRs with updated screenshots

### Troubleshooting

**"openadapt_capture not available"**
```bash
cd ../openadapt-capture
uv pip install -e .
```

**"playwright not available"**
```bash
uv pip install "openadapt-viewer[screenshots]"
uv run playwright install chromium
```

**"Capture not found"**
- Verify capture directory path
- Check that capture.db exists in the directory
- Ensure captures were recorded properly

**Screenshots look wrong**
- Check viewport size (default 1400x900)
- Verify HTML renders correctly in browser
- Try adjusting `--max-events` to include more/fewer events

### Development

When modifying the script:

1. Test with `--check-deps` first
2. Use `--skip-screenshots` for faster iteration
3. Run tests: `uv run pytest tests/test_screenshot_generation.py -v`
4. Update this README if adding new features

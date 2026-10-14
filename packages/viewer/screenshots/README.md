# Screenshots

This directory contains automatically generated screenshots of OpenAdapt viewers for documentation and testing.

## Structure

```
screenshots/
├── segmentation/          # Segmentation viewer screenshots
│   ├── 01_initial_empty.png
│   ├── 02_episodes_loaded.png
│   ├── ...
│   └── metadata.json      # Optional metadata
│
├── benchmark/             # Benchmark viewer screenshots (future)
└── capture/               # Capture viewer screenshots (future)
```

## Generating Screenshots

### Segmentation Viewer

```bash
# Generate all screenshots (desktop + responsive)
uv run openadapt-viewer screenshots segmentation

# Desktop only (faster)
uv run openadapt-viewer screenshots segmentation --skip-responsive

# Custom output
uv run openadapt-viewer screenshots segmentation --output custom/path/

# With metadata
uv run openadapt-viewer screenshots segmentation --save-metadata
```

### Direct Script Usage

```bash
# Use the script directly for more control
uv run python scripts/generate_segmentation_screenshots.py --help

# Generate with custom settings
uv run python scripts/generate_segmentation_screenshots.py \
    --viewer segmentation_viewer.html \
    --test-data test_episodes.json \
    --output screenshots/segmentation \
    --save-metadata
```

## Screenshot List

### Segmentation Viewer

| Screenshot | Description | Viewport | Size |
|------------|-------------|----------|------|
| `01_initial_empty.png` | Empty viewer before loading | 1920x1080 | ~45 KB |
| `02_episodes_loaded.png` | Episode list with all episodes | 1920x1080 | ~180 KB |
| `03_episode_thumbnails.png` | Episode cards with thumbnails | 1920x1080 | ~190 KB |
| `04_episode_details_expanded.png` | First episode expanded | 1920x1080 | ~200 KB |
| `05_key_frames_gallery.png` | Key frames gallery view | 1920x1080 | ~210 KB |
| `06_search_empty.png` | Search input focused | 1920x1080 | ~185 KB |
| `07_search_filtered.png` | Search results for "nightshift" | 1920x1080 | ~120 KB |
| `08_recording_filter.png` | Recording filter dropdown | 1920x1080 | ~185 KB |
| `09_full_page.png` | Full page scroll capture | 1920x1080 | ~300 KB |
| `10_tablet_list.png` | Tablet - Episode list | 768x1024 | ~120 KB |
| `11_tablet_details.png` | Tablet - Episode details | 768x1024 | ~130 KB |
| `12_mobile_list.png` | Mobile - Episode list | 375x667 | ~60 KB |
| `13_mobile_details.png` | Mobile - Episode details | 375x667 | ~70 KB |

## Requirements

- Python 3.10+
- Playwright (`uv pip install playwright`)
- Chromium browser (`uv run playwright install chromium`)

## Documentation

See [../docs/SCREENSHOT_GENERATION.md](../docs/SCREENSHOT_GENERATION.md) for complete documentation including:
- Detailed usage guide
- All screenshot scenarios
- Adding custom screenshots
- CI/CD integration
- Troubleshooting
- Performance benchmarks

## Testing

Screenshots are automatically tested in the test suite:

```bash
# Run screenshot generation tests
uv run pytest tests/test_segmentation_screenshots.py -v

# Fast tests only (no Playwright)
uv run pytest tests/test_segmentation_screenshots.py -m "not slow" -v
```

## CI/CD

Screenshots can be generated automatically in CI:

```yaml
- name: Generate screenshots
  run: uv run openadapt-viewer screenshots segmentation --save-metadata

- name: Upload artifacts
  uses: actions/upload-artifact@v4
  with:
    name: screenshots
    path: screenshots/
```

## Notes

- Screenshots use consistent test data from `test_episodes.json`
- Desktop viewport: 1920x1080
- Tablet viewport: 768x1024
- Mobile viewport: 375x667
- Format: PNG (lossless)
- Generation time: ~30s (desktop only), ~60s (all viewports)

## Troubleshooting

### Playwright not installed

```bash
uv pip install playwright
uv run playwright install chromium
```

### Permission denied

```bash
mkdir -p screenshots/segmentation
chmod 755 screenshots/segmentation
```

### Screenshots are blank

Check that:
1. Viewer HTML exists and is valid
2. Test data JSON is valid
3. Playwright browser is installed

See [../docs/SCREENSHOT_GENERATION.md](../docs/SCREENSHOT_GENERATION.md) for more troubleshooting.

# Documentation Scripts

This directory contains automation scripts for maintaining OpenAdapt documentation.

## Screenshot Generation

### `generate_docs_screenshots.py`

Generates professional screenshots for documentation using Playwright automation.

**Installation:**
```bash
pip install playwright
playwright install chromium
```

**Usage:**
```bash
# Generate all screenshots
python docs/_scripts/generate_docs_screenshots.py

# Generate specific categories
python docs/_scripts/generate_docs_screenshots.py --categories viewers segmentation

# Custom output directory
python docs/_scripts/generate_docs_screenshots.py --output /path/to/output

# Save metadata JSON
python docs/_scripts/generate_docs_screenshots.py --save-metadata
```

**Categories:**
- `cli` - CLI command examples (requires terminal automation)
- `viewers` - Viewer interfaces (capture, training, benchmark)
- `segmentation` - Episode segmentation viewer
- `architecture` - Architecture diagrams (requires mermaid rendering)

**Output:**
Screenshots are saved to `docs/assets/screenshots/` by default.

**Prerequisites:**
- For viewer screenshots: Generate HTML viewers first
  ```bash
  cd ../openadapt-viewer
  uv run openadapt-viewer demo --output viewer.html
  uv run python scripts/generate_segmentation_viewer.py --output segmentation_viewer.html
  ```

- For CLI screenshots: Install terminal automation tools
  - iTerm2 automation (macOS)
  - asciinema (cross-platform)
  - termtosvg (SVG output)

**Integration:**
Add generated screenshots to documentation:
```markdown
![Segmentation Viewer](assets/screenshots/segmentation_overview.png)
```

## Other Scripts

### `aggregate_docs.py` (planned)

Aggregates documentation from sub-package READMEs into the main docs site.

### `validate_links.py` (planned)

Validates all internal and external links in documentation.

### `test_examples.py` (planned)

Tests all code examples in documentation to ensure they work.

## Development

When adding new scripts:

1. Add comprehensive docstrings
2. Include usage examples
3. Update this README
4. Add error handling
5. Test on all platforms (if applicable)

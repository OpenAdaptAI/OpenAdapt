# Recording Catalog Quick Start

## What is it?

Automatic discovery system for OpenAdapt recordings and segmentation results. No more manual file selection!

## 3-Step Setup

```bash
# 1. Scan for recordings
uv run openadapt-viewer catalog scan

# 2. Generate viewer
uv run openadapt-viewer segmentation --output viewer.html

# 3. Open in browser
open viewer.html
```

Done! The viewer now has a dropdown of all recordings with segmentation results.

## Common Commands

```bash
# View all recordings
uv run openadapt-viewer catalog list

# Statistics
uv run openadapt-viewer catalog stats

# Auto-load specific recording
uv run openadapt-viewer segmentation --auto-load turn-off-nightshift --open

# Clean up stale entries
uv run openadapt-viewer catalog clean
```

## Python Usage

```python
from openadapt_viewer import get_catalog, scan_and_update_catalog

# Index recordings
scan_and_update_catalog()

# Query catalog
catalog = get_catalog()
recordings = catalog.get_all_recordings()
```

## Where is the data?

- **Catalog database**: `~/.openadapt/catalog.db`
- **Recordings**: `~/oa/src/openadapt-capture/`
- **Segmentation results**: `~/oa/src/openadapt-ml/segmentation_output/`

## Full Documentation

See [CATALOG_SYSTEM.md](CATALOG_SYSTEM.md) for complete details.

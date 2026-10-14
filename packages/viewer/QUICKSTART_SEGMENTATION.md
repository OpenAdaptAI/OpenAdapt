# Segmentation Viewer Quick Start

## Generate Viewer with Auto-Discovery

```bash
cd /path/to/openadapt-viewer
python scripts/generate_segmentation_viewer.py --output viewer.html --open
```

This will:
1. Scan for all episode files in default locations
2. Generate a standalone HTML viewer with embedded catalog
3. Open it in your default browser
4. Auto-load the latest episode file

## What You'll See

1. **Dropdown with available files**:
   - Latest file marked with ★
   - Shows: Recording name, episode count, date/time

2. **Auto-loaded episodes**:
   - Latest file loads automatically on page open
   - Status message: "Auto-loading latest: ..."

3. **Interactive controls**:
   - Select different files from dropdown
   - Click "Load Selected" to switch files
   - Click "Refresh" to reload page
   - Manual file input as fallback

## Locations Scanned

- `/path/to/openadapt-ml/segmentation_output/`
- `~/.openadapt/segmentation_output/`
- `./segmentation_output/` (current directory)

## Add New Episode Files

1. Create episode file in segmentation_output:
   ```json
   {
     "recording_id": "my-task",
     "episodes": [
       {"name": "...", "description": "...", "steps": [...]}
     ]
   }
   ```

2. Regenerate viewer to pick up new file:
   ```bash
   python scripts/generate_segmentation_viewer.py --output viewer.html
   ```

3. Open viewer - new file will be in dropdown

## Custom Scan Directories

```bash
python scripts/generate_segmentation_viewer.py \
  --output viewer.html \
  --scan-dir /path/to/my/episodes \
  --open
```

## Troubleshooting

### "No episode files found"
- Check that episode files exist in scan directories
- Verify file names: `episode_library.json` or `*_episodes.json`

### "Error loading file"
- Browser may block fetch() for `file://` protocol
- Use manual file input (shown automatically)
- Or serve via HTTP: `python -m http.server 8000`

### Viewer not updating
- Regenerate viewer to pick up new files:
  ```bash
  python scripts/generate_segmentation_viewer.py --output viewer.html
  ```
- Hard refresh browser (Cmd+Shift+R on macOS)

## Full Documentation

- [SEGMENTATION_VIEWER_AUTO_DISCOVERY.md](docs/SEGMENTATION_VIEWER_AUTO_DISCOVERY.md) - Complete technical documentation
- [CLAUDE.md](CLAUDE.md) - Developer guide with catalog system overview

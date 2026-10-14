# Segmentation Viewer Auto-Discovery

**Status**: Implemented (January 2026)

## Overview

The segmentation viewer now automatically discovers and loads episode files without requiring manual file selection. This feature eliminates the need for users to navigate file systems and manually select episode_library.json or *_episodes.json files.

## Features

### 1. Automatic Discovery
- Scans predefined directories for episode files
- Finds both consolidated libraries (`episode_library.json`) and per-recording files (`*_episodes.json`)
- Extracts metadata: recording name, creation time, episode count

### 2. Auto-Select Latest
- Automatically selects the most recently created episode file
- Auto-loads it on page load via fetch()
- Marks latest file with ★ in dropdown

### 3. Dropdown Selection
- Shows all available files in a dropdown
- Displays: Recording name, episode count, creation date/time
- User can select different files and reload

### 4. Graceful Fallback
- If catalog is not available: shows manual file input
- If fetch() fails (file:// protocol, CORS): shows manual file input with error message
- Manual selection always available as backup

## Usage

### Basic Usage

```bash
# Generate viewer with embedded catalog
cd /path/to/openadapt-viewer
python scripts/generate_segmentation_viewer.py --output viewer.html --open

# Open the generated viewer in your browser
# The latest episode file will auto-load
```

### Custom Scan Directories

```bash
# Scan additional directories for episode files
python scripts/generate_segmentation_viewer.py \
  --output viewer.html \
  --scan-dir /path/to/custom/segmentation_output \
  --scan-dir /another/path \
  --open
```

### CLI Options

```
--template PATH       Path to segmentation_viewer.html template
                      Default: ../segmentation_viewer.html

--output PATH, -o     Output HTML file path
                      Default: segmentation_viewer_with_catalog.html

--scan-dir PATH       Additional directory to scan for episode files
                      Can be specified multiple times

--open                Open the generated viewer in default browser
```

## How It Works

### 1. Discovery Phase (Python)

The `segmentation_catalog.py` module discovers episode files:

```python
from openadapt_viewer.segmentation_catalog import discover_episode_files

# Scans default locations + custom directories
entries = discover_episode_files(segmentation_dirs=[...])

# Returns list of SegmentationCatalogEntry objects:
# - file_path: Absolute path to JSON file
# - recording_name: Human-readable name
# - recording_id: Unique identifier
# - created_at: Unix timestamp
# - created_at_formatted: "YYYY-MM-DD HH:MM:SS"
# - episode_count: Number of episodes in file
# - file_type: "episode_library" or "episodes"
```

**Default scan locations**:
1. `~/oa/src/openadapt-ml/segmentation_output/`
2. `~/.openadapt/segmentation_output/`
3. `./segmentation_output/` (current directory)

### 2. JavaScript Generation (Python)

The catalog data is serialized to JavaScript:

```javascript
window.SEGMENTATION_CATALOG = {
  "files": [
    {
      "file_path": "/path/to/file.json",
      "recording_name": "Test Recording",
      "recording_id": "test-recording",
      "created_at": 1768668047.386,
      "created_at_formatted": "2026-01-17 11:40:47",
      "episode_count": 3,
      "file_type": "episodes"
    },
    // ... more files
  ],
  "generated_at": "2026-01-17T11:40:52.233967",
  "total_files": 4
};

// Helper functions
window.SegmentationCatalog = {
  getFiles: function() { ... },
  getLatest: function() { ... },
  getByRecordingId: function(recordingId) { ... },
  getLibrary: function() { ... },
  getRecordingFiles: function() { ... },
  getCount: function() { ... }
};
```

### 3. Embedding (Python)

The `generate_segmentation_viewer.py` script:
1. Reads `segmentation_viewer.html` template
2. Generates catalog JavaScript via `generate_catalog_javascript()`
3. Replaces `<script id="catalog-data">` placeholder with actual catalog
4. Writes standalone HTML file

```python
# Find and replace catalog placeholder
pattern = r'(<script id="catalog-data">)(.*?)(</script>)'
output_html = re.sub(pattern, catalog_js, template_html, flags=re.DOTALL)
```

### 4. Page Load (JavaScript)

When the viewer HTML loads in the browser:

```javascript
// 1. Check if catalog is available
function initializeCatalog() {
    if (window.SEGMENTATION_CATALOG && window.SEGMENTATION_CATALOG.files) {
        // 2. Populate dropdown
        populateFileDropdown();

        // 3. Show auto-discovery UI
        document.getElementById('auto-discovery-section').style.display = 'block';
        document.getElementById('manual-selection-section').style.display = 'none';

        // 4. Auto-load latest file
        const latest = window.SEGMENTATION_CATALOG.files[0];
        loadFileFromPath(latest.file_path);
    } else {
        // 5. Fallback to manual selection
        document.getElementById('auto-discovery-section').style.display = 'none';
        document.getElementById('manual-selection-section').style.display = 'block';
    }
}

// Run on page load
document.addEventListener('DOMContentLoaded', initializeCatalog);
```

### 5. Loading Episode Data (JavaScript)

```javascript
function loadFileFromPath(filePath) {
    fetch(filePath)
        .then(response => response.json())
        .then(data => {
            processData(data);  // Existing function to render episodes
            showStatus('Successfully loaded', 'success');
        })
        .catch(error => {
            showStatus('Error: ' + error.message, 'error');
            // Show manual selection as fallback
        });
}
```

## UI Components

### Auto-Discovery Section

Shown when catalog is available:

```html
<div id="auto-discovery-section">
    <label>Available Episode Files:</label>
    <select id="file-dropdown">
        <option value="/path/to/test.json">
            Test Recording (3 episodes) - 2026-01-17 11:40:47 ★ Latest
        </option>
        <option value="/path/to/turn-off.json">
            Turn Off Nightshift (0 episodes) - 2026-01-17 10:48:33
        </option>
    </select>
    <button onclick="loadFromDropdown()">Load Selected</button>
    <button onclick="refreshCatalog()">Refresh</button>
</div>
```

### Manual Selection Section

Shown when catalog is not available or as fallback:

```html
<div id="manual-selection-section">
    <p>Select an episode_library.json or *_episodes.json file...</p>
    <input type="file" id="file-input" accept=".json">
    <button onclick="loadFile()">Load File</button>
</div>
```

### Status Messages

```html
<div id="load-status">
    <!-- Dynamically populated with messages -->
    Auto-loading latest: Test Recording (2026-01-17 11:40:47)
</div>
```

**Message types**:
- **Info** (blue): "Auto-loading latest: ..."
- **Success** (green): "Successfully loaded: test_recording_episodes.json"
- **Error** (red): "Error loading file: ... Try manual file selection below."

## Regenerating Catalog

The catalog is embedded at generation time. To pick up new episode files:

### Option 1: Regenerate Viewer

```bash
python scripts/generate_segmentation_viewer.py --output viewer.html
```

This rescans for episode files and embeds fresh catalog data.

### Option 2: Click "Refresh" Button

The "Refresh" button in the viewer reloads the page:

```javascript
function refreshCatalog() {
    showStatus('Refreshing catalog...', 'info');
    setTimeout(() => location.reload(), 1500);
}
```

**Note**: This only works if the HTML file was regenerated. The catalog is static in the HTML.

## File Structure

```
openadapt-viewer/
├── src/openadapt_viewer/
│   ├── segmentation_catalog.py       # Discovery logic
│   ├── catalog.py                     # SQLite catalog DB
│   └── scanner.py                     # Recording scanner
├── scripts/
│   └── generate_segmentation_viewer.py  # CLI for viewer generation
├── segmentation_viewer.html          # Template with placeholders
└── docs/
    └── SEGMENTATION_VIEWER_AUTO_DISCOVERY.md  # This file
```

## Example: Generated Catalog

```javascript
window.SEGMENTATION_CATALOG = {
  "files": [
    {
      "file_path": "/path/to/openadapt-ml/segmentation_output/test_recording_episodes.json",
      "recording_name": "Test Recording",
      "recording_id": "test_recording",
      "created_at": 1768668047.3864803,
      "created_at_formatted": "2026-01-17 11:40:47",
      "episode_count": 3,
      "file_type": "episodes"
    },
    {
      "file_path": "/path/to/openadapt-ml/segmentation_output/episode_library.json",
      "recording_name": "Library (0 recordings)",
      "recording_id": "episode_library",
      "created_at": 1768664913.3536773,
      "created_at_formatted": "2026-01-17 10:48:33",
      "episode_count": 0,
      "file_type": "episode_library"
    }
  ],
  "generated_at": "2026-01-17T11:40:52.233967",
  "total_files": 2
};
```

## Error Handling

### Scenario 1: No Catalog Available

**Cause**: Viewer opened without catalog generation (opened `segmentation_viewer.html` directly)

**Behavior**:
- Auto-discovery section hidden
- Manual selection section shown
- No error message (expected behavior)

### Scenario 2: Fetch Fails (CORS/file://)

**Cause**: Browser blocks fetch() for `file://` protocol

**Behavior**:
- Error message: "Error loading file: ... Try manual file selection below."
- Manual selection section shown
- User can use file input to load episode data

### Scenario 3: Invalid Episode File

**Cause**: JSON parsing error or missing `episodes` field

**Behavior**:
- Alert: "Invalid data format. Expected episode library or extraction result JSON."
- Data not loaded
- User can select different file

### Scenario 4: No Episode Files Found

**Cause**: Scan directories have no episode files

**Behavior**:
- Dropdown shows: "No episode files found"
- Load button disabled
- Manual selection shown as fallback

## Integration with Catalog System

The segmentation viewer auto-discovery integrates with the broader catalog system:

### Recording Catalog
- `catalog.py`: SQLite database for recordings and segmentation results
- `scanner.py`: Scans for recordings and indexes them
- Used to get recording names from recording IDs

### Segmentation Catalog
- `segmentation_catalog.py`: Discovers episode files (extends catalog system)
- Does NOT require SQLite database (standalone)
- Queries `catalog.py` for recording names if available

### Relationship

```
RecordingCatalog (catalog.py)
        ↓ optional query
SegmentationCatalog (segmentation_catalog.py)
        ↓ discovers
Episode files (*.json)
        ↓ embeds into
Viewer HTML (standalone)
```

## Future Enhancements

### Dynamic Catalog Loading
Currently, the catalog is embedded at generation time (static). Future enhancement: load catalog dynamically via HTTP endpoint.

```javascript
// Future: Dynamic catalog loading
fetch('/api/segmentation/catalog')
    .then(response => response.json())
    .then(catalog => {
        window.SEGMENTATION_CATALOG = catalog;
        initializeCatalog();
    });
```

**Benefits**:
- No need to regenerate HTML for new files
- "Refresh" button actually rescans directories
- Works with hosted viewers

**Requirements**:
- HTTP server with catalog API endpoint
- CORS configuration
- Catalog cache/invalidation strategy

### Per-User Preferences
Store user's last selected file in localStorage:

```javascript
// Save selection
localStorage.setItem('lastSelectedFile', filePath);

// Restore on page load
const lastFile = localStorage.getItem('lastSelectedFile');
if (lastFile && fileExists(lastFile)) {
    loadFileFromPath(lastFile);
}
```

### Search/Filter
Add search box to filter episode files by name:

```html
<input type="text" placeholder="Search files..." oninput="filterFileDropdown()">
```

## Related Documentation

- [CLAUDE.md](../CLAUDE.md) - Main developer guide with catalog system overview
- [CATALOG_SYSTEM.md](../CATALOG_SYSTEM.md) - Full catalog system documentation
- [catalog.py](../src/openadapt_viewer/catalog.py) - Recording catalog API
- [segmentation_catalog.py](../src/openadapt_viewer/segmentation_catalog.py) - Segmentation discovery

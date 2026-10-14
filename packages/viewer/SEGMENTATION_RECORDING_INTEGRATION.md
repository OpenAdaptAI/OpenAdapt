# Segmentation Viewer & Recording Viewer Integration

## Overview

This document describes the integration between the **Segmentation Viewer** (which displays extracted episodes) and the **Capture Viewer** (which shows full recording playback). The integration allows users to seamlessly navigate from an episode overview to seeing it in the context of the complete recording.

## Integration Approach: External Link with URL Parameters

**Selected approach:** Option A - External Link

**Why this approach:**
1. **Simplicity**: Minimal code changes, easy to maintain
2. **File protocol compatible**: Works with both `file://` and `http://` URLs
3. **Separation of concerns**: Each viewer remains standalone
4. **No complex communication**: No iframe postMessage complexity
5. **Bookmarkable**: Users can bookmark specific episode contexts

## Architecture

```
┌─────────────────────────────┐
│  Segmentation Viewer        │
│  segmentation_viewer.html   │
│                             │
│  ┌───────────────────────┐  │
│  │ Episode: Navigate to  │  │
│  │ System Settings       │  │
│  │                       │  │
│  │ [View Full Recording] │──┼──┐
│  └───────────────────────┘  │  │
└─────────────────────────────┘  │
                                 │ URL with parameters:
                                 │ ?highlight_start=0.0
                                 │ &highlight_end=3.5
                                 │ &episode_name=Navigate+to+System+Settings
                                 │
                                 ▼
┌─────────────────────────────────────────────┐
│  Capture Viewer                             │
│  ../openadapt-capture/{recording-id}/       │
│  viewer.html                                │
│                                             │
│  ┌─────────────────────────────────────┐   │
│  │ 🔔 Viewing Episode Context          │   │
│  │ Episode: Navigate to System Settings│   │
│  │ (Time range: 0.0s - 3.5s)           │   │
│  └─────────────────────────────────────┘   │
│                                             │
│  Timeline: [====|█████|==========]          │
│           ^     ^     ^                     │
│           │     │     └─ End of recording   │
│           │     └─ Highlighted episode      │
│           └─ Start of recording             │
└─────────────────────────────────────────────┘
```

## Data Flow

### Episode JSON Structure

Episodes are stored in JSON files with the following key fields:

```json
{
  "recording_id": "turn-off-nightshift",
  "episodes": [
    {
      "episode_id": "episode_001",
      "name": "Navigate to System Settings",
      "description": "User opens System Settings...",
      "start_time": 0.0,
      "end_time": 3.5,
      "recording_ids": ["turn-off-nightshift"],
      "steps": [...],
      "boundary_confidence": 0.92
    }
  ]
}
```

### URL Parameters

When clicking "View Full Recording", the following parameters are passed:

| Parameter | Type | Description | Example |
|-----------|------|-------------|---------|
| `highlight_start` | float | Episode start time in seconds | `0.0` |
| `highlight_end` | float | Episode end time in seconds | `3.5` |
| `episode_name` | string | Episode name for display | `Navigate to System Settings` |

**Example URL:**
```
../openadapt-capture/turn-off-nightshift/viewer.html?highlight_start=0.0&highlight_end=3.5&episode_name=Navigate%20to%20System%20Settings
```

## Implementation Details

### Segmentation Viewer Changes

**File:** `/path/to/openadapt-viewer/segmentation_viewer.html`

#### 1. Added Recording Links Section (HTML)

```html
<div class="detail-section" id="recording-links-section" style="display: none;">
    <h3>View in Context</h3>
    <div id="recording-links" style="display: flex; flex-wrap: wrap; gap: 12px;"></div>
</div>
```

#### 2. Added Link Button Styles (CSS)

```css
.recording-link-btn {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 12px 20px;
    background: linear-gradient(135deg, #00d9ff 0%, #0099cc 100%);
    color: #0a0a0f;
    text-decoration: none;
    border-radius: 6px;
    font-weight: 600;
    transition: all 0.3s ease;
}
```

#### 3. Added Link Generation Logic (JavaScript)

In the `showEpisodeDetails()` function:

```javascript
// Recording links
const recordingLinksSection = document.getElementById('recording-links-section');
const recordingLinksContainer = document.getElementById('recording-links');
recordingLinksContainer.innerHTML = '';

if (episode.recording_ids && episode.recording_ids.length > 0) {
    recordingLinksSection.style.display = 'block';

    episode.recording_ids.forEach(recordingId => {
        // Construct absolute path to capture viewer
        // Works with file:// protocol when opening HTML directly
        const capturePath = `file:///path/to/openadapt-capture/${recordingId}/viewer.html`;

        const params = new URLSearchParams();
        if (episode.start_time !== undefined) {
            params.set('highlight_start', episode.start_time);
        }
        if (episode.end_time !== undefined) {
            params.set('highlight_end', episode.end_time);
        }
        if (episode.name) {
            params.set('episode_name', episode.name);
        }

        const url = params.toString() ? `${capturePath}?${params.toString()}` : capturePath;

        const link = document.createElement('a');
        link.href = url;
        link.className = 'recording-link-btn';
        link.target = '_blank';
        link.innerHTML = `
            <svg viewBox="0 0 24 24">
                <path d="M19 19H5V5h7V3H5c-1.11 0-2 .9-2 2v14c0 1.1.89 2 2 2h14c1.1 0 2-.9 2-2v-7h-2v7zM14 3v2h3.59l-9.83 9.83 1.41 1.41L19 6.41V10h2V3h-7z"/>
            </svg>
            <span>View Full Recording: ${recordingId}</span>
        `;
        recordingLinksContainer.appendChild(link);
    });
}
```

### Capture Viewer Changes

**File:** `/path/to/openadapt-viewer/capture_viewer.html`

#### 1. Added URL Parameter Handling (Alpine.js init)

```javascript
init() {
    // Parse URL parameters
    const params = new URLSearchParams(window.location.search);
    this.highlightStart = params.has('highlight_start') ? parseFloat(params.get('highlight_start')) : null;
    this.highlightEnd = params.has('highlight_end') ? parseFloat(params.get('highlight_end')) : null;
    this.episodeName = params.get('episode_name');

    // Jump to highlight start time if provided
    if (this.highlightStart !== null) {
        this.jumpToTimestamp(this.highlightStart);
    }
}
```

#### 2. Added Timestamp Navigation Function

```javascript
jumpToTimestamp(timestamp) {
    // Find the step closest to this timestamp
    for (let i = 0; i < this.steps.length; i++) {
        if (this.steps[i].timestamp >= timestamp) {
            this.currentStep = i;
            return;
        }
    }
    // If timestamp is after all steps, go to last step
    this.currentStep = this.steps.length - 1;
}
```

#### 3. Added Episode Context Banner

```html
<div x-data="{
    episodeName: new URLSearchParams(window.location.search).get('episode_name'),
    highlightStart: new URLSearchParams(window.location.search).get('highlight_start'),
    highlightEnd: new URLSearchParams(window.location.search).get('highlight_end')
}"
x-show="episodeName"
style="background: linear-gradient(135deg, rgba(255, 200, 0, 0.2), rgba(255, 165, 0, 0.2)); border: 2px solid rgba(255, 200, 0, 0.5); border-radius: 8px; padding: 16px; margin-bottom: 24px;">
    <div style="font-weight: 600;">Viewing Episode Context</div>
    <div>Episode: <span x-text="episodeName"></span></div>
    <div>(Time range: <span x-text="parseFloat(highlightStart).toFixed(1)"></span>s - <span x-text="parseFloat(highlightEnd).toFixed(1)"></span>s)</div>
</div>
```

#### 4. Added Timeline Highlight Overlay

```html
<!-- Episode highlight overlay -->
<template x-if="highlightStart !== null && highlightEnd !== null">
    <div style="position: absolute; top: 0; height: 100%; background: rgba(255, 200, 0, 0.3); border: 2px solid rgba(255, 200, 0, 0.8);"
         :style="`left: ${(highlightStart / steps[steps.length - 1].timestamp) * 100}%; width: ${((highlightEnd - highlightStart) / steps[steps.length - 1].timestamp) * 100}%`">
    </div>
</template>
```

#### 5. Added Episode Name Display Below Timeline

```html
<template x-if="episodeName">
    <div style="margin-top: 8px; text-align: center; font-size: 0.85rem; color: rgba(255, 200, 0, 0.9); font-weight: 600;">
        Episode: <span x-text="episodeName"></span>
    </div>
</template>
```

## User Experience Flow

### 1. Loading Episodes

1. User opens `segmentation_viewer.html`
2. Clicks "Load File" and selects an episode JSON file
3. Episodes are displayed in a grid with metadata

### 2. Viewing Episode Details

1. User clicks on an episode card
2. Episode details section appears showing:
   - Overview (description)
   - Information grid (application, duration, timestamps, confidence scores)
   - **View in Context** section with "View Full Recording" button(s)
   - Steps list
   - Timeline (if available)

### 3. Navigating to Recording Context

1. User clicks "View Full Recording: {recording_id}" button
2. Button opens in new tab (`target="_blank"`)
3. URL includes episode context parameters

### 4. Viewing in Recording Viewer

1. Capture viewer loads with URL parameters
2. **Episode context banner** appears at top showing:
   - "Viewing Episode Context"
   - Episode name
   - Time range
3. **Playback automatically jumps** to episode start time
4. **Timeline shows highlight overlay** in yellow/orange marking the episode segment
5. **Episode name displayed** below timeline

## File Structure Requirements

For the integration to work, the directory structure must be:

```
/path/to/
├── openadapt-viewer/
│   ├── segmentation_viewer.html     # Episode list viewer
│   └── test_episodes.json            # Example episode data
│
└── openadapt-capture/
    └── {recording-id}/               # e.g., turn-off-nightshift/
        ├── viewer.html               # Recording playback viewer
        ├── screenshots/              # Frame screenshots
        └── transcript.json           # Audio transcript
```

**Path assumptions:**
- Segmentation viewer is at: `openadapt-viewer/segmentation_viewer.html`
- Capture viewers are at: `openadapt-capture/{recording_id}/viewer.html`
- Absolute path to capture: `file:///path/to/openadapt-capture/{recording_id}/viewer.html`
  (Uses absolute file:// URL to work when opening HTML files directly in browser)

## Usage Instructions

### For Users

**Step 1: Load Episode Data**

1. Open `segmentation_viewer.html` in a web browser
2. Click "Load File" button
3. Select an episode JSON file (e.g., `test_episodes.json`)
4. Episodes appear in the grid

**Step 2: Explore Episodes**

1. Browse episodes using:
   - **Filter by Recording** dropdown
   - **Search** by name or description
2. Click an episode card to see details

**Step 3: View Full Recording**

1. Scroll to "View in Context" section
2. Click "View Full Recording: {recording_id}" button
3. Recording viewer opens in new tab, showing:
   - Episode context banner at top
   - Highlighted segment in timeline
   - Playback starting at episode start time

**Step 4: Navigate Recording**

1. Use playback controls (Play/Pause, Prev/Next)
2. Adjust playback speed (0.5x, 1x, 2x, 4x)
3. Click timeline to jump to any point
4. Yellow highlight shows episode boundaries

### For Developers

**Testing the Integration**

1. Open segmentation viewer:
   ```bash
   open /path/to/openadapt-viewer/segmentation_viewer.html
   ```

2. Load test episodes:
   - Use provided `test_episodes.json` file
   - Or create your own following the schema below

3. Click episode and then "View Full Recording" button

4. Verify in capture viewer:
   - [ ] Banner shows episode name and time range
   - [ ] Timeline has yellow highlight overlay
   - [ ] Playback starts at episode start time
   - [ ] Episode name appears below timeline

**Episode JSON Schema**

```json
{
  "recording_id": "string",
  "recording_name": "string",
  "episodes": [
    {
      "episode_id": "string",
      "name": "string",
      "description": "string",
      "application": "string (optional)",
      "start_time": "number (seconds)",
      "end_time": "number (seconds)",
      "start_time_formatted": "string (optional)",
      "end_time_formatted": "string (optional)",
      "duration": "number (seconds, optional)",
      "recording_ids": ["array of recording IDs"],
      "frame_indices": ["array of integers (optional)"],
      "steps": ["array of step descriptions"],
      "step_summaries": ["alternative to steps array"],
      "boundary_confidence": "number 0-1 (optional)",
      "coherence_score": "number 0-1 (optional)",
      "occurrence_count": "integer (for canonical episodes, optional)"
    }
  ],
  "boundaries": [...],
  "llm_model": "string",
  "processing_timestamp": "ISO 8601 string",
  "coverage": "number 0-1",
  "avg_confidence": "number 0-1"
}
```

## Technical Decisions & Rationale

### Why External Link (Not Iframe)?

| Approach | Pros | Cons | Selected |
|----------|------|------|----------|
| External Link | Simple, works with file://, bookmarkable | Opens new tab | ✅ Yes |
| Inline Embed | Seamless UX | Complex, file:// restrictions, security | ❌ No |
| Unified Viewer | Best UX | Major refactoring needed | ❌ No |
| Deep Link | Shows context | Same as External Link | ✅ Implemented |

### Why URL Parameters (Not localStorage)?

- **Cross-origin compatible**: Works with file:// protocol
- **Shareable**: URLs can be bookmarked or shared
- **Stateless**: No cleanup needed
- **Simple**: Standard web pattern

### Why Timestamp-based Navigation?

Episodes store `start_time` and `end_time` in seconds. The capture viewer's steps also have timestamps, making it straightforward to:
1. Find the step closest to `highlight_start`
2. Jump to that step on load
3. Highlight the range in the timeline

## Future Improvements

### Phase 1: Current Implementation ✅
- [x] External link from episode to recording
- [x] URL parameter passing
- [x] Auto-jump to episode start
- [x] Timeline highlight overlay
- [x] Episode context banner

### Phase 2: Enhancements (TODO)

1. **Bidirectional Navigation**
   - Add "View Episodes" button in capture viewer
   - Link back to segmentation viewer filtered to current recording

2. **Multi-Episode View**
   - Show all episodes from recording in sidebar
   - Click to jump between episodes
   - Visual timeline with all episode boundaries

3. **Episode Comparison**
   - Side-by-side view of multiple episodes
   - Show differences in steps/actions
   - Highlight common patterns

4. **Better Highlight Visualization**
   - Fade out non-episode frames
   - Add episode boundaries as markers on timeline
   - Show step numbers within episode

5. **Search & Filter in Recording**
   - Search for specific actions within episode
   - Filter by action type (click, type, scroll)
   - Navigate between matching actions

### Phase 3: Advanced Features (TODO)

1. **Episode Editing**
   - Adjust episode boundaries in recording viewer
   - Save modifications back to episode JSON
   - Split/merge episodes

2. **Annotation Tools**
   - Add notes to specific frames
   - Tag important actions
   - Export annotated episodes

3. **Performance Metrics**
   - Show action accuracy (if ground truth available)
   - Highlight errors or anomalies
   - Compare human vs AI actions

## Troubleshooting

### Issue: "View Full Recording" Button Not Appearing

**Symptoms:**
- Episode details show but no "View in Context" section

**Causes:**
1. Episode has no `recording_ids` field
2. `recording_ids` array is empty

**Solution:**
Ensure episode JSON includes:
```json
{
  "recording_ids": ["turn-off-nightshift"]
}
```

### Issue: Recording Viewer Shows 404 or ERR_FILE_NOT_FOUND

**Symptoms:**
- Clicking button opens blank page or shows "File not found"
- Browser shows `ERR_FILE_NOT_FOUND` error

**Causes:**
1. Recording directory doesn't exist
2. `viewer.html` not present in recording directory
3. Directory structure mismatch
4. Incorrect `recording_id` in JSON (e.g., "sample-" prefix that doesn't match actual directory)
5. Using relative path instead of absolute file:// URL

**Solution:**
1. Verify directory exists: `/path/to/openadapt-capture/{recording_id}/`
2. Check viewer file: `/path/to/openadapt-capture/{recording_id}/viewer.html`
3. Ensure `recording_id` in episode JSON matches directory name exactly (no "sample-" prefix unless directory has it)
4. Verify the segmentation viewer uses absolute file:// paths: `file:///path/to/openadapt-capture/${recordingId}/viewer.html`
5. Check test data files (e.g., `sample_segmentation_results.json`) have correct recording IDs

### Issue: No Timeline Highlight Appears

**Symptoms:**
- Banner shows episode name but timeline has no yellow highlight

**Causes:**
1. `highlight_start` or `highlight_end` missing from URL
2. Recording has no steps/timestamps
3. Calculation error in overlay positioning

**Solution:**
1. Check URL includes both parameters: `?highlight_start=X&highlight_end=Y`
2. Verify steps array in viewer has `timestamp` fields
3. Open browser console and check for JavaScript errors

### Issue: Playback Doesn't Jump to Episode Start

**Symptoms:**
- Viewer opens but starts at step 0, not episode start

**Causes:**
1. `jumpToTimestamp()` function not being called
2. No step matches the start timestamp
3. Alpine.js not initialized properly

**Solution:**
1. Check browser console for errors
2. Verify `init()` function is defined in x-data
3. Ensure Alpine.js is loaded: check for `<script defer src="...alpinejs..."></script>`

## Demo

A complete demo is available using the test data:

**File locations:**
- Segmentation viewer: `/path/to/openadapt-viewer/segmentation_viewer.html`
- Test episodes: `/path/to/openadapt-viewer/test_episodes.json`
- Recording viewer: `/path/to/openadapt-capture/turn-off-nightshift/viewer.html`

**Demo flow:**
1. Open segmentation viewer
2. Load `test_episodes.json`
3. Click "Navigate to System Settings" episode
4. Click "View Full Recording: turn-off-nightshift"
5. Observe episode context features in capture viewer

## Summary

The segmentation-recording integration provides a seamless way for users to explore extracted episodes while maintaining access to the full recording context. The external link approach is simple, maintainable, and works across different serving methods (file:// and http://).

Key benefits:
- **Easy navigation** from episodes to recordings
- **Visual context** with timeline highlighting
- **Automatic positioning** at episode start
- **Clear indicators** when viewing episode context
- **Minimal code changes** to existing viewers

The integration is production-ready and can be extended with the enhancements listed in the Future Improvements section.

---

# Screenshot Integration (Added January 2026)

## Overview

The segmentation viewer now displays screenshots from recordings throughout the UI to provide visual context:

1. **Thumbnails** - Episode cards show preview images in list view
2. **Key Frames Gallery** - Episode details display a grid of important frames
3. **Step Screenshots** - Inline images show what each step looks like

## Screenshot Data Structure

Episodes can include a `screenshots` object with thumbnail and key frames:

```json
{
  "episode_id": "episode_001",
  "name": "Navigate to System Settings",
  "steps": [
    "Click System Settings icon in dock",
    "Wait for Settings window to open",
    "Click on Displays in sidebar"
  ],
  "screenshots": {
    "thumbnail": "file:///path/to/screenshots/capture_31807990_step_0.png",
    "key_frames": [
      {
        "frame_index": 0,
        "step_index": 0,
        "path": "file:///path/to/screenshots/capture_31807990_step_0.png",
        "action": "Click System Settings icon in dock"
      },
      {
        "frame_index": 2,
        "step_index": 1,
        "path": "file:///path/to/screenshots/capture_31807990_step_2.png",
        "action": "Wait for Settings window to open"
      }
    ]
  }
}
```

## Screenshot Storage

Screenshots are stored in the recording's screenshots directory:

```
openadapt-capture/
  turn-off-nightshift/
    screenshots/
      capture_31807990_step_0.png
      capture_31807990_step_1.png
      capture_31807990_step_2.png
      ...
```

## Implementation

### 1. Episode Card Thumbnails

Episode cards now show a thumbnail at the top:

```html
<div class="episode-card">
    <div class="episode-thumbnail">
        <img src="file:///path/to/screenshot.png" alt="Episode Name" loading="lazy">
    </div>
    <div class="episode-content">
        <div class="episode-name">Navigate to System Settings</div>
        <div class="episode-description">User opens System Settings...</div>
    </div>
</div>
```

**CSS:**
```css
.episode-thumbnail {
    width: 100%;
    height: 160px;
    background: #0a0a0f;
    overflow: hidden;
}

.episode-thumbnail img {
    width: 100%;
    height: 100%;
    object-fit: cover;
}
```

- Uses `object-fit: cover` for consistent card height
- Falls back gracefully if no thumbnail available
- Lazy loading for performance

### 2. Key Frames Gallery

Episode details show a grid of key frames:

```html
<div class="detail-section key-frames-section">
    <h3>Key Frames</h3>
    <div class="key-frames-grid">
        <div class="key-frame-card">
            <img src="file:///path/to/screenshot.png" class="key-frame-img">
            <div class="key-frame-caption">
                <span class="key-frame-step-number">Step 1</span>
                Click System Settings icon in dock
            </div>
        </div>
    </div>
</div>
```

**CSS:**
```css
.key-frames-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
    gap: 16px;
}

.key-frame-img {
    width: 100%;
    height: 180px;
    object-fit: contain;
    background: #000;
}
```

- Responsive grid layout
- Uses `object-fit: contain` to preserve aspect ratio
- Shows step number badge and action description

### 3. Step Screenshots

Screenshots are displayed inline below each step:

```html
<ul class="step-list">
    <li class="step-item">
        <div>1. Click System Settings icon in dock</div>
        <img src="file:///path/to/screenshot.png" class="step-screenshot" loading="lazy">
    </li>
</ul>
```

**CSS:**
```css
.step-screenshot {
    margin-top: 12px;
    max-width: 100%;
    border-radius: 6px;
    border: 1px solid rgba(255, 255, 255, 0.1);
}
```

- Only shown if screenshot matches step index
- Full width within step container
- Lazy loading for performance

## Path Formats

Use absolute `file://` URLs for cross-directory access:

```json
"thumbnail": "file:///path/to/openadapt-capture/turn-off-nightshift/screenshots/capture_31807990_step_0.png"
```

**Important**: Use three slashes after `file:` for absolute paths.

## Generating Screenshot Metadata

When creating segmentation results, include screenshot paths in the episode data:

```python
from pathlib import Path

def add_screenshot_metadata(episode, recording_path):
    """Add screenshot paths to episode data."""
    screenshots_dir = recording_path / "screenshots"
    screenshot_files = sorted(screenshots_dir.glob("*.png"))

    key_frames = []
    for step_idx, frame_idx in enumerate(episode["frame_indices"]):
        if frame_idx < len(screenshot_files):
            screenshot_path = screenshot_files[frame_idx]
            key_frames.append({
                "frame_index": frame_idx,
                "step_index": step_idx,
                "path": f"file://{screenshot_path.absolute()}",
                "action": episode["steps"][step_idx]
            })

    episode["screenshots"] = {
        "thumbnail": f"file://{screenshot_files[episode['frame_indices'][0]].absolute()}",
        "key_frames": key_frames
    }

    return episode
```

## Selecting Key Frames

Choose key frames that best represent each step:

1. **First frame** - Initial state before the step
2. **Action frame** - Moment of interaction (click, type)
3. **Result frame** - State after action completes

Limit to 3-5 key frames per episode for performance.

## Performance Considerations

1. **Lazy Loading** - Uses `loading="lazy"` attribute for deferred loading
2. **Image Format** - PNG format (100-300KB typical)
3. **Key Frame Limit** - Recommend 3-5 per episode
4. **Object Fit** - `cover` for thumbnails, `contain` for details

## Browser Compatibility

The `file://` protocol has security restrictions:

- **Works**: Opening HTML file directly via `file://` URL
- **Blocked**: Accessing via HTTP server (CORS)
- **Solution**: Serve from same origin or embed as base64

## Testing Screenshot Integration

1. Open `/path/to/openadapt-viewer/segmentation_viewer.html`
2. Load `/path/to/openadapt-viewer/test_episodes.json`
3. Verify:
   - Episode cards show thumbnails
   - Click episode to see key frames gallery (3 images)
   - Steps section shows inline screenshots
   - All images load correctly

## Files Updated

- `/path/to/openadapt-viewer/segmentation_viewer.html` - Viewer UI
- `/path/to/openadapt-viewer/test_episodes.json` - Example data with screenshots
- `/path/to/openadapt-viewer/SEGMENTATION_RECORDING_INTEGRATION.md` - Documentation

## Future Enhancements

1. **Timeline Scrubber** - Browse all frames in episode with interactive timeline
2. **Click Overlays** - Show click markers on screenshots (H for human, AI for predicted)
3. **Zoom/Lightbox** - Click to view screenshot in full size
4. **Video Playback** - Generate video clip from frames
5. **Base64 Embedding** - Embed screenshots in JSON for portability

# Screenshot Loading Fix - Quick Reference

## The Problem
Screenshots not loading in segmentation_viewer.html due to browser `file://` protocol restrictions.

## The Solution
Change screenshot paths from absolute to relative.

## Quick Fix Command

If you generate new JSON files and screenshots don't load, check if paths are absolute:

```bash
# Check if paths are absolute (BAD)
grep "file://" your_episodes.json

# If found, convert to relative paths:
sed -i '' 's|file:///path/to/openadapt-capture/|../openadapt-capture/|g' your_episodes.json
```

## Test in 30 Seconds

```bash
# 1. Open viewer
open /path/to/openadapt-viewer/segmentation_viewer.html

# 2. Load test_episodes.json through the UI

# 3. Should see thumbnails immediately
# 4. Click an episode → should see key frames gallery
# 5. Check browser console → no errors
```

## Path Format Reference

| Format | Works? | Example |
|--------|--------|---------|
| Absolute file:// | ❌ NO | `file:///path/to/openadapt-capture/...` |
| Relative | ✅ YES | `../openadapt-capture/turn-off-nightshift/screenshots/...` |
| Absolute /path | ❌ NO | `/path/to/openadapt-capture/...` |

## What Was Changed

### test_episodes.json
```diff
- "thumbnail": "file:///path/to/openadapt-capture/turn-off-nightshift/screenshots/capture_31807990_step_0.png"
+ "thumbnail": "../openadapt-capture/turn-off-nightshift/screenshots/capture_31807990_step_0.png"
```

### segmentation_viewer.html
- Added console logging for debugging
- Added image error handlers
- No functional changes (viewer code was already correct)

## Verify Fix Works

```bash
# Open test page
open /path/to/openadapt-viewer/verify_screenshots.html

# Should show: "5 / 5 tests passed" ✓
```

## If Screenshots Still Don't Load

1. **Hard refresh browser**: Cmd+Shift+R
2. **Check paths are relative**: `grep "thumbnail" test_episodes.json`
3. **Verify files exist**: `ls ../openadapt-capture/turn-off-nightshift/screenshots/`
4. **Check console**: Open DevTools (Cmd+Opt+I) → Console tab

## Directory Structure

```
/path/to/
├── openadapt-viewer/
│   ├── segmentation_viewer.html  ← Viewer
│   └── test_episodes.json        ← Data (FIXED)
└── openadapt-capture/
    └── turn-off-nightshift/
        └── screenshots/
            ├── capture_31807990_step_0.png  ← Images
            ├── capture_31807990_step_2.png
            └── ...
```

Relative path: `../openadapt-capture/turn-off-nightshift/screenshots/...`

## Success Criteria (All Must Pass)

- ✓ Episode cards show thumbnail images
- ✓ Key frames gallery displays when clicking episode
- ✓ Steps show inline screenshots
- ✓ No console errors
- ✓ Works in Safari, Chrome, Firefox

## For New Recordings

When generating JSON for new recordings, use this Python snippet:

```python
from pathlib import Path

def make_screenshot_path(recording_id: str, step_idx: int) -> str:
    """Generate relative path from viewer directory."""
    return f"../openadapt-capture/{recording_id}/screenshots/capture_{recording_id}_step_{step_idx}.png"

# In your episode JSON:
episode = {
    "screenshots": {
        "thumbnail": make_screenshot_path(recording_id, 0),
        "key_frames": [
            {
                "path": make_screenshot_path(recording_id, step_idx),
                "step_index": step_idx,
                "action": action_description
            }
            for step_idx, action_description in enumerate(steps)
        ]
    }
}
```

## Need More Help?

See detailed guides:
- **SCREENSHOT_FIX_GUIDE.md** - Full testing procedure
- **FIX_SUMMARY.md** - Complete technical overview
- **TEST_RESULTS.md** - Root cause analysis

## Status: ✅ FIXED

All three screenshot features now work correctly:
1. Thumbnails in episode cards
2. Key frames gallery
3. Inline screenshots in steps

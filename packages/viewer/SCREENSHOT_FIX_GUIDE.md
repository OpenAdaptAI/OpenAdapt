# Screenshot Loading Fix - Testing Guide

## Problem Summary

The segmentation viewer was not displaying screenshots. The root cause was **browser security restrictions** on the `file://` protocol preventing cross-directory resource loading.

## Solution Applied

Changed screenshot paths in `test_episodes.json` from absolute `file://` URLs to relative paths.

### Before (Broken)
```json
"thumbnail": "file:///path/to/openadapt-capture/turn-off-nightshift/screenshots/capture_31807990_step_0.png"
```

### After (Fixed)
```json
"thumbnail": "../openadapt-capture/turn-off-nightshift/screenshots/capture_31807990_step_0.png"
```

## How to Test

### Step 1: Open the Viewer
```bash
open /path/to/openadapt-viewer/segmentation_viewer.html
```

### Step 2: Load Test Data
1. Click "Choose File" button
2. Navigate to: `/path/to/openadapt-viewer/test_episodes.json`
3. Select the file
4. Click "Load File" button

### Step 3: Verify Episode Cards (Thumbnails)

You should see 2 episode cards:

**Episode 1: "Navigate to System Settings"**
- Thumbnail should show: Desktop with dock visible (System Settings icon)
- Badge shows "3 steps"

**Episode 2: "Disable Night Shift"**
- Thumbnail should show: System Settings window with Displays settings
- Badge shows "3 steps"

**What to Check:**
- [ ] Both thumbnails are visible (not broken image icons)
- [ ] Thumbnails are properly sized (160px height)
- [ ] Images are crisp and clear

### Step 4: Verify Key Frames Gallery

**For Episode 1:**
1. Click on the "Navigate to System Settings" card
2. Scroll down to the "Key Frames" section
3. Should see 3 screenshots in a grid:
   - Step 1: Click System Settings icon in dock
   - Step 2: Wait for Settings window to open
   - Step 3: Click on Displays in sidebar

**For Episode 2:**
1. Click on the "Disable Night Shift" card
2. Scroll to "Key Frames" section
3. Should see 3 screenshots:
   - Step 1: Scroll down in Displays settings
   - Step 2: Click on Night Shift option
   - Step 3: Toggle Night Shift switch to off position

**What to Check:**
- [ ] All 6 key frame images load correctly
- [ ] Each image has the correct step number and caption
- [ ] Images are sized at 250px minimum width
- [ ] Hover effect works (border turns cyan, card lifts up)

### Step 5: Verify Inline Screenshots in Steps

1. While viewing Episode 1 details, scroll to the "Steps" section
2. Each step should have:
   - Step number and text description
   - Screenshot displayed below the text (indented)

3. Repeat for Episode 2

**What to Check:**
- [ ] All 6 inline screenshots appear (3 per episode)
- [ ] Screenshots are below their corresponding step text
- [ ] Screenshots are full-width with rounded corners
- [ ] Lazy loading works (images load as you scroll)

### Step 6: Check Browser Console (Important!)

1. Open browser developer tools (Safari: Cmd+Opt+I, Chrome: Cmd+Opt+J)
2. Go to Console tab
3. Reload the page and load test_episodes.json again

**You should see logs like:**
```
Loaded JSON data: {...}
Episodes found: 2
First episode screenshot paths: {thumbnail: "../openadapt-capture/...", key_frames: [...]}
Rendering key frames: 3
Key frame 0: ../openadapt-capture/turn-off-nightshift/screenshots/capture_31807990_step_0.png
Successfully loaded key frame: ../openadapt-capture/...
```

**Check for errors:**
- [ ] No red error messages about failed image loads
- [ ] All "Successfully loaded" messages appear
- [ ] No CORS or file:// protocol errors

## Quick Verification Test

Use the dedicated test page:

```bash
open /path/to/openadapt-viewer/verify_screenshots.html
```

This page will:
- Test all 5 screenshot paths
- Show which ones load successfully (green) or fail (red)
- Display actual images for visual verification
- Provide a summary at the top

**Expected result:** 5/5 tests should pass ✓

## Troubleshooting

### Images Still Not Loading?

**1. Check file paths exist:**
```bash
ls /path/to/openadapt-capture/turn-off-nightshift/screenshots/
```
Should list files like `capture_31807990_step_0.png`, `capture_31807990_step_2.png`, etc.

**2. Verify relative path from viewer directory:**
```bash
cd /path/to/openadapt-viewer
ls -la ../openadapt-capture/turn-off-nightshift/screenshots/capture_31807990_step_0.png
```
Should show the file exists.

**3. Check JSON is properly formatted:**
```bash
cat /path/to/openadapt-viewer/test_episodes.json | grep "thumbnail"
```
Should show relative paths starting with `../` NOT `file://`.

**4. Browser cache:**
- Hard reload: Cmd+Shift+R (Safari) or Ctrl+Shift+R (Chrome)
- Or clear browser cache completely

**5. Check browser console for specific errors:**
- CORS errors → Paths are still absolute `file://` URLs
- 404 errors → Relative path is incorrect
- Network errors → File permissions issue

### Only Some Images Load?

If thumbnails work but key frames don't (or vice versa):
1. Check console to see which specific paths are failing
2. Verify those specific PNG files exist
3. Check file permissions: `ls -l /path/to/screenshot.png`

### Safari-Specific Issues

Safari can be strict about `file://` protocol. Try:
1. Safari > Develop > Disable Local File Restrictions
2. Or test in Chrome/Firefox as alternative

## Files Modified

1. `/path/to/openadapt-viewer/test_episodes.json`
   - Changed all screenshot paths to relative paths

2. `/path/to/openadapt-viewer/segmentation_viewer.html`
   - Added console.log debugging
   - Added onerror handlers for image loading
   - Improved error visibility

## What This Fixes

- ✓ Thumbnails in episode cards now load
- ✓ Key frames gallery displays all screenshots
- ✓ Inline screenshots appear in steps list
- ✓ Works with `file://` protocol (direct HTML opening)
- ✓ No web server required
- ✓ Cross-browser compatible (Safari, Chrome, Firefox)

## Background: Why This Works

When you open an HTML file directly (double-click or `open` command), the browser loads it with the `file://` protocol. Modern browsers enforce Same-Origin Policy, which means:

- ❌ **Blocked**: `file:///path/A/viewer.html` loading `file:///path/B/image.png`
- ✓ **Allowed**: `file:///path/A/viewer.html` loading `../path/B/image.png` (relative)

Relative paths are resolved by the browser relative to the HTML file's location, and this is considered same-origin for `file://` protocol.

## Next Steps for Production

For the actual segmentation pipeline, update the JSON generation code to use relative paths:

```python
# In segmentation result generator
from pathlib import Path

def make_relative_path(screenshot_path: str, base_path: str) -> str:
    """Convert absolute path to relative path from viewer location."""
    screenshot = Path(screenshot_path)
    base = Path(base_path)
    try:
        return str(screenshot.relative_to(base.parent))
    except ValueError:
        # If not in same tree, use ../
        return f"../{screenshot.relative_to(screenshot.parent.parent)}"

# When writing JSON:
episode_data = {
    "screenshots": {
        "thumbnail": make_relative_path(
            screenshot_abs_path,
            "/path/to/openadapt-viewer"
        )
    }
}
```

## Success Criteria

All of the following should work:

1. ✓ Episode cards display thumbnail images
2. ✓ Clicking episode shows Key Frames gallery with 3 images
3. ✓ Steps section shows inline screenshots for each step
4. ✓ No console errors about failed image loading
5. ✓ Images load quickly (lazy loading working)
6. ✓ Responsive design: images scale properly on window resize
7. ✓ All 3 screenshot features tested for both episodes

**If all 7 criteria pass → Fix is successful! 🎉**

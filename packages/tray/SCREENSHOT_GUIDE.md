# Screenshot Generation Guide

This guide provides detailed instructions for generating professional screenshots for OpenAdapt Tray documentation.

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Quick Start](#quick-start)
3. [Screenshot List](#screenshot-list)
4. [Manual Capture Instructions](#manual-capture-instructions)
5. [Automated Helper Script](#automated-helper-script)
6. [Post-Processing](#post-processing)
7. [Adding to Documentation](#adding-to-documentation)

---

## Prerequisites

### Software Requirements

- **OpenAdapt Tray installed**: `pip install -e .` in `/Users/abrichr/oa/src/openadapt-tray`
- **Screenshot tool**:
  - macOS: Built-in (Cmd+Shift+4 or Cmd+Shift+5)
  - Windows: Snipping Tool or Snip & Sketch (Win+Shift+S)
  - Linux: gnome-screenshot, flameshot, or spectacle

### Optional Tools

- **Image editor**: For cropping and annotations
  - macOS: Preview (built-in) or Pixelmator
  - Cross-platform: GIMP, Photoshop
- **GIF creation**: For animated demos
  - macOS: Gifski or LICEcap
  - Cross-platform: ScreenToGif, Peek

### System Configuration

**macOS**:
- Notification permissions enabled for Python/Terminal
- System Preferences → Notifications → Python/Terminal → Allow notifications
- Do Not Disturb disabled temporarily

**Windows**:
- Focus Assist disabled temporarily
- Notifications enabled for Python

**Linux**:
- Notification daemon running
- Desktop environment supports notifications

---

## Quick Start

### Option 1: Use Automated Helper

```bash
cd /Users/abrichr/oa/src/openadapt-tray
python generate_screenshots.py
```

This script will:
1. Display a countdown before each screenshot
2. Trigger notifications automatically
3. Give you time to capture each screenshot
4. Guide you through the entire process

### Option 2: Manual Capture

1. Run the tray app: `python -m openadapt_tray`
2. Follow the [Manual Capture Instructions](#manual-capture-instructions) below
3. Capture screenshots using OS screenshot tool

---

## Screenshot List

### Essential Screenshots (Priority 1)

These screenshots are critical for documentation:

| # | Filename | Description | State | Instructions |
|---|----------|-------------|-------|--------------|
| 1 | `tray-icon-idle.png` | Tray icon in idle state | Idle | Capture menu bar with icon visible |
| 2 | `notification-basic.png` | Basic notification popup | Any | Show "Recording Started" notification |
| 3 | `notification-critical.png` | Critical error notification | Any | Show critical error notification |
| 4 | `notification-center.png` | Notification Center with grouped | Any | Open NC, show multiple notifications |
| 5 | `menu-structure.png` | Full tray menu | Idle | Right-click icon, capture menu |

### Optional Screenshots (Priority 2)

These enhance documentation but aren't critical:

| # | Filename | Description | State | Instructions |
|---|----------|-------------|-------|--------------|
| 6 | `tray-icon-recording.png` | Icon during recording | Recording | Capture red pulsing icon |
| 7 | `tray-icon-training.png` | Icon during training | Training | Capture purple gear icon |
| 8 | `tray-icon-error.png` | Icon in error state | Error | Capture red exclamation icon |
| 9 | `menu-recording.png` | Menu while recording | Recording | Show "Stop Recording" option |
| 10 | `notification-callback.png` | Notification with action | Any | Show "Click to view" notification |
| 11 | `system-settings.png` | System notification settings | Any | System Prefs → Notifications |

### Advanced Screenshots (Priority 3)

For comprehensive documentation:

| # | Filename | Description | State | Instructions |
|---|----------|-------------|-------|--------------|
| 12 | `notification-buttons.png` | Notification with action buttons | Any | macOS only, show buttons |
| 13 | `notification-reply.png` | Notification with reply field | Any | macOS only, show text field |
| 14 | `menu-captures.png` | Recent captures submenu | Any | Show captures list |
| 15 | `menu-training.png` | Training submenu | Any | Show training options |

---

## Manual Capture Instructions

### macOS Screenshot Workflow

#### 1. Setup

```bash
# Terminal 1: Run tray app
cd /Users/abrichr/oa/src/openadapt-tray
python -m openadapt_tray

# Terminal 2: Trigger notifications (if needed)
python test_notification_simple.py
```

#### 2. Capture Tray Icon

**Steps**:
1. Locate OpenAdapt icon in menu bar (top-right)
2. Press `Cmd+Shift+4` (cursor becomes crosshair)
3. Drag to select icon area
4. Release to capture
5. Screenshot saved to Desktop

**Tips**:
- Include surrounding menu bar for context
- Capture at 2x resolution (Retina) for clarity
- Save as: `tray-icon-idle.png`

#### 3. Capture Notifications

**Steps**:
1. Trigger notification (using script or app action)
2. Wait for notification to appear (top-right)
3. Press `Cmd+Shift+4` immediately
4. Drag to select notification popup
5. Release to capture

**Tips**:
- Capture quickly - notifications fade after 5-10 seconds
- Include slight margin around notification
- For Notification Center view: Click bell icon first, then screenshot
- Save as: `notification-basic.png`, `notification-critical.png`, etc.

#### 4. Capture Menu

**Steps**:
1. Right-click (or click) tray icon
2. Menu appears
3. Press `Cmd+Shift+4`
4. Select menu area
5. Release to capture

**Tips**:
- Include icon at top of menu
- Capture full menu, including separators
- Don't close menu while capturing
- Save as: `menu-structure.png`

#### 5. Capture Notification Center

**Steps**:
1. Click bell icon in menu bar (top-right)
2. Notification Center slides out
3. Press `Cmd+Shift+4`
4. Select Notification Center panel
5. Release to capture

**Tips**:
- Show multiple notifications for "grouping" example
- Scroll to show notification history
- Save as: `notification-center.png`

### Windows Screenshot Workflow

#### 1. Setup

```powershell
# PowerShell: Run tray app
cd C:\path\to\openadapt-tray
python -m openadapt_tray
```

#### 2. Capture System Tray

**Steps**:
1. Locate OpenAdapt icon in system tray (bottom-right)
2. May need to click "Show hidden icons" arrow
3. Press `Win+Shift+S`
4. Select area to capture
5. Notification appears: "Screenshot saved"
6. Open Snip & Sketch to edit/save

**Tips**:
- Include taskbar for context
- Use rectangular snip mode
- Save as PNG

#### 3. Capture Notifications

**Steps**:
1. Trigger notification
2. Action Center shows notification (bottom-right)
3. Press `Win+Shift+S`
4. Capture notification popup
5. Save from Snip & Sketch

**Tips**:
- Windows notifications appear in Action Center
- Can replay from Action Center if missed
- Capture both popup and Action Center view

#### 4. Capture Menu

**Steps**:
1. Right-click tray icon
2. Context menu appears
3. Press `Win+Shift+S`
4. Capture menu
5. Save from Snip & Sketch

**Tips**:
- Menu may close when pressing Win key - be quick!
- Alternative: Use Snipping Tool (pre-launch, delay mode)

### Linux Screenshot Workflow

#### 1. Setup

```bash
# Terminal: Run tray app
cd /path/to/openadapt-tray
python -m openadapt_tray
```

#### 2. Capture System Tray

**GNOME**:
```bash
gnome-screenshot -a  # Select area
```

**KDE**:
```bash
spectacle -r  # Rectangular region
```

**CLI**:
```bash
flameshot gui  # Interactive selection
```

**Tips**:
- System tray location varies by desktop environment
- May be top panel or bottom panel
- Ensure icon is visible (not hidden)

#### 3. Capture Notifications

**Steps**:
1. Trigger notification
2. Use screenshot tool quickly
3. Capture notification popup

**Tips**:
- Notifications often disappear quickly on Linux
- Use `flameshot gui` for quick interactive capture
- GNOME notifications appear top-center
- KDE notifications appear bottom-right

---

## Automated Helper Script

### Usage

```bash
cd /Users/abrichr/oa/src/openadapt-tray
python generate_screenshots.py
```

### What It Does

The script:
1. **Initializes** notification system
2. **Displays countdown** before each screenshot
3. **Triggers notification** or state
4. **Gives time** to capture manually
5. **Guides through** entire sequence
6. **Creates directory**: `docs/screenshots/`

### Script Output

```
OpenAdapt Tray Screenshot Generation Helper
================================================================================

This script will trigger various notifications and UI states.
Save screenshots to: /Users/abrichr/oa/src/openadapt-tray/docs/screenshots

Press Enter when ready to start...

Backend detected: desktop-notifier

Starting screenshot sequence...
================================================================================

Screenshot 1/10: Basic notification popup
  File: 01-idle-icon.png
  Instructions: Capture: Tray icon in idle state

  Triggering in 3 seconds...
  ✓ Triggered! Capture screenshot now...
  Time remaining in 5 seconds...

--------------------------------------------------------------------------------

Screenshot 2/10: Basic notification popup
  File: 02-basic-notification.png
  Instructions: Capture: Notification popup (Recording Started)

  Triggering in 3 seconds...
  ✓ Triggered! Capture screenshot now...
  Time remaining in 5 seconds...

...
```

### Customizing

Edit `generate_screenshots.py` to:
- Change timing (wait periods)
- Add more screenshots
- Modify notification text
- Add custom actions

---

## Post-Processing

### Cropping

Remove unnecessary background:

```bash
# macOS Preview
open screenshot.png
# Tools → Adjust Size → Crop
# Save

# ImageMagick (CLI)
convert screenshot.png -trim +repage screenshot-cropped.png
```

### Annotations

Add arrows, text, highlights:

**macOS Preview**:
1. Open image in Preview
2. Tools → Annotate
3. Add shapes, text, arrows
4. Save

**GIMP** (cross-platform):
1. Open image
2. Use text tool, arrow tool
3. Export as PNG

### Resizing

For Retina/high-DPI screenshots:

```bash
# Reduce to 50% for web
convert screenshot@2x.png -resize 50% screenshot.png

# Or keep 2x for documentation clarity
cp screenshot@2x.png screenshot.png
```

### Optimization

Reduce file size:

```bash
# macOS/Linux
pngcrush screenshot.png screenshot-optimized.png

# Or use online tools
# TinyPNG: https://tinypng.com/
```

---

## Adding to Documentation

### File Organization

```
openadapt-tray/
├── docs/
│   └── screenshots/
│       ├── tray-icon-idle.png
│       ├── tray-icon-recording.png
│       ├── notification-basic.png
│       ├── notification-critical.png
│       ├── menu-structure.png
│       └── notification-center.png
├── README.md
└── NOTIFICATIONS.md
```

### README.md

Add screenshots to key sections:

```markdown
## Features

- **System Tray Icon**: Shows in the menu bar (macOS) or system tray (Windows/Linux)

  ![Tray Icon](docs/screenshots/tray-icon-idle.png)

- **Native Notifications**: Modern notifications with desktop-notifier

  ![Notification](docs/screenshots/notification-basic.png)

## Menu Structure

![Menu](docs/screenshots/menu-structure.png)
```

### NOTIFICATIONS.md

Add to examples:

```markdown
## Basic Usage

Shows a notification like this:

![Basic Notification](docs/screenshots/notification-basic.png)

## Critical Notifications

Error notifications appear more prominently:

![Critical Notification](docs/screenshots/notification-critical.png)
```

### Creating GIF Animations

For animated workflows:

**macOS**:
```bash
# Using Gifski (install: brew install gifski)
# 1. Record video with QuickTime (Cmd+Shift+5)
# 2. Convert to GIF
gifski --fps 10 --quality 90 recording.mov -o workflow.gif
```

**Cross-platform**:
- ScreenToGif (Windows)
- Peek (Linux)
- LICEcap (macOS/Windows)

**Add to docs**:
```markdown
## Recording Workflow

![Recording Workflow](docs/screenshots/workflow.gif)
```

---

## Screenshot Checklist

Before considering screenshots complete:

### Quality Checks

- [ ] All screenshots are clear and in focus
- [ ] Text is readable (high DPI if needed)
- [ ] No personal information visible
- [ ] Consistent styling across screenshots
- [ ] Appropriate file format (PNG for UI, JPG for photos)
- [ ] Optimized file sizes (<500KB each)

### Coverage Checks

- [ ] Tray icon (at least idle state)
- [ ] Basic notification
- [ ] Critical/error notification
- [ ] Menu structure
- [ ] Notification Center view
- [ ] At least one state change (optional)

### Documentation Checks

- [ ] Screenshots added to README.md
- [ ] Screenshots added to NOTIFICATIONS.md
- [ ] File paths correct
- [ ] Alt text provided for accessibility
- [ ] Captions explain what's shown

---

## Tips and Best Practices

### Timing

- **Notifications**: Capture within 5-10 seconds
- **Menus**: Can stay open while capturing
- **Icons**: Static, capture anytime

### Quality

- **Resolution**: Capture at native resolution (Retina/2x on macOS)
- **Format**: PNG for UI screenshots (lossless)
- **Size**: Optimize but maintain clarity

### Consistency

- **Background**: Use same desktop background
- **Theme**: Use same system theme (light/dark)
- **Time**: Hide or crop clock if showing test data

### Privacy

- **Personal info**: Don't show real email, names, paths with usernames
- **Notifications**: Use example data, not real recordings
- **Desktop**: Clean desktop, no personal files visible

---

## Troubleshooting

### Notifications Don't Appear

**macOS**:
```bash
# Check permissions
# System Preferences → Notifications → Python/Terminal
# Ensure: Allow Notifications is ON

# Check Python signing
codesign -dv $(which python3)

# If unsigned, use official python.org installer
```

**Windows**:
```powershell
# Check Focus Assist
# Settings → System → Focus Assist → Off

# Check notification settings
# Settings → System → Notifications & actions
```

**Linux**:
```bash
# Check notification daemon
ps aux | grep -i notif

# Install if missing (Ubuntu/Debian)
sudo apt install libnotify-bin

# Test
notify-send "Test" "Testing notifications"
```

### Can't Capture Menu

**Issue**: Menu closes when pressing screenshot key

**Solution**:
- Use screenshot tool with delay mode
- macOS: Cmd+Shift+5 → Options → Timer (5s or 10s)
- Windows: Snipping Tool → Mode → Window Snip → Delay (5s)
- Linux: `gnome-screenshot -d 5` (5 second delay)

### Screenshots Too Large

**Solution**:
```bash
# Resize
convert input.png -resize 50% output.png

# Optimize
pngcrush input.png output.png

# Or use online tool: https://tinypng.com/
```

---

## Example Session

Complete screenshot capture session:

```bash
# 1. Setup
cd /Users/abrichr/oa/src/openadapt-tray
python -m openadapt_tray  # Terminal 1

# 2. Run helper script
python generate_screenshots.py  # Terminal 2

# 3. Follow prompts, capture each screenshot

# 4. Review screenshots
open docs/screenshots/

# 5. Post-process as needed
# - Crop
# - Annotate
# - Optimize

# 6. Add to documentation
# Edit README.md and NOTIFICATIONS.md

# 7. Commit
git add docs/screenshots/
git add README.md NOTIFICATIONS.md
git commit -m "docs: add screenshots for tray and notifications"
```

---

## Resources

### Screenshot Tools

- **macOS**: Built-in (Cmd+Shift+4/5)
- **Windows**: Snip & Sketch (Win+Shift+S), Snipping Tool
- **Linux**: gnome-screenshot, flameshot, spectacle
- **Cross-platform**: ShareX (Windows), Shutter (Linux)

### Image Editors

- **Basic**: Preview (macOS), Paint (Windows), Gwenview (Linux)
- **Advanced**: GIMP (free), Photoshop, Pixelmator

### GIF Creation

- **macOS**: Gifski, LICEcap
- **Windows**: ScreenToGif, LICEcap
- **Linux**: Peek, gifcurry

### Optimization

- **Online**: TinyPNG, Squoosh
- **CLI**: pngcrush, optipng, imagemagick

---

## Summary

This guide provides everything needed to generate professional screenshots for OpenAdapt Tray documentation. Use the automated helper script for notifications, and manual capture for tray icons and menus.

**Quick workflow**:
1. Run `generate_screenshots.py`
2. Capture suggested screenshots
3. Post-process as needed
4. Add to documentation
5. Commit and share

Questions? See [NOTIFICATIONS.md](NOTIFICATIONS.md) for notification details or [README.md](README.md) for general tray usage.

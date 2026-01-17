# Screenshot Capture - Simplified Workflow

**Time**: 10-15 minutes

This guide provides a streamlined workflow for capturing and organizing screenshots of the OpenAdapt tray application and notifications.

## Prerequisites

- macOS with OpenAdapt tray installed
- Screenshots save to Desktop (default macOS behavior)

## Workflow Overview

1. Take screenshots with Cmd+Shift+4
2. Run automated rename script
3. Commit to git

## Step 1: Take Screenshots (5-10 minutes)

Use macOS built-in screenshot tool: **Cmd+Shift+4** (selection mode)

### Required Screenshots (in order)

1. **Tray Icon (Idle)**
   - Look at menu bar, find OpenAdapt icon
   - Capture just the icon area
   - File will be: `Screen Shot [timestamp].png`

2. **Menu (Idle)**
   - Click the tray icon to open menu
   - Capture the entire menu dropdown
   - File will be: `Screen Shot [timestamp].png`

3. **Notification (Basic)**
   - Run: `uv run python test_notification_simple.py`
   - Wait for notification to appear
   - Capture the notification popup
   - File will be: `Screen Shot [timestamp].png`

4. **Notification (Critical)**
   - The test script sends multiple notifications
   - Capture the critical urgency notification (usually red/orange)
   - File will be: `Screen Shot [timestamp].png`

5. **Notification Center**
   - Open Notification Center (click clock in menu bar or swipe from right)
   - Capture showing OpenAdapt notifications grouped
   - File will be: `Screen Shot [timestamp].png`

### Optional Screenshots

6. **Tray Icon (Recording)**
   - Start a recording (if app is functional)
   - Capture the recording state icon

7. **Menu (Recording)**
   - While recording, capture the menu

8. **Recording Started Notification**
   - Capture notification when recording starts

## Step 2: Auto-Rename (2 minutes)

Once you've taken your screenshots, run the automated rename script:

```bash
cd /Users/abrichr/oa/src/openadapt-tray
python rename_screenshots.py
```

The script will:
1. Find all screenshots taken in the last 30 minutes on Desktop
2. Show you the list with timestamps
3. Ask for confirmation
4. Rename them to standardized names:
   - `tray-icon-idle.png`
   - `menu-idle.png`
   - `notification-basic.png`
   - `notification-critical.png`
   - `notification-center.png`
   - etc.
5. Move them to `docs/screenshots/`
6. Backup any existing files

### Example Output

```
OpenAdapt Screenshot Rename Tool
============================================================

Found 5 recent screenshots:
  1. Screen Shot 2026-01-17 at 14.23.45.png (taken at 14:23:45)
  2. Screen Shot 2026-01-17 at 14.24.12.png (taken at 14:24:12)
  3. Screen Shot 2026-01-17 at 14.25.03.png (taken at 14:25:03)
  4. Screen Shot 2026-01-17 at 14.25.38.png (taken at 14:25:38)
  5. Screen Shot 2026-01-17 at 14.26.15.png (taken at 14:26:15)

Will rename them to:
  1. tray-icon-idle.png
  2. menu-idle.png
  3. notification-basic.png
  4. notification-critical.png
  5. notification-center.png

Proceed? (y/n): y

  Moved Screen Shot 2026-01-17 at 14.23.45.png -> tray-icon-idle.png
  Moved Screen Shot 2026-01-17 at 14.24.12.png -> menu-idle.png
  Moved Screen Shot 2026-01-17 at 14.25.03.png -> notification-basic.png
  Moved Screen Shot 2026-01-17 at 14.25.38.png -> notification-critical.png
  Moved Screen Shot 2026-01-17 at 14.26.15.png -> notification-center.png

Done! 5 screenshots in docs/screenshots
```

## Step 3: Review and Commit (3 minutes)

1. **Review screenshots**:
   ```bash
   open docs/screenshots
   ```
   Check that each screenshot is clear and captures the right thing.

2. **If satisfied, commit**:
   ```bash
   git add docs/screenshots/
   git commit -m "Add tray notification screenshots

   - Tray icon in idle state
   - Menu structure
   - Notification examples (basic, critical)
   - Notification Center view

   Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
   ```

3. **Push to remote**:
   ```bash
   git push origin fix/add-readme-badges
   ```

## Troubleshooting

### No screenshots found

- Check Desktop for files starting with "Screen Shot" or "Screenshot"
- Check screenshot save location: System Preferences > Keyboard > Screenshots
- Take screenshots and run script within 30 minutes

### Wrong screenshots renamed

- Script includes timestamps to help verify order
- If wrong files selected, they're backed up with `.backup` extension
- Restore from backup and run script again

### Test notifications don't appear

- Check Notification Center settings for OpenAdapt
- System Preferences > Notifications > Python (or OpenAdapt)
- Ensure "Allow Notifications" is enabled
- Try running test script with sudo (may help with permissions)

### Screenshots look wrong

- No problem! Just delete them from `docs/screenshots/`
- Take new screenshots
- Run rename script again
- Existing files are automatically backed up

## Tips for Better Screenshots

1. **Clean up menu bar**: Hide unnecessary icons for cleaner captures
2. **Use system screenshot tool**: Cmd+Shift+5 gives more control
3. **High resolution**: Retina displays capture at 2x resolution automatically
4. **Timing**: Wait for animations to complete before capturing
5. **Lighting**: Use default light mode for consistency (unless showing dark mode)

## Alternative: Manual Rename

If you prefer to rename manually:

```bash
cd ~/Desktop
mv "Screen Shot 2026-01-17 at 14.23.45.png" tray-icon-idle.png
mv "Screen Shot 2026-01-17 at 14.24.12.png" menu-idle.png
# ... etc

mkdir -p /Users/abrichr/oa/src/openadapt-tray/docs/screenshots
mv *.png /Users/abrichr/oa/src/openadapt-tray/docs/screenshots/
```

## Next Steps

After screenshots are committed:

1. Update README.md to reference screenshots
2. Update NOTIFICATIONS.md with visual examples
3. Consider creating a visual guide document
4. Share PR for review

---

**Questions or issues?** Check the main SCREENSHOT_GUIDE.md for more detailed instructions.

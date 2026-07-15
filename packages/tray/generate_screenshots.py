#!/usr/bin/env python3
"""Screenshot generation helper for OpenAdapt Tray documentation.

This script helps capture screenshots by triggering notifications and UI states
in a controlled sequence, giving you time to capture screenshots manually.

Usage:
    python generate_screenshots.py

Requirements:
    - openadapt-tray installed and working
    - Manual screenshot tool ready (Cmd+Shift+4 on macOS)
    - Notification Center visible

Screenshots to capture:
    1. Tray icon (idle state)
    2. Tray icon (recording state)
    3. Tray icon (compiling state)
    4. Tray icon (break badge — automations need attention)
    5. Tray icon (error state)
    6. Basic notification popup
    7. Needs-attention notification popup
    8. Menu structure
    9. Notification Center with grouped notifications
    10. Settings/configuration (if available)
"""

import time
import sys
from pathlib import Path

# Try to import the notification system
try:
    from openadapt_tray.notifications import NotificationManager
    NOTIFICATIONS_AVAILABLE = True
except ImportError:
    NOTIFICATIONS_AVAILABLE = False
    print("WARNING: openadapt_tray not available. Install with: pip install -e .")

# Screenshot save location
SCREENSHOT_DIR = Path(__file__).parent / "docs" / "screenshots"
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

def countdown(seconds, message=""):
    """Display countdown timer."""
    for i in range(seconds, 0, -1):
        print(f"\r{message} in {i} seconds... ", end="", flush=True)
        time.sleep(1)
    print("\r" + " " * 80, end="\r")  # Clear line


def main():
    """Run the screenshot generation sequence."""
    print("=" * 80)
    print("OpenAdapt Tray Screenshot Generation Helper")
    print("=" * 80)
    print()
    print("This script will trigger various notifications and UI states.")
    print("You should manually capture screenshots using:")
    print("  • macOS: Cmd+Shift+4 (selection) or Cmd+Shift+5 (screen recording)")
    print("  • Windows: Win+Shift+S or Snipping Tool")
    print("  • Linux: gnome-screenshot or flameshot")
    print()
    print(f"Save screenshots to: {SCREENSHOT_DIR}")
    print()

    if not NOTIFICATIONS_AVAILABLE:
        print("ERROR: openadapt_tray not available!")
        print("Install with: pip install -e .")
        sys.exit(1)

    input("Press Enter when ready to start...")
    print()

    # Initialize notification manager
    notifications = NotificationManager()

    print(f"Backend detected: {notifications._backend}")
    print()

    # Sequence of screenshots
    screenshots = [
        {
            "name": "01-idle-icon",
            "description": "Tray icon in IDLE state",
            "action": lambda: print("  → Check menu bar for idle icon (blue/gray)"),
            "wait": 3,
            "instructions": "Capture: Tray icon in idle state"
        },
        {
            "name": "02-basic-notification",
            "description": "Basic notification popup",
            "action": lambda: notifications.show(
                "Recording Started",
                "Capturing: my-workflow"
            ),
            "wait": 2,
            "instructions": "Capture: Notification popup (Recording Started)"
        },
        {
            "name": "03-needs-attention-notification",
            "description": "Needs-attention notification (break badge source)",
            "action": lambda: notifications.show(
                "Automations need attention",
                "2 automations need attention",
                urgency="critical",
                on_clicked=lambda: print("  → Click routes to needs-attention"),
            ),
            "wait": 2,
            "instructions": "Capture: Needs-attention notification (critical urgency)"
        },
        {
            "name": "04-compiling-notification",
            "description": "Compiling notification (recording → workflow)",
            "action": lambda: notifications.show(
                "Compiling",
                "Building a workflow from: my-workflow",
            ),
            "wait": 2,
            "instructions": "Capture: Compiling notification"
        },
        {
            "name": "05-multiple-notifications",
            "description": "Multiple notifications (grouping)",
            "action": lambda: [
                notifications.show("Recording Started", "Capture session 1"),
                time.sleep(0.5),
                notifications.show("Recording Started", "Capture session 2"),
                time.sleep(0.5),
                notifications.show("Recording Started", "Capture session 3"),
            ],
            "wait": 2,
            "instructions": "Capture: Notification Center showing grouped notifications"
        },
        {
            "name": "06-recording-icon",
            "description": "Tray icon in RECORDING state",
            "action": lambda: print("  → NOTE: Recording icon requires running tray app"),
            "wait": 2,
            "instructions": "Capture: Recording icon (red - requires running app)"
        },
        {
            "name": "07-compiling-icon",
            "description": "Tray icon in COMPILING state",
            "action": lambda: print("  → NOTE: Compiling icon requires running tray app"),
            "wait": 2,
            "instructions": "Capture: Compiling icon (purple - requires running app)"
        },
        {
            "name": "07b-break-badge-icon",
            "description": "Tray icon with break badge (needs attention)",
            "action": lambda: print("  → NOTE: Badge icon requires running tray app with break_count > 0"),
            "wait": 2,
            "instructions": "Capture: Idle/recording icon with red break badge dot"
        },
        {
            "name": "08-error-icon",
            "description": "Tray icon in ERROR state",
            "action": lambda: print("  → NOTE: Error icon requires running tray app"),
            "wait": 2,
            "instructions": "Capture: Error icon (red exclamation - requires running app)"
        },
        {
            "name": "09-menu-structure",
            "description": "Tray menu structure",
            "action": lambda: print("  → NOTE: Menu requires running tray app - right-click icon"),
            "wait": 2,
            "instructions": "Capture: Full menu structure (requires running app)"
        },
        {
            "name": "10-notification-settings",
            "description": "System notification settings",
            "action": lambda: print("  → Open System Preferences → Notifications"),
            "wait": 2,
            "instructions": "Capture: System Preferences showing OpenAdapt notification settings"
        },
    ]

    print("Starting screenshot sequence...")
    print("=" * 80)
    print()

    for i, shot in enumerate(screenshots, 1):
        print(f"Screenshot {i}/{len(screenshots)}: {shot['description']}")
        print(f"  File: {shot['name']}.png")
        print(f"  Instructions: {shot['instructions']}")
        print()

        # Countdown before action
        countdown(3, f"  Triggering")

        # Execute action
        if callable(shot['action']):
            result = shot['action']()

        # Wait for notification to appear and give time to screenshot
        print(f"  ✓ Triggered! Capture screenshot now...")
        time.sleep(shot['wait'])

        # Give extra time to capture
        countdown(shot['wait'] + 3, f"  Time remaining")

        print()
        print("-" * 80)
        print()
        time.sleep(1)

    # Cleanup
    notifications.cleanup()

    print("=" * 80)
    print("Screenshot sequence complete!")
    print("=" * 80)
    print()
    print("Next steps:")
    print(f"  1. Review screenshots in: {SCREENSHOT_DIR}")
    print("  2. Crop and annotate as needed")
    print("  3. Add to documentation (README.md, NOTIFICATIONS.md)")
    print("  4. Create GIF animations (optional)")
    print()
    print("Recommended screenshots still needed:")
    print("  • Tray icon states (recording, compiling, break-badge, error) - requires running app")
    print("  • Menu structure - requires running app")
    print("  • Settings UI - if implemented")
    print()
    print("To capture these, run: python -m openadapt_tray")
    print("Then manually capture screenshots while interacting with the app.")
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nInterrupted by user. Exiting...")
        sys.exit(0)
    except Exception as e:
        print(f"\n\nERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

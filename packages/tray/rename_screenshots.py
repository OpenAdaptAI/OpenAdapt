#!/usr/bin/env python3
"""
Automatically find and rename screenshots from Desktop to docs/screenshots/.

Usage: Just take screenshots with Cmd+Shift+4, then run this script.
"""

import os
import shutil
from pathlib import Path
from datetime import datetime, timedelta


def find_recent_screenshots(minutes=30):
    """Find screenshots from Desktop taken in last N minutes."""
    desktop = Path.home() / "Desktop"
    cutoff = datetime.now() - timedelta(minutes=minutes)

    screenshots = []
    for file in desktop.glob("Screen Shot *.png"):
        mtime = datetime.fromtimestamp(file.stat().st_mtime)
        if mtime > cutoff:
            screenshots.append(file)

    # Also check for Screenshot (macOS Ventura+)
    for file in desktop.glob("Screenshot *.png"):
        mtime = datetime.fromtimestamp(file.stat().st_mtime)
        if mtime > cutoff:
            screenshots.append(file)

    return sorted(screenshots, key=lambda f: f.stat().st_mtime)


def rename_and_move_screenshots(screenshots, target_dir):
    """Interactively rename and move screenshots."""
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    # Predefined names for tray application screenshots
    names = [
        "tray-icon-idle.png",
        "menu-idle.png",
        "notification-basic.png",
        "notification-critical.png",
        "notification-center.png",
        "tray-icon-recording.png",
        "menu-recording.png",
        "notification-recording-started.png",
    ]

    print(f"\nFound {len(screenshots)} recent screenshots:")
    for i, screenshot in enumerate(screenshots, 1):
        mtime = datetime.fromtimestamp(screenshot.stat().st_mtime)
        time_str = mtime.strftime("%H:%M:%S")
        print(f"  {i}. {screenshot.name} (taken at {time_str})")

    print(f"\nWill rename them to:")
    for i, name in enumerate(names[:len(screenshots)], 1):
        print(f"  {i}. {name}")

    response = input("\nProceed? (y/n): ")
    if response.lower() != 'y':
        print("Cancelled.")
        return False

    for screenshot, new_name in zip(screenshots, names):
        dest = target_dir / new_name
        # Backup existing file if it exists
        if dest.exists():
            backup = target_dir / f"{dest.stem}.backup{dest.suffix}"
            shutil.move(str(dest), str(backup))
            print(f"  Backed up existing {dest.name} -> {backup.name}")
        shutil.move(str(screenshot), str(dest))
        print(f"  Moved {screenshot.name} -> {dest}")

    print(f"\nDone! {len(screenshots)} screenshots in {target_dir}")
    return True


def main():
    """Main entry point."""
    print("OpenAdapt Screenshot Rename Tool")
    print("=" * 60)

    # Find screenshots from last 30 minutes
    screenshots = find_recent_screenshots(minutes=30)

    if not screenshots:
        print("\nNo recent screenshots found on Desktop.")
        print("Tips:")
        print("  - Take screenshots with Cmd+Shift+4 (selection)")
        print("  - Or Cmd+Shift+5 (screenshot tool)")
        print("  - Screenshots save to Desktop by default")
        print("\nThen run this script again.")
        return 1

    # Target directory
    target_dir = Path(__file__).parent / "docs" / "screenshots"

    # Rename and move
    success = rename_and_move_screenshots(screenshots, target_dir)

    if success:
        print("\nNext steps:")
        print("  1. Check that screenshots look good:")
        print(f"     open {target_dir}")
        print("  2. Commit the screenshots:")
        print("     git add docs/screenshots/")
        print("     git commit -m 'Add tray notification screenshots'")
        print("  3. Push to remote:")
        print("     git push origin fix/add-readme-badges")

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

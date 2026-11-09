#!/usr/bin/env python3
"""Simple quick test for notification system."""

import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from openadapt_tray.notifications import NotificationManager


def main():
    """Run quick tests."""
    print("Testing OpenAdapt Notification System")
    print("=" * 60)

    nm = NotificationManager()
    print(f"Backend: {nm._backend}")
    print(f"Notifier available: {nm._notifier is not None}")

    # Test 1: Basic notification
    print("\n1. Sending basic notification...")
    result1 = nm.show(
        title="OpenAdapt Test",
        body="This is a basic test notification from desktop-notifier!"
    )
    print(f"   Result: {result1}")

    # Test 2: Critical notification
    print("\n2. Sending critical notification...")
    result2 = nm.show(
        title="OpenAdapt Critical",
        body="This is a critical notification!",
        urgency="critical"
    )
    print(f"   Result: {result2}")

    # Test 3: Notification with callback
    print("\n3. Sending notification with callback...")
    clicked = [False]

    def on_click():
        print("   [Callback triggered!]")
        clicked[0] = True

    result3 = nm.show(
        title="OpenAdapt Callback Test",
        body="Click this notification if you want to test callbacks",
        on_clicked=on_click
    )
    print(f"   Result: {result3}")

    # Summary
    print("\n" + "=" * 60)
    print("Summary:")
    print(f"  Basic notification: {'PASS' if result1 else 'FAIL'}")
    print(f"  Critical notification: {'PASS' if result2 else 'FAIL'}")
    print(f"  Callback notification: {'PASS' if result3 else 'FAIL'}")

    # Cleanup
    nm.cleanup()
    print("\nAll tests completed. Check Notification Center to see notifications!")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Simple test script for notification system."""

import sys
import time
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from openadapt_tray.notifications import NotificationManager


def test_basic_notification():
    """Test basic notification."""
    print("Testing basic notification...")
    nm = NotificationManager()
    result = nm.show(
        title="OpenAdapt Test",
        body="This is a basic test notification from desktop-notifier!"
    )
    print(f"Basic notification result: {result}")
    return result


def test_notification_with_callback():
    """Test notification with callback."""
    print("\nTesting notification with callback...")

    clicked = [False]  # Use list to allow modification in closure

    def on_click():
        print("Notification was clicked!")
        clicked[0] = True

    nm = NotificationManager()
    result = nm.show(
        title="OpenAdapt Test",
        body="Click this notification to test callbacks!",
        on_clicked=on_click
    )
    print(f"Callback notification result: {result}")

    # Wait a bit to see if user clicks
    print("Waiting 10 seconds for click...")
    time.sleep(10)

    if clicked[0]:
        print("Callback was triggered!")
    else:
        print("No click detected (this is normal if you didn't click)")

    return result


def test_critical_notification():
    """Test critical urgency notification."""
    print("\nTesting critical notification...")
    nm = NotificationManager()
    result = nm.show(
        title="OpenAdapt Critical",
        body="This is a critical notification!",
        urgency="critical"
    )
    print(f"Critical notification result: {result}")
    return result


def test_notification_with_buttons():
    """Test notification with action buttons."""
    print("\nTesting notification with buttons...")

    def on_click():
        print("Notification with buttons was clicked!")

    nm = NotificationManager()
    result = nm.show(
        title="OpenAdapt Actions",
        body="This notification has action buttons (if supported on your platform)",
        buttons=["Yes", "No", "Maybe"],
        on_clicked=on_click
    )
    print(f"Button notification result: {result}")

    # Wait a bit to see if user interacts
    print("Waiting 10 seconds for interaction...")
    time.sleep(10)

    return result


def main():
    """Run all tests."""
    print("=" * 60)
    print("OpenAdapt Notification System Test")
    print("=" * 60)

    tests = [
        test_basic_notification,
        test_notification_with_callback,
        test_critical_notification,
        test_notification_with_buttons,
    ]

    results = []
    for test in tests:
        try:
            result = test()
            results.append((test.__name__, result))
            time.sleep(2)  # Brief pause between tests
        except Exception as e:
            print(f"Error in {test.__name__}: {e}")
            results.append((test.__name__, False))

    # Summary
    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)
    for name, result in results:
        status = "PASS" if result else "FAIL"
        print(f"{name}: {status}")

    # Cleanup
    print("\nCleaning up...")
    nm = NotificationManager()
    nm.cleanup()
    print("Done!")


if __name__ == "__main__":
    main()

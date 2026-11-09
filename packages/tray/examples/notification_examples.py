#!/usr/bin/env python3
"""
OpenAdapt Tray Notification Examples

This file demonstrates various ways to use the notification system
in your OpenAdapt Tray application.
"""

import sys
import time
from pathlib import Path

# Add parent src to path for examples
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from openadapt_tray.notifications import NotificationManager


def example_basic():
    """Example 1: Basic notification."""
    print("Example 1: Basic Notification")
    print("-" * 60)

    notifications = NotificationManager()

    # Simplest form - just title and body
    notifications.show(
        title="OpenAdapt",
        body="This is a basic notification"
    )

    print("✓ Sent basic notification")
    notifications.cleanup()


def example_recording_started():
    """Example 2: Recording started notification."""
    print("\nExample 2: Recording Started")
    print("-" * 60)

    notifications = NotificationManager()

    # Notify user that recording has started
    notifications.show(
        title="Recording Started",
        body="Capturing: my-awesome-workflow",
        urgency="normal"
    )

    print("✓ Sent recording notification")
    notifications.cleanup()


def example_critical_error():
    """Example 3: Critical error notification."""
    print("\nExample 3: Critical Error")
    print("-" * 60)

    notifications = NotificationManager()

    # Show critical error - high priority
    notifications.show(
        title="Recording Failed",
        body="Could not access screen capture permissions",
        urgency="critical"
    )

    print("✓ Sent critical error notification")
    notifications.cleanup()


def example_with_callback():
    """Example 4: Notification with click callback."""
    print("\nExample 4: Notification with Callback")
    print("-" * 60)

    notifications = NotificationManager()

    # Track if notification was clicked
    clicked = {"value": False}

    def on_notification_clicked():
        """Called when user clicks the notification."""
        print("   [User clicked the notification!]")
        clicked["value"] = True
        # In a real app, you might:
        # - Open a dashboard
        # - Show details window
        # - Bring app to foreground

    notifications.show(
        title="Training Complete",
        body="Click to view results",
        on_clicked=on_notification_clicked
    )

    print("✓ Sent notification with callback")
    print("  (Click the notification to test the callback)")

    # Note: In a real app, the callback happens asynchronously
    # Here we just demonstrate the API
    notifications.cleanup()


def example_with_icon():
    """Example 5: Notification with custom icon."""
    print("\nExample 5: Notification with Icon")
    print("-" * 60)

    notifications = NotificationManager()

    # Assume we have an icon file
    icon_path = Path(__file__).parent.parent / "assets" / "icons" / "recording.png"

    if icon_path.exists():
        notifications.show(
            title="OpenAdapt",
            body="Notification with custom icon",
            icon_path=str(icon_path)
        )
        print(f"✓ Sent notification with icon: {icon_path}")
    else:
        # Send without icon if not found
        notifications.show(
            title="OpenAdapt",
            body="Custom icons require icon files in assets/icons/"
        )
        print("✓ Sent notification (icon file not found)")

    notifications.cleanup()


def example_action_buttons():
    """Example 6: Notification with action buttons (platform-dependent)."""
    print("\nExample 6: Notification with Action Buttons")
    print("-" * 60)

    notifications = NotificationManager()

    def on_button_clicked():
        """Called when user interacts with notification."""
        print("   [User clicked an action button!]")
        # In desktop-notifier, you can determine which button
        # was clicked via the callback parameter

    notifications.show(
        title="Recording Ready",
        body="What would you like to do?",
        buttons=["Play", "Edit", "Delete"],
        on_clicked=on_button_clicked
    )

    print("✓ Sent notification with action buttons")
    print("  (Note: Button support varies by platform)")
    notifications.cleanup()


def example_reply_field():
    """Example 7: Notification with reply field (macOS only)."""
    print("\nExample 7: Notification with Reply Field")
    print("-" * 60)

    notifications = NotificationManager()

    def on_reply_submitted():
        """Called when user submits reply."""
        print("   [User submitted a reply!]")
        # In desktop-notifier, the reply text is passed to the callback

    notifications.show(
        title="Name Your Recording",
        body="Enter a name for this capture",
        reply_field="Recording name",
        on_clicked=on_reply_submitted
    )

    print("✓ Sent notification with reply field")
    print("  (Note: Reply fields only work on macOS)")
    notifications.cleanup()


def example_notification_sequence():
    """Example 8: Sequence of notifications (simulating workflow)."""
    print("\nExample 8: Notification Sequence")
    print("-" * 60)

    notifications = NotificationManager()

    # Step 1: Starting
    notifications.show(
        title="Processing Started",
        body="Analyzing recorded actions...",
        urgency="low"
    )
    print("✓ Step 1: Started processing")

    # Simulate some work
    time.sleep(1)

    # Step 2: Progress
    notifications.show(
        title="Processing",
        body="50% complete - extracting patterns...",
        urgency="normal"
    )
    print("✓ Step 2: Progress update")

    # Simulate more work
    time.sleep(1)

    # Step 3: Complete
    notifications.show(
        title="Processing Complete",
        body="Successfully analyzed 127 actions",
        urgency="normal"
    )
    print("✓ Step 3: Complete")

    notifications.cleanup()


def example_conditional_notifications():
    """Example 9: Conditional notifications based on settings."""
    print("\nExample 9: Conditional Notifications")
    print("-" * 60)

    notifications = NotificationManager()

    # Simulate config
    config = {
        "show_notifications": True,
        "notify_on_start": True,
        "notify_on_complete": True,
        "notify_on_error": True,
    }

    # Recording started
    if config["show_notifications"] and config["notify_on_start"]:
        notifications.show(
            title="Recording Started",
            body="Capture in progress..."
        )
        print("✓ Sent start notification (enabled in config)")

    # Recording complete
    if config["show_notifications"] and config["notify_on_complete"]:
        notifications.show(
            title="Recording Complete",
            body="Saved successfully"
        )
        print("✓ Sent completion notification (enabled in config)")

    # Simulating disabled notification
    config["notify_on_error"] = False
    if config["show_notifications"] and config["notify_on_error"]:
        notifications.show(
            title="Error",
            body="This won't show"
        )
    else:
        print("✓ Skipped error notification (disabled in config)")

    notifications.cleanup()


def example_state_based_notifications():
    """Example 10: State-based notifications (from TrayApplication pattern)."""
    print("\nExample 10: State-Based Notifications")
    print("-" * 60)

    notifications = NotificationManager()

    # Simulate different application states
    states = {
        "IDLE": {"title": "Ready", "body": "OpenAdapt is ready"},
        "RECORDING": {"title": "Recording", "body": "Capture in progress"},
        "TRAINING": {"title": "Training", "body": "Model training started"},
        "ERROR": {"title": "Error", "body": "Something went wrong", "urgency": "critical"},
    }

    # Show notification for each state
    for state_name, notification in states.items():
        notifications.show(
            title=notification["title"],
            body=notification["body"],
            urgency=notification.get("urgency", "normal")
        )
        print(f"✓ {state_name}: {notification['title']}")
        time.sleep(0.5)

    notifications.cleanup()


def main():
    """Run all examples."""
    print("=" * 60)
    print("OpenAdapt Tray - Notification Examples")
    print("=" * 60)
    print()

    examples = [
        example_basic,
        example_recording_started,
        example_critical_error,
        example_with_callback,
        example_with_icon,
        example_action_buttons,
        example_reply_field,
        example_notification_sequence,
        example_conditional_notifications,
        example_state_based_notifications,
    ]

    try:
        for i, example_func in enumerate(examples, 1):
            example_func()
            if i < len(examples):
                time.sleep(2)  # Brief pause between examples

        print("\n" + "=" * 60)
        print("All examples completed!")
        print("Check your system's Notification Center to see the results.")
        print("=" * 60)

    except KeyboardInterrupt:
        print("\n\nExamples interrupted by user")
    except Exception as e:
        print(f"\n\nError running examples: {e}")


if __name__ == "__main__":
    main()

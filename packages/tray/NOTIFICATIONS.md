# Notification System

OpenAdapt Tray uses [desktop-notifier](https://github.com/samschott/desktop-notifier) for modern, native notifications across all platforms. This provides a rich notification experience with support for callbacks, action buttons, and platform-specific features.

## Overview

The notification system is implemented in `src/openadapt_tray/notifications.py` and provides:

- **Cross-platform support**: Works on macOS, Windows, and Linux
- **Native notifications**: Uses platform-native notification centers
- **Click callbacks**: Respond to user interactions
- **Action buttons**: Add interactive buttons (platform-dependent)
- **Reply fields**: Support text replies on macOS
- **Urgency levels**: Normal, low, and critical priorities
- **Graceful fallback**: Falls back to AppleScript/PowerShell if desktop-notifier unavailable

## Basic Usage

```python
from openadapt_tray.notifications import NotificationManager

# Initialize
notifications = NotificationManager()

# Show a simple notification
notifications.show(
    title="OpenAdapt",
    body="Recording started successfully!"
)

# Cleanup when done
notifications.cleanup()
```

## Advanced Features

### Click Callbacks

Respond when users click on notifications:

```python
def on_notification_clicked():
    print("User clicked the notification!")
    # Open dashboard, show details, etc.

notifications.show(
    title="Recording Complete",
    body="Click to view details",
    on_clicked=on_notification_clicked
)
```

### Urgency Levels

Control notification priority:

```python
# Low priority
notifications.show(
    title="Info",
    body="Background task complete",
    urgency="low"
)

# Critical priority (stays visible longer, may bypass Do Not Disturb)
notifications.show(
    title="Error",
    body="Recording failed!",
    urgency="critical"
)
```

### Action Buttons (Platform-Dependent)

Add interactive buttons to notifications:

```python
def on_click():
    print("User interacted with notification")

notifications.show(
    title="Training Complete",
    body="Choose what to do next",
    buttons=["View Results", "Start New Training", "Dismiss"],
    on_clicked=on_click
)
```

**Note**: Action button support varies by platform:
- **macOS**: Full support via Notification Center
- **Windows**: Limited support
- **Linux**: Depends on desktop environment

### Reply Fields (macOS Only)

Add text input to notifications on macOS:

```python
notifications.show(
    title="Name this recording",
    body="Enter a name for your capture",
    reply_field="Recording name",
    on_clicked=lambda: print("User submitted reply")
)
```

### Icon Support

Display custom icons with notifications:

```python
notifications.show(
    title="OpenAdapt",
    body="Custom icon notification",
    icon_path="/path/to/icon.png"
)
```

## Async API

For async contexts, use the async interface:

```python
import asyncio

async def send_notification():
    await notifications.show_async(
        title="Async Notification",
        body="Sent from async context"
    )

asyncio.run(send_notification())
```

## Architecture

### Backend Detection

The NotificationManager automatically selects the best backend:

1. **desktop-notifier** (preferred): If available, uses native notification APIs
   - macOS: Notification Center framework
   - Windows: WinRT Python bridge
   - Linux: DBus (org.freedesktop.Notifications)

2. **Fallback implementations**: If desktop-notifier unavailable
   - macOS: AppleScript `display notification`
   - Windows: PowerShell toast notifications or pystray
   - Linux: `notify-send` command

### Event Loop Management

desktop-notifier requires asyncio. The NotificationManager:
- Creates/reuses an event loop automatically
- Handles both sync and async contexts
- Cleans up resources on shutdown

```python
# The event loop is managed internally
notifications = NotificationManager()

# Works in sync contexts
notifications.show("Title", "Body")

# Also works in async contexts
await notifications.show_async("Title", "Body")

# Clean up when done
notifications.cleanup()
```

## Integration with TrayApplication

The notification system integrates seamlessly with the tray app:

```python
# In app.py
class TrayApplication:
    def __init__(self):
        self.notifications = NotificationManager()
        # ... other setup

    def _on_state_change(self, state: AppState):
        if self.config.show_notifications:
            self.notifications.show(
                title="State Changed",
                body=f"Now {state.state}"
            )

    def quit(self):
        self.notifications.cleanup()
        # ... other cleanup
```

## Configuration

Notifications can be configured via `tray.json`:

```json
{
  "show_notifications": true,
  "notification_duration_ms": 5000
}
```

- `show_notifications`: Enable/disable notifications (default: true)
- `notification_duration_ms`: Advisory duration in milliseconds (default: 5000)

**Note**: The actual display duration is controlled by the OS and may differ.

## Platform-Specific Notes

### macOS

- **Signing requirement**: Python must be properly signed for notifications on macOS 10.14+
  - Official python.org installer: Works out of the box
  - Homebrew Python: May not show notifications (unsigned)

- **Notification Center**: Notifications appear in Notification Center
- **Do Not Disturb**: Respects system DND settings (except critical urgency)
- **Grouping**: Notifications group by app name ("OpenAdapt")

### Windows

- **Windows 10+**: Full support via WinRT
- **Older Windows**: Falls back to PowerShell or pystray
- **Action Center**: Notifications appear in Action Center
- **Focus Assist**: Respects Focus Assist settings

### Linux

- **Requirements**: Requires `notify-send` or compatible notification daemon
- **Desktop environments**: Works with GNOME, KDE, XFCE, etc.
- **Fallback**: Uses `notify-send` command if desktop-notifier unavailable

## Testing

Test notifications manually:

```bash
# Run the test script
cd /Users/abrichr/oa/src/openadapt-tray
python test_notification_simple.py
```

Test script shows:
1. Basic notification
2. Critical notification
3. Notification with callback

Check your system's Notification Center to verify notifications appear correctly.

## Troubleshooting

### Notifications not appearing on macOS

1. Check Python signing:
   ```bash
   codesign -dv /path/to/python
   ```

2. Use official python.org installer instead of Homebrew

3. Check Notification Center settings:
   - System Preferences → Notifications
   - Ensure Python/Terminal is allowed

### Notifications not appearing on Windows

1. Check Focus Assist settings
2. Ensure Windows 10+ or fallback dependencies available
3. Check Action Center settings

### Notifications not appearing on Linux

1. Install notification daemon:
   ```bash
   # Ubuntu/Debian
   sudo apt install libnotify-bin

   # Fedora
   sudo dnf install libnotify
   ```

2. Check desktop environment supports notifications

## API Reference

### NotificationManager

#### `__init__()`
Initialize the notification manager. Automatically detects the best backend.

#### `show(title, body, icon_path=None, duration_ms=5000, on_clicked=None, urgency="normal", buttons=None, reply_field=None) -> bool`
Show a notification.

**Parameters:**
- `title` (str): Notification title
- `body` (str): Notification body text
- `icon_path` (str, optional): Path to icon image
- `duration_ms` (int): Advisory duration in milliseconds
- `on_clicked` (callable, optional): Callback when notification is clicked
- `urgency` (str): "low", "normal", or "critical"
- `buttons` (list, optional): List of button labels
- `reply_field` (str, optional): Reply field placeholder (macOS only)

**Returns:** `bool` - True if successful

#### `show_async(...) -> bool`
Async version of `show()`. Use in async contexts.

#### `set_tray_icon(icon)`
Set pystray icon for Windows fallback notifications.

#### `cleanup()`
Clean up resources. Call when shutting down the application.

## Migration from Old Implementation

If migrating from the old AppleScript/pystray implementation:

### Old API (Still Supported)
```python
# Basic usage still works
notifications.show("Title", "Body")
notifications.show("Title", "Body", duration_ms=3000)
```

### New Capabilities
```python
# Now you can also use:
notifications.show(
    "Title",
    "Body",
    on_clicked=callback,
    urgency="critical",
    buttons=["Action 1", "Action 2"]
)
```

The API is backward compatible - existing code continues to work without changes.

## Future Enhancements

Potential improvements:

1. **Notification history**: Track and replay recent notifications
2. **Custom sounds**: Per-notification sound support
3. **Rich media**: Images and videos in notification body
4. **Progress notifications**: Show progress bars in notifications
5. **Scheduled notifications**: Send notifications at specific times

## References

- [desktop-notifier GitHub](https://github.com/samschott/desktop-notifier)
- [desktop-notifier Documentation](https://desktop-notifier.readthedocs.io/)
- [macOS Notification Center](https://developer.apple.com/documentation/usernotifications)
- [Windows Notifications](https://docs.microsoft.com/en-us/windows/apps/design/shell/tiles-and-notifications/)
- [Linux Desktop Notifications](https://specifications.freedesktop.org/notification-spec/notification-spec-latest.html)

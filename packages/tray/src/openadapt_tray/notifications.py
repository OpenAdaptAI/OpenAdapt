"""Cross-platform notification manager for OpenAdapt Tray.

This module provides a unified notification interface using desktop-notifier
for modern, native notifications across all platforms.
"""

import asyncio
import os
import sys
import subprocess
from typing import Optional, Callable
from pathlib import Path

try:
    from desktop_notifier import DesktopNotifier, Urgency, Button, ReplyField
    DESKTOP_NOTIFIER_AVAILABLE = True
except ImportError:
    DESKTOP_NOTIFIER_AVAILABLE = False


class NotificationManager:
    """Cross-platform notification manager using desktop-notifier.

    This class provides a clean API for showing notifications with support for:
    - Titles and bodies
    - Icons
    - Click callbacks
    - Action buttons (platform dependent)
    - Reply fields (platform dependent)
    - Duration control

    Falls back to platform-specific implementations if desktop-notifier is unavailable.
    """

    def __init__(self):
        """Initialize the notification manager."""
        self._backend = self._detect_backend()
        self._tray_icon = None  # Set by TrayApplication for Windows fallback

        # Initialize desktop-notifier if available
        if DESKTOP_NOTIFIER_AVAILABLE and self._backend == "desktop-notifier":
            self._notifier = DesktopNotifier(
                app_name="OpenAdapt",
                notification_limit=10,  # Keep recent notifications
            )
            # Create event loop for async operations
            try:
                self._loop = asyncio.get_event_loop()
            except RuntimeError:
                self._loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._loop)
        else:
            self._notifier = None
            self._loop = None

    def _detect_backend(self) -> str:
        """Detect best notification backend for platform.

        Returns:
            Backend name: "desktop-notifier", "macos", "windows", or "linux"
        """
        if not DESKTOP_NOTIFIER_AVAILABLE:
            # Fall back to legacy implementations
            if sys.platform == "darwin":
                return "macos"
            elif sys.platform == "win32":
                return "windows"
            else:
                return "linux"

        # On macOS, check if running from app bundle
        if sys.platform == "darwin":
            # Check both APP_BUNDLE env var and actual bundle structure
            is_app_bundle = (
                os.environ.get('APP_BUNDLE') or
                'Contents/MacOS' in str(Path(__file__).resolve())
            )
            if not is_app_bundle:
                # Not in app bundle - try desktop-notifier anyway, fall back if it fails
                try:
                    from desktop_notifier.macos import CocoaNotificationCenter
                    # Try to initialize to see if it works
                    test_notifier = CocoaNotificationCenter()
                    print("desktop-notifier initialized successfully")
                    return "desktop-notifier"
                except Exception as e:
                    # If desktop-notifier can't initialize, use AppleScript
                    print(f"desktop-notifier not available ({e}), using AppleScript for notifications")
                    return "macos"

        # Use desktop-notifier on all platforms
        return "desktop-notifier"

    def set_tray_icon(self, icon) -> None:
        """Set the pystray icon for Windows fallback notifications.

        Args:
            icon: pystray.Icon instance.
        """
        self._tray_icon = icon

    def show(
        self,
        title: str,
        body: str,
        icon_path: Optional[str] = None,
        duration_ms: int = 5000,
        on_clicked: Optional[Callable] = None,
        urgency: str = "normal",
        buttons: Optional[list] = None,
        reply_field: Optional[str] = None,
    ) -> bool:
        """Show a notification.

        Args:
            title: Notification title.
            body: Notification body text.
            icon_path: Optional path to icon image.
            duration_ms: Notification duration in milliseconds (advisory).
            on_clicked: Optional callback when notification is clicked.
            urgency: Notification urgency: "low", "normal", or "critical".
            buttons: Optional list of button labels for interactive notifications.
            reply_field: Optional placeholder text for reply field (macOS only).

        Returns:
            True if notification was shown successfully.
        """
        try:
            if self._backend == "desktop-notifier":
                return self._show_desktop_notifier(
                    title, body, icon_path, on_clicked, urgency, buttons, reply_field
                )
            elif self._backend == "macos":
                return self._show_macos(title, body)
            elif self._backend == "windows":
                return self._show_windows(title, body, icon_path, duration_ms)
            else:
                return self._show_linux(title, body, icon_path)
        except Exception as e:
            print(f"Failed to show notification: {e}")
            return False

    def _show_desktop_notifier(
        self,
        title: str,
        body: str,
        icon_path: Optional[str],
        on_clicked: Optional[Callable],
        urgency: str,
        buttons: Optional[list],
        reply_field: Optional[str],
    ) -> bool:
        """Show notification using desktop-notifier.

        Args:
            title: Notification title.
            body: Notification body text.
            icon_path: Optional path to icon image.
            on_clicked: Optional callback when notification is clicked.
            urgency: Notification urgency level.
            buttons: Optional list of button labels.
            reply_field: Optional placeholder text for reply field.

        Returns:
            True if successful.
        """
        if not self._notifier or not self._loop:
            return False

        # Map urgency string to enum
        urgency_map = {
            "low": Urgency.Low,
            "normal": Urgency.Normal,
            "critical": Urgency.Critical,
        }
        urgency_level = urgency_map.get(urgency.lower(), Urgency.Normal)

        # Convert icon path to Path object if provided
        icon = Path(icon_path) if icon_path else None

        # Create button objects if provided
        button_objects = None
        if buttons:
            button_objects = [Button(label) for label in buttons]

        # Create reply field object if provided
        reply_field_object = None
        if reply_field:
            reply_field_object = ReplyField(title=reply_field, button_title="Send")

        # Show notification asynchronously
        try:
            if self._loop.is_running():
                # If loop is already running, schedule the coroutine
                asyncio.run_coroutine_threadsafe(
                    self._notifier.send(
                        title=title,
                        message=body,
                        icon=icon,
                        urgency=urgency_level,
                        buttons=button_objects,
                        reply_field=reply_field_object,
                        on_clicked=on_clicked,
                    ),
                    self._loop
                )
            else:
                # Run in the event loop
                self._loop.run_until_complete(
                    self._notifier.send(
                        title=title,
                        message=body,
                        icon=icon,
                        urgency=urgency_level,
                        buttons=button_objects,
                        reply_field=reply_field_object,
                        on_clicked=on_clicked,
                    )
                )
            return True
        except Exception as e:
            print(f"desktop-notifier error: {e}")
            return False

    def _show_macos(self, title: str, body: str) -> bool:
        """Show notification on macOS using AppleScript (fallback).

        Args:
            title: Notification title.
            body: Notification body text.

        Returns:
            True if successful.
        """
        # Escape special characters for AppleScript
        title = title.replace('"', '\\"').replace("\\", "\\\\")
        body = body.replace('"', '\\"').replace("\\", "\\\\")

        script = f'display notification "{body}" with title "{title}"'
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0

    def _show_windows(
        self,
        title: str,
        body: str,
        icon_path: Optional[str],
        duration_ms: int,
    ) -> bool:
        """Show notification on Windows using pystray's built-in notify (fallback).

        Args:
            title: Notification title.
            body: Notification body text.
            icon_path: Optional path to icon image.
            duration_ms: Notification duration in milliseconds.

        Returns:
            True if successful.
        """
        if self._tray_icon is not None:
            try:
                self._tray_icon.notify(body, title)
                return True
            except Exception:
                pass

        # Fallback to Windows toast notification via PowerShell
        try:
            script = f"""
            [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
            [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
            $template = @"
            <toast>
                <visual>
                    <binding template="ToastText02">
                        <text id="1">{title}</text>
                        <text id="2">{body}</text>
                    </binding>
                </visual>
            </toast>
"@
            $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
            $xml.LoadXml($template)
            $toast = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("OpenAdapt")
            $toast.Show($xml)
            """
            subprocess.run(
                ["powershell", "-Command", script],
                capture_output=True,
                timeout=10,
            )
            return True
        except Exception:
            return False

    def _show_linux(
        self,
        title: str,
        body: str,
        icon_path: Optional[str],
    ) -> bool:
        """Show notification on Linux using notify-send (fallback).

        Args:
            title: Notification title.
            body: Notification body text.
            icon_path: Optional path to icon image.

        Returns:
            True if successful.
        """
        cmd = ["notify-send", title, body]
        if icon_path:
            cmd.extend(["-i", icon_path])

        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0

    async def show_async(
        self,
        title: str,
        body: str,
        icon_path: Optional[str] = None,
        on_clicked: Optional[Callable] = None,
        urgency: str = "normal",
        buttons: Optional[list] = None,
        reply_field: Optional[str] = None,
    ) -> bool:
        """Show a notification asynchronously (desktop-notifier only).

        This is useful when called from async contexts. For most use cases,
        use the synchronous show() method instead.

        Args:
            title: Notification title.
            body: Notification body text.
            icon_path: Optional path to icon image.
            on_clicked: Optional callback when notification is clicked.
            urgency: Notification urgency: "low", "normal", or "critical".
            buttons: Optional list of button labels for interactive notifications.
            reply_field: Optional placeholder text for reply field (macOS only).

        Returns:
            True if notification was shown successfully.
        """
        if self._backend != "desktop-notifier" or not self._notifier:
            # Fall back to synchronous version for non-desktop-notifier backends
            return self.show(title, body, icon_path, on_clicked=on_clicked)

        # Map urgency string to enum
        urgency_map = {
            "low": Urgency.Low,
            "normal": Urgency.Normal,
            "critical": Urgency.Critical,
        }
        urgency_level = urgency_map.get(urgency.lower(), Urgency.Normal)

        # Convert icon path to Path object if provided
        icon = Path(icon_path) if icon_path else None

        # Create button objects if provided
        button_objects = None
        if buttons:
            button_objects = [Button(label) for label in buttons]

        # Create reply field object if provided
        reply_field_object = None
        if reply_field:
            reply_field_object = ReplyField(title=reply_field, button_title="Send")

        try:
            await self._notifier.send(
                title=title,
                message=body,
                icon=icon,
                urgency=urgency_level,
                buttons=button_objects,
                reply_field=reply_field_object,
                on_clicked=on_clicked,
            )
            return True
        except Exception as e:
            print(f"Failed to show async notification: {e}")
            return False

    def cleanup(self) -> None:
        """Cleanup notification resources.

        Call this when shutting down the application to ensure
        proper cleanup of async resources.
        """
        if self._loop and not self._loop.is_closed():
            try:
                # Cancel any pending tasks
                pending = asyncio.all_tasks(self._loop)
                for task in pending:
                    task.cancel()
                # Close the loop
                self._loop.close()
            except Exception as e:
                print(f"Error during notification cleanup: {e}")

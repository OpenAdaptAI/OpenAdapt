"""Cross-platform notification manager for OpenAdapt Tray."""

import sys
import subprocess
from typing import Optional


class NotificationManager:
    """Cross-platform notification manager."""

    def __init__(self):
        self._backend = self._detect_backend()
        self._tray_icon = None  # Set by TrayApplication for Windows

    def _detect_backend(self) -> str:
        """Detect best notification backend for platform."""
        if sys.platform == "darwin":
            return "macos"
        elif sys.platform == "win32":
            return "windows"
        else:
            return "linux"

    def set_tray_icon(self, icon) -> None:
        """Set the pystray icon for Windows notifications.

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
    ) -> bool:
        """Show a notification.

        Args:
            title: Notification title.
            body: Notification body text.
            icon_path: Optional path to icon image.
            duration_ms: Notification duration in milliseconds.

        Returns:
            True if notification was shown successfully.
        """
        try:
            if self._backend == "macos":
                return self._show_macos(title, body)
            elif self._backend == "windows":
                return self._show_windows(title, body, icon_path, duration_ms)
            else:
                return self._show_linux(title, body, icon_path)
        except Exception as e:
            print(f"Failed to show notification: {e}")
            return False

    def _show_macos(self, title: str, body: str) -> bool:
        """Show notification on macOS.

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
        """Show notification on Windows using pystray's built-in notify.

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
        """Show notification on Linux.

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

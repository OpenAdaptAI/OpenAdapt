"""macOS (Darwin) platform-specific implementations.

This module provides macOS-specific functionality for:
- Screen capture using Quartz
- Display information (resolution, Retina pixel ratio)
- Accessibility permission checking
"""

from __future__ import annotations

import sys

if sys.platform != "darwin":
    raise ImportError("This module is only available on macOS")


class DarwinPlatform:
    """macOS platform provider.

    Provides macOS-specific implementations for screen capture,
    display information, and accessibility checking.
    """

    @staticmethod
    def get_screen_dimensions() -> tuple[int, int]:
        """Get screen dimensions in physical pixels.

        On Retina displays, this returns the actual pixel dimensions,
        not the scaled logical dimensions.

        Returns:
            Tuple of (width, height) in physical pixels.
        """
        try:
            from PIL import ImageGrab
            screenshot = ImageGrab.grab()
            return screenshot.size
        except Exception:
            # Fallback using Quartz
            try:
                import Quartz

                main_display = Quartz.CGMainDisplayID()
                width = Quartz.CGDisplayPixelsWide(main_display)
                height = Quartz.CGDisplayPixelsHigh(main_display)
                return (width, height)
            except Exception:
                return (1920, 1080)

    @staticmethod
    def get_display_pixel_ratio() -> float:
        """Get the display pixel ratio for Retina displays.

        Returns 2.0 for Retina displays, 1.0 for standard displays.

        Returns:
            Pixel ratio (physical pixels / logical pixels).
        """
        try:
            import mss
            from PIL import ImageGrab

            # Get physical dimensions from screenshot
            screenshot = ImageGrab.grab()
            physical_width = screenshot.size[0]

            # Get logical dimensions from mss
            with mss.mss() as sct:
                # monitors[1] is typically the primary monitor
                monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                logical_width = monitor["width"]

            if logical_width > 0:
                return physical_width / logical_width

            return 1.0
        except ImportError:
            # Try using Quartz directly
            try:
                import Quartz

                main_display = Quartz.CGMainDisplayID()

                # Get physical dimensions
                physical_width = Quartz.CGDisplayPixelsWide(main_display)

                # Get logical dimensions using display mode
                mode = Quartz.CGDisplayCopyDisplayMode(main_display)
                if mode:
                    logical_width = Quartz.CGDisplayModeGetWidth(mode)
                    if logical_width > 0:
                        return physical_width / logical_width

                return 1.0
            except Exception:
                return 1.0
        except Exception:
            return 1.0

    @staticmethod
    def is_accessibility_enabled() -> bool:
        """Check if accessibility permissions are enabled.

        macOS requires accessibility permissions for capturing
        keyboard and mouse events globally.

        Returns:
            True if accessibility is enabled, False otherwise.
        """
        try:
            import Quartz  # noqa: F401 - needed for ApplicationServices

            # Check if we can access accessibility features
            # This uses the AXIsProcessTrustedWithOptions function
            from ApplicationServices import (
                AXIsProcessTrustedWithOptions,
                kAXTrustedCheckOptionPrompt,
            )

            # Check without prompting
            options = {kAXTrustedCheckOptionPrompt: False}
            return AXIsProcessTrustedWithOptions(options)
        except ImportError:
            # If ApplicationServices is not available, try a simpler check
            try:
                import subprocess

                result = subprocess.run(
                    [
                        "osascript",
                        "-e",
                        'tell application "System Events" to get name of first process',
                    ],
                    capture_output=True,
                    timeout=5,
                )
                return result.returncode == 0
            except Exception:
                return True  # Assume enabled if we can't check
        except Exception:
            return True  # Assume enabled if we can't check

    @staticmethod
    def get_active_window_info() -> dict | None:
        """Get information about the currently active window.

        Returns:
            Dictionary with window info (title, app_name, bounds) or None.
        """
        try:
            import Quartz

            # Get the list of windows
            options = Quartz.kCGWindowListOptionOnScreenOnly
            window_list = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID)

            if not window_list:
                return None

            # Find the frontmost window (layer 0 is typically the frontmost)
            for window in window_list:
                layer = window.get("kCGWindowLayer", -1)
                if layer == 0:
                    bounds = window.get("kCGWindowBounds", {})
                    return {
                        "title": window.get("kCGWindowName", ""),
                        "app_name": window.get("kCGWindowOwnerName", ""),
                        "bounds": {
                            "x": bounds.get("X", 0),
                            "y": bounds.get("Y", 0),
                            "width": bounds.get("Width", 0),
                            "height": bounds.get("Height", 0),
                        },
                        "pid": window.get("kCGWindowOwnerPID", 0),
                    }

            return None
        except Exception:
            return None


__all__ = ["DarwinPlatform"]

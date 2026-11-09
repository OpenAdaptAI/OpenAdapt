"""macOS (Darwin) platform-specific implementations.

This module provides macOS-specific functionality for:
- Screen capture using Quartz
- Display information (resolution, Retina pixel ratio)
- Accessibility permission checking
"""

from __future__ import annotations

import sys

from openadapt_capture.platform import DisplayMetricsUnavailable

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

        Raises:
            DisplayMetricsUnavailable: If the screen could not be measured.
        """
        try:
            from PIL import ImageGrab
            screenshot = ImageGrab.grab()
            return screenshot.size
        except Exception as grab_exc:
            # Fallback using Quartz
            try:
                import Quartz

                main_display = Quartz.CGMainDisplayID()
                width = Quartz.CGDisplayPixelsWide(main_display)
                height = Quartz.CGDisplayPixelsHigh(main_display)
                return (width, height)
            except Exception as quartz_exc:
                raise DisplayMetricsUnavailable(
                    "Could not measure macOS screen dimensions: "
                    f"ImageGrab failed ({grab_exc}) and Quartz failed "
                    f"({quartz_exc})"
                ) from quartz_exc

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

            raise DisplayMetricsUnavailable(
                "macOS reported a non-positive logical display width; "
                "the pixel ratio could not be measured"
            )
        except ImportError as import_exc:
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

                raise DisplayMetricsUnavailable(
                    "Quartz did not report a usable display mode; "
                    "the pixel ratio could not be measured"
                )
            except DisplayMetricsUnavailable:
                raise
            except Exception as quartz_exc:
                raise DisplayMetricsUnavailable(
                    "Could not measure the macOS pixel ratio: mss/PIL missing "
                    f"({import_exc}) and Quartz failed ({quartz_exc})"
                ) from quartz_exc
        except DisplayMetricsUnavailable:
            raise
        except Exception as exc:
            raise DisplayMetricsUnavailable(
                f"Could not measure the macOS pixel ratio: {exc}"
            ) from exc

    @staticmethod
    def is_accessibility_enabled() -> bool | None:
        """Check if accessibility permissions are enabled.

        macOS requires accessibility permissions for capturing
        keyboard and mouse events globally.

        Returns:
            True if verified enabled, False if verified disabled, and None if
            the permission state could not be determined. None must not be
            read as True: an unchecked permission is not a granted one.
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
                return None  # Undetermined - never report this as enabled
        except Exception:
            return None  # Undetermined - never report this as enabled

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

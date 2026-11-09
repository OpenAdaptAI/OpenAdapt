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
    def _main_display_pixel_size() -> tuple[int, int]:
        """Physical pixel size of the main display via Quartz (no framebuffer read).

        ``CGDisplayPixelsWide`` / ``High`` follow the looking-glass size on
        scaled Retina modes; ``CGDisplayModeGetPixelWidth`` / ``Height`` are
        the framebuffer dimensions that match ``mss`` / ``ImageGrab`` grabs.
        """
        import Quartz

        main_display = Quartz.CGMainDisplayID()
        mode = Quartz.CGDisplayCopyDisplayMode(main_display)
        if mode is None:
            raise DisplayMetricsUnavailable(
                "Quartz did not report a display mode for the main display"
            )
        width = int(Quartz.CGDisplayModeGetPixelWidth(mode))
        height = int(Quartz.CGDisplayModeGetPixelHeight(mode))
        if width <= 0 or height <= 0:
            raise DisplayMetricsUnavailable(
                "Quartz reported a non-positive main-display pixel size: "
                f"{width}x{height}"
            )
        return width, height

    @staticmethod
    def get_screen_dimensions() -> tuple[int, int]:
        """Get screen dimensions in physical pixels.

        On Retina displays, this returns the actual pixel dimensions,
        not the scaled logical dimensions. Prefers Quartz mode metadata so
        callers (video startup) never wait on a full-screen framebuffer read.

        Returns:
            Tuple of (width, height) in physical pixels.

        Raises:
            DisplayMetricsUnavailable: If the screen could not be measured.
        """
        try:
            return DarwinPlatform._main_display_pixel_size()
        except DisplayMetricsUnavailable:
            raise
        except Exception as quartz_exc:
            try:
                from PIL import ImageGrab

                screenshot = ImageGrab.grab()
                if screenshot.size[0] > 0 and screenshot.size[1] > 0:
                    return screenshot.size
                raise DisplayMetricsUnavailable(
                    "ImageGrab returned a non-positive screen size: "
                    f"{screenshot.size}"
                )
            except DisplayMetricsUnavailable:
                raise
            except Exception as grab_exc:
                raise DisplayMetricsUnavailable(
                    "Could not measure macOS screen dimensions: "
                    f"Quartz failed ({quartz_exc}) and ImageGrab failed "
                    f"({grab_exc})"
                ) from grab_exc

    @staticmethod
    def get_display_pixel_ratio() -> float:
        """Get the display pixel ratio for Retina displays.

        Returns 2.0 for Retina displays, 1.0 for standard displays.
        Prefers Quartz mode metadata so recording startup never waits on a
        full-screen framebuffer read.

        Returns:
            Pixel ratio (physical pixels / logical pixels).
        """
        try:
            import Quartz

            main_display = Quartz.CGMainDisplayID()
            mode = Quartz.CGDisplayCopyDisplayMode(main_display)
            if mode is None:
                raise DisplayMetricsUnavailable(
                    "Quartz did not report a display mode for the main display"
                )
            physical_width = float(Quartz.CGDisplayModeGetPixelWidth(mode))
            logical_width = float(Quartz.CGDisplayModeGetWidth(mode))
            if logical_width <= 0 or physical_width <= 0:
                raise DisplayMetricsUnavailable(
                    "Quartz reported a non-positive display width while "
                    f"measuring pixel ratio: physical={physical_width} "
                    f"logical={logical_width}"
                )
            return physical_width / logical_width
        except DisplayMetricsUnavailable:
            raise
        except Exception as quartz_exc:
            try:
                import mss
                from PIL import ImageGrab

                screenshot = ImageGrab.grab()
                physical_width = screenshot.size[0]
                with mss.mss() as sct:
                    monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                    logical_width = monitor["width"]
                if logical_width > 0 and physical_width > 0:
                    return physical_width / logical_width
                raise DisplayMetricsUnavailable(
                    "macOS reported a non-positive display width while "
                    "measuring pixel ratio from a screenshot fallback"
                )
            except DisplayMetricsUnavailable:
                raise
            except Exception as grab_exc:
                raise DisplayMetricsUnavailable(
                    "Could not measure the macOS pixel ratio: "
                    f"Quartz failed ({quartz_exc}) and the screenshot "
                    f"fallback failed ({grab_exc})"
                ) from grab_exc

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

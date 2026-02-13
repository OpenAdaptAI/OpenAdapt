"""Windows platform-specific implementations.

This module provides Windows-specific functionality for:
- Screen capture using Win32 API
- Display information (resolution, DPI scaling)
- Accessibility/permission checking
"""

from __future__ import annotations

import sys

if sys.platform != "win32":
    raise ImportError("This module is only available on Windows")


class WindowsPlatform:
    """Windows platform provider.

    Provides Windows-specific implementations for screen capture,
    display information, and permission checking.
    """

    @staticmethod
    def get_screen_dimensions() -> tuple[int, int]:
        """Get screen dimensions in physical pixels.

        On high-DPI displays, this returns the actual pixel dimensions,
        accounting for DPI scaling.

        Returns:
            Tuple of (width, height) in physical pixels.
        """
        try:
            from PIL import ImageGrab
            screenshot = ImageGrab.grab()
            return screenshot.size
        except Exception:
            # Fallback using ctypes
            try:
                import ctypes

                user32 = ctypes.windll.user32
                # Make process DPI aware to get correct dimensions
                try:
                    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-monitor DPI aware
                except Exception:
                    try:
                        user32.SetProcessDPIAware()
                    except Exception:
                        pass

                width = user32.GetSystemMetrics(0)  # SM_CXSCREEN
                height = user32.GetSystemMetrics(1)  # SM_CYSCREEN
                return (width, height)
            except Exception:
                return (1920, 1080)

    @staticmethod
    def get_display_pixel_ratio() -> float:
        """Get the display pixel ratio for high-DPI displays.

        Returns the DPI scaling factor. For example, 1.5 for 150% scaling.

        Returns:
            Pixel ratio (physical pixels / logical pixels).
        """
        try:
            import ctypes

            # Get DPI for the primary monitor
            try:
                # Windows 8.1+ method
                shcore = ctypes.windll.shcore
                dpi = ctypes.c_uint()
                shcore.GetDpiForMonitor(
                    ctypes.windll.user32.MonitorFromPoint(ctypes.wintypes.POINT(0, 0), 1),
                    0,  # MDT_EFFECTIVE_DPI
                    ctypes.byref(dpi),
                    ctypes.byref(ctypes.c_uint()),
                )
                return dpi.value / 96.0  # 96 DPI is the baseline (100% scaling)
            except Exception:
                pass

            # Fallback: Get DPI from device context
            try:
                user32 = ctypes.windll.user32
                gdi32 = ctypes.windll.gdi32

                hdc = user32.GetDC(0)
                dpi = gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
                user32.ReleaseDC(0, hdc)

                return dpi / 96.0
            except Exception:
                pass

            return 1.0
        except Exception:
            return 1.0

    @staticmethod
    def is_accessibility_enabled() -> bool:
        """Check if the application can capture input events.

        On Windows, input capture typically works without special permissions,
        but we check if we're running with sufficient privileges.

        Returns:
            True if input capture is available, False otherwise.
        """
        try:
            import ctypes

            # Check if running as administrator
            try:
                ctypes.windll.shell32.IsUserAnAdmin()
                # Even non-admin can typically capture input
                return True
            except Exception:
                return True  # Assume enabled
        except Exception:
            return True

    @staticmethod
    def get_active_window_info() -> dict | None:
        """Get information about the currently active window.

        Returns:
            Dictionary with window info (title, app_name, bounds) or None.
        """
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32

            # Get foreground window handle
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return None

            # Get window title
            title_length = user32.GetWindowTextLengthW(hwnd) + 1
            title_buffer = ctypes.create_unicode_buffer(title_length)
            user32.GetWindowTextW(hwnd, title_buffer, title_length)
            title = title_buffer.value

            # Get window rectangle
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))

            # Get process name
            process_name = ""
            try:
                import psutil

                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                process = psutil.Process(pid.value)
                process_name = process.name()
            except Exception:
                pass

            return {
                "title": title,
                "app_name": process_name,
                "bounds": {
                    "x": rect.left,
                    "y": rect.top,
                    "width": rect.right - rect.left,
                    "height": rect.bottom - rect.top,
                },
                "hwnd": hwnd,
            }
        except Exception:
            return None


__all__ = ["WindowsPlatform"]

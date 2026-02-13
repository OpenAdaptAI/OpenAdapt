"""Linux platform-specific implementations.

This module provides Linux-specific functionality for:
- Screen capture using X11 or Wayland
- Display information (resolution, scaling)
- Accessibility/permission checking
"""

from __future__ import annotations

import sys

if not sys.platform.startswith("linux"):
    raise ImportError("This module is only available on Linux")


class LinuxPlatform:
    """Linux platform provider.

    Provides Linux-specific implementations for screen capture,
    display information, and permission checking.
    """

    @staticmethod
    def _is_wayland() -> bool:
        """Check if running under Wayland.

        Returns:
            True if Wayland, False if X11.
        """
        import os

        return os.environ.get("XDG_SESSION_TYPE") == "wayland" or os.environ.get(
            "WAYLAND_DISPLAY"
        )

    @staticmethod
    def get_screen_dimensions() -> tuple[int, int]:
        """Get screen dimensions in physical pixels.

        Works with both X11 and Wayland (falls back to PIL).

        Returns:
            Tuple of (width, height) in physical pixels.
        """
        try:
            from PIL import ImageGrab
            screenshot = ImageGrab.grab()
            return screenshot.size
        except Exception:
            # Fallback for X11
            try:
                import subprocess

                result = subprocess.run(
                    ["xdpyinfo"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    for line in result.stdout.split("\n"):
                        if "dimensions:" in line:
                            # Parse "dimensions:    1920x1080 pixels"
                            parts = line.split()
                            for part in parts:
                                if "x" in part and part[0].isdigit():
                                    w, h = part.split("x")
                                    return (int(w), int(h))
            except Exception:
                pass

            # Fallback for Wayland using wlr-randr
            try:
                import subprocess

                result = subprocess.run(
                    ["wlr-randr"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    for line in result.stdout.split("\n"):
                        if "current" in line.lower():
                            # Parse resolution from wlr-randr output
                            parts = line.split()
                            for part in parts:
                                if "x" in part and part[0].isdigit():
                                    dims = part.split("x")
                                    if len(dims) == 2:
                                        try:
                                            return (int(dims[0]), int(dims[1].split("@")[0]))
                                        except ValueError:
                                            pass
            except Exception:
                pass

            return (1920, 1080)

    @staticmethod
    def get_display_pixel_ratio() -> float:
        """Get the display pixel ratio for HiDPI displays.

        Returns the scaling factor set in the desktop environment.

        Returns:
            Pixel ratio (physical pixels / logical pixels).
        """
        import os

        # Check GNOME scaling factor
        try:
            import subprocess

            result = subprocess.run(
                ["gsettings", "get", "org.gnome.desktop.interface", "scaling-factor"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                factor = int(result.stdout.strip())
                if factor > 0:
                    return float(factor)
        except Exception:
            pass

        # Check GDK_SCALE environment variable
        gdk_scale = os.environ.get("GDK_SCALE")
        if gdk_scale:
            try:
                return float(gdk_scale)
            except ValueError:
                pass

        # Check QT_SCALE_FACTOR
        qt_scale = os.environ.get("QT_SCALE_FACTOR")
        if qt_scale:
            try:
                return float(qt_scale)
            except ValueError:
                pass

        # Check for mss-based calculation
        try:
            import mss
            from PIL import ImageGrab

            screenshot = ImageGrab.grab()
            physical_width = screenshot.size[0]

            with mss.mss() as sct:
                monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                logical_width = monitor["width"]

            if logical_width > 0:
                return physical_width / logical_width
        except Exception:
            pass

        return 1.0

    @staticmethod
    def is_accessibility_enabled() -> bool:
        """Check if input capture is available.

        On Linux, this typically requires:
        - X11: xdotool or similar tool access
        - Wayland: Portal permissions or root access

        Returns:
            True if input capture is likely available, False otherwise.
        """
        import os

        # Check if running as root (always has access)
        if os.geteuid() == 0:
            return True

        # Check for X11
        if not LinuxPlatform._is_wayland():
            # X11 typically allows input capture
            display = os.environ.get("DISPLAY")
            return display is not None

        # Wayland is more restrictive
        # Check if we have portal access
        try:
            import subprocess

            result = subprocess.run(
                [
                    "dbus-send",
                    "--session",
                    "--dest=org.freedesktop.portal.Desktop",
                    "--type=method_call",
                    "--print-reply",
                    "/org/freedesktop/portal/desktop",
                    "org.freedesktop.DBus.Properties.Get",
                    "string:org.freedesktop.portal.RemoteDesktop",
                    "string:version",
                ],
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0
        except Exception:
            pass

        # Assume enabled if we can't determine
        return True

    @staticmethod
    def get_active_window_info() -> dict | None:
        """Get information about the currently active window.

        Returns:
            Dictionary with window info (title, app_name, bounds) or None.
        """
        # Try X11 first
        if not LinuxPlatform._is_wayland():
            try:
                import subprocess

                # Get active window ID
                result = subprocess.run(
                    ["xdotool", "getactivewindow"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode != 0:
                    return None

                window_id = result.stdout.strip()

                # Get window name
                name_result = subprocess.run(
                    ["xdotool", "getwindowname", window_id],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                title = name_result.stdout.strip() if name_result.returncode == 0 else ""

                # Get window geometry
                geo_result = subprocess.run(
                    ["xdotool", "getwindowgeometry", window_id],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )

                x, y, width, height = 0, 0, 0, 0
                if geo_result.returncode == 0:
                    for line in geo_result.stdout.split("\n"):
                        if "Position:" in line:
                            # Parse "Position: 100,200 (screen: 0)"
                            pos = line.split(":")[1].split("(")[0].strip()
                            parts = pos.split(",")
                            if len(parts) == 2:
                                x, y = int(parts[0]), int(parts[1])
                        elif "Geometry:" in line:
                            # Parse "Geometry: 800x600"
                            geo = line.split(":")[1].strip()
                            if "x" in geo:
                                parts = geo.split("x")
                                width, height = int(parts[0]), int(parts[1])

                # Get process info
                pid_result = subprocess.run(
                    ["xdotool", "getwindowpid", window_id],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                pid = 0
                app_name = ""
                if pid_result.returncode == 0:
                    try:
                        pid = int(pid_result.stdout.strip())
                        # Get process name
                        with open(f"/proc/{pid}/comm") as f:
                            app_name = f.read().strip()
                    except Exception:
                        pass

                return {
                    "title": title,
                    "app_name": app_name,
                    "bounds": {
                        "x": x,
                        "y": y,
                        "width": width,
                        "height": height,
                    },
                    "window_id": window_id,
                    "pid": pid,
                }
            except Exception:
                return None

        # Wayland doesn't provide easy access to window info
        # due to security model
        return None


__all__ = ["LinuxPlatform"]

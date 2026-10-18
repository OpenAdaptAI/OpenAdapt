"""Platform-specific implementations for GUI event capture.

This module provides platform-specific implementations for:
- Screen capture
- Input event capture
- Display information (resolution, DPI, pixel ratio)

The module automatically selects the appropriate implementation based on
the current platform (darwin, win32, linux).
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Protocol

    class PlatformProvider(Protocol):
        """Protocol for platform-specific providers."""

        @staticmethod
        def get_screen_dimensions() -> tuple[int, int]:
            """Get screen dimensions in physical pixels."""
            ...

        @staticmethod
        def get_display_pixel_ratio() -> float:
            """Get display pixel ratio (physical/logical)."""
            ...

        @staticmethod
        def is_accessibility_enabled() -> bool | None:
            """Check if accessibility permissions are enabled."""
            ...


class DisplayMetricsUnavailable(RuntimeError):
    """A display measurement could not be taken.

    Raised instead of returning a plausible default. A fabricated pixel ratio
    or screen size is written into the recording and is indistinguishable from
    a measured one, so every downstream coordinate is silently reinterpreted
    at the wrong scale while the recording reports success.
    """


def get_platform() -> str:
    """Get the current platform identifier.

    Returns:
        'darwin' for macOS, 'win32' for Windows, 'linux' for Linux.
    """
    return sys.platform


def get_platform_provider() -> "PlatformProvider":
    """Get the platform-specific provider for the current OS.

    Returns:
        Platform provider instance for the current operating system.

    Raises:
        NotImplementedError: If the platform is not supported.
    """
    platform = get_platform()

    if platform == "darwin":
        from openadapt_capture.platform.darwin import DarwinPlatform
        return DarwinPlatform()
    elif platform == "win32":
        from openadapt_capture.platform.windows import WindowsPlatform
        return WindowsPlatform()
    elif platform.startswith("linux"):
        from openadapt_capture.platform.linux import LinuxPlatform
        return LinuxPlatform()
    else:
        raise NotImplementedError(f"Platform not supported: {platform}")


def get_screen_dimensions() -> tuple[int, int]:
    """Get screen dimensions in physical pixels.

    This returns the actual screenshot pixel dimensions, which may be
    larger than logical dimensions on HiDPI/Retina displays.

    Returns:
        Tuple of (width, height) in physical pixels.

    Raises:
        DisplayMetricsUnavailable: If the screen could not be measured. A
            plausible default is never returned: it would be recorded as a
            real measurement.
    """
    try:
        provider = get_platform_provider()
        return provider.get_screen_dimensions()
    except (NotImplementedError, ImportError) as exc:
        # Fallback to generic implementation
        try:
            from PIL import ImageGrab
            screenshot = ImageGrab.grab()
            return screenshot.size
        except Exception as grab_exc:
            raise DisplayMetricsUnavailable(
                f"Could not measure screen dimensions: {exc}; "
                f"the generic screenshot fallback also failed: {grab_exc}"
            ) from grab_exc


def get_display_pixel_ratio() -> float:
    """Get the display pixel ratio (physical/logical).

    This is the ratio of physical pixels to logical pixels.
    For example, 2.0 for Retina displays on macOS.

    Returns:
        Pixel ratio (e.g., 1.0 for standard displays, 2.0 for Retina).

    Raises:
        DisplayMetricsUnavailable: If the ratio could not be measured. `1.0` is
            never substituted: a Retina display whose probe failed would be
            recorded as a standard display and every captured coordinate would
            be reinterpreted at half scale.
    """
    try:
        provider = get_platform_provider()
        return provider.get_display_pixel_ratio()
    except (NotImplementedError, ImportError) as exc:
        raise DisplayMetricsUnavailable(
            f"Could not measure the display pixel ratio: {exc}"
        ) from exc


def is_accessibility_enabled() -> bool | None:
    """Check if accessibility permissions are enabled.

    On macOS, this checks if the application has accessibility permissions
    required for keyboard and mouse event capture.

    Returns:
        True if accessibility is verified enabled, False if it is verified
        disabled, and None if the permission state could not be determined.
        None is not a synonym for True: a caller gating "safe to record" on an
        undetermined state would start a recording that captures no input.
    """
    try:
        provider = get_platform_provider()
        return provider.is_accessibility_enabled()
    except (NotImplementedError, ImportError):
        return None


__all__ = [
    "DisplayMetricsUnavailable",
    "get_platform",
    "get_platform_provider",
    "get_screen_dimensions",
    "get_display_pixel_ratio",
    "is_accessibility_enabled",
]

"""Version-aware macOS permission guidance; never an access decision.

Kept dependency-free in each independently installable package. The shared
conformance cases in tests/test_macos_permission_guidance.py cover all copies.
"""

from __future__ import annotations

import platform
import re


def control_permission_guidance(version: str | None = None) -> dict[str, str]:
    """Describe setup without inferring target access from process AX trust."""
    if version is None:
        try:
            version = platform.mac_ver()[0]
        except (OSError, ValueError, TypeError, RuntimeError):
            version = ""
    valid = re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", version)
    scope = "unknown"
    if valid:
        parts = tuple(int(part) for part in version.split(".")[:2])
        scope = "app_selection" if parts >= (27, 3) else "legacy"
    pane = "x-apple.systempreferences:com.apple.preference.security"
    if scope == "legacy":
        return {
            "setup_mode": scope,
            "label": "Accessibility",
            "settings_url": pane + "?Privacy_Accessibility",
            "instructions": "In System Settings > Privacy & Security > Accessibility, "
            "enable the app that launches OpenAdapt, then restart that app.",
        }
    steps = (
        "In System Settings > Privacy & Security > System & App Protection "
        "Override, click + and add the app that launches OpenAdapt. Choose Apps, "
        "then set each target to Ask First or At Any Time. For unrestricted "
        "control, select the system-protection override option, confirm its "
        "warnings and authenticate. Restart the launching app, then check the "
        "selected target."
    )
    return {
        "setup_mode": scope,
        "label": "System & App Protection Override"
        if scope == "app_selection"
        else "macOS app control",
        # No supported direct link to the new pane has been verified.
        "settings_url": pane,
        "instructions": steps
        if scope == "app_selection"
        else "On macOS 27.3 or later: "
        + steps
        + " On earlier macOS, use Privacy & Security > Accessibility instead.",
    }

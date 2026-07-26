"""Capability-aware execution-surface detection (single source of truth).

Every execution surface Desktop offers (``web`` / ``windows`` / ``macos`` /
``linux`` / ``rdp`` / ``citrix``) is probed against what ``openadapt-flow``
actually requires to drive it, per ``openadapt_flow/backends/factory.py``:

* ``web``      -- Playwright (a core Flow dependency) plus a Chromium build in
                  the ms-playwright cache (``python -m playwright install
                  chromium``; Flow also auto-provisions it on first launch).
* ``windows``  -- driven through the in-guest WAA agent over HTTP.  The local
                  client needs ``requests`` (``pip install
                  'openadapt-flow[windows]'``); on a Windows host the in-guest
                  agent itself needs ``uiautomation`` (which brings comtypes).
* ``macos``    -- pyobjc ApplicationServices/Quartz (``pip install
                  'openadapt-flow[macos]'``) plus the Accessibility and Screen
                  Recording permissions.
* ``linux``    -- PyGObject + the AT-SPI typelib/runtime (``pip install
                  'openadapt-flow[linux]'`` plus gir1.2-atspi-2.0 /
                  at-spi2-core) in an interactive session.
* ``rdp``      -- local client-window capture (macOS Quartz or Win32 hosts
                  only) or network RDP via ``aardwolf`` (``pip install
                  'openadapt-flow[rdp]'``).
* ``citrix``   -- the local-window path pointed at an installed Citrix
                  Workspace app ("Citrix Viewer" on macOS, wfica32 on Windows).

Detection is pure Python, fast, and NEVER raises: every probe is guarded and
an unexpected failure degrades to a conservative, explained state.  The same
:func:`ensure_backend_capability` gate is called by the UI dispatch command,
the CLI, and the record/replay/run entry paths, so remediation text can never
drift between surfaces.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import platform
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

CAPABILITY_SCHEMA = "openadapt-desktop.capability-report/v1"

CapabilityState = Literal[
    "available",
    "driver_required",
    "permission_required",
    "unsupported_host",
]

SURFACES: tuple[str, ...] = ("web", "windows", "macos", "linux", "rdp", "citrix")

_SURFACE_LABELS = {
    "web": "web (browser)",
    "windows": "windows (WAA agent)",
    "macos": "macOS (Accessibility)",
    "linux": "linux (AT-SPI)",
    "rdp": "RDP",
    "citrix": "Citrix",
}

_MAC_ACCESSIBILITY_REMEDIATION = (
    "Open System Settings > Privacy & Security > Accessibility, enable "
    "OpenAdapt Desktop, then restart the app."
)
_MAC_SCREEN_RECORDING_REMEDIATION = (
    "Open System Settings > Privacy & Security > Screen & System Audio "
    "Recording, enable OpenAdapt Desktop, then restart the app."
)
_CHROMIUM_REMEDIATION = (
    "Run: python -m playwright install chromium (Desktop also installs it "
    "automatically when a web recording or replay starts)."
)


@dataclass(frozen=True)
class SurfaceCapability:
    """The detected state of one execution surface on this host.

    Args:
        surface: The backend name (``web`` / ``windows`` / ...).
        state: One of the four capability states.
        detail: What was detected, in plain language.
        remediation: The exact command or settings path that resolves a
            non-available state (``None`` when available).
        requirement: Short phrase naming the missing thing, used in refusal
            messages (``None`` when available).
        driver_name: The dependency this surface hinges on, when one exists.
        driver_version: Its detected version, when detectable.
        blocking: Internal enforcement hint.  ``False`` when the runtime
            auto-provisions the missing piece (Chromium), so record/run may
            proceed while the UI still reports the true state.
    """

    surface: str
    state: CapabilityState
    detail: str
    remediation: str | None = None
    requirement: str | None = None
    driver_name: str | None = None
    driver_version: str | None = None
    blocking: bool = True

    def to_dict(self) -> dict:
        """Render the machine-readable per-surface report entry."""
        driver = None
        if self.driver_name is not None:
            driver = {"name": self.driver_name, "version": self.driver_version}
        return {
            "state": self.state,
            "detail": self.detail,
            "remediation": self.remediation,
            "driver": driver,
        }


class CapabilityError(RuntimeError):
    """A chosen surface is missing a capability; the message is the refusal."""

    def __init__(self, message: str, capability: SurfaceCapability) -> None:
        super().__init__(message)
        self.capability = capability


# --------------------------------------------------------------------- probes


def _find_spec(name: str) -> bool:
    """True when ``name`` is importable; never raises."""
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _dist_version(name: str) -> str | None:
    """Best-effort installed-distribution version; never raises."""
    try:
        return importlib.metadata.version(name)
    except Exception:
        return None


def _playwright_cache_dir() -> Path:
    """The ms-playwright browser cache directory for this host."""
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override and override != "0":
        return Path(override)
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Caches" / "ms-playwright"
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) if local else home / "AppData" / "Local"
        return base / "ms-playwright"
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else home / ".cache"
    return base / "ms-playwright"


def _chromium_installed() -> bool:
    """True when a Playwright Chromium build exists in the driver cache."""
    try:
        cache = _playwright_cache_dir()
        if not cache.is_dir():
            return False
        return any(cache.glob("chromium-*")) or any(cache.glob("chromium_headless_shell-*"))
    except Exception:
        return False


def _mac_accessibility_trusted() -> bool | None:
    """macOS Accessibility grant; ``None`` when the API is unavailable."""
    try:
        from ApplicationServices import AXIsProcessTrusted

        return bool(AXIsProcessTrusted())
    except Exception:
        return None


def _mac_screen_recording_granted() -> bool | None:
    """macOS Screen Recording grant; ``None`` when the API is unavailable."""
    try:
        import Quartz

        preflight = getattr(Quartz, "CGPreflightScreenCaptureAccess", None)
        if preflight is None:
            return None
        return bool(preflight())
    except Exception:
        return None


def _citrix_workspace_installed() -> tuple[bool, str | None]:
    """Whether the Citrix Workspace client app is installed, with its version."""
    try:
        if sys.platform == "darwin":
            for base in (Path("/Applications"), Path.home() / "Applications"):
                app = base / "Citrix Workspace.app"
                if app.is_dir():
                    return True, _mac_app_version(app)
            return False, None
        if sys.platform == "win32":
            for env in ("ProgramFiles(x86)", "ProgramFiles"):
                root = os.environ.get(env)
                if root and (Path(root) / "Citrix" / "ICA Client").is_dir():
                    return True, None
            return False, None
        return False, None
    except Exception:
        return False, None


def _mac_app_version(app: Path) -> str | None:
    """Read CFBundleShortVersionString from an app bundle; never raises."""
    try:
        import plistlib

        with (app / "Contents" / "Info.plist").open("rb") as fh:
            return str(plistlib.load(fh).get("CFBundleShortVersionString") or "") or None
    except Exception:
        return None


# ----------------------------------------------------------- surface checks


def _check_web() -> SurfaceCapability:
    if not _find_spec("playwright"):
        return SurfaceCapability(
            surface="web",
            state="driver_required",
            detail="Playwright (a core openadapt-flow dependency) is not importable.",
            remediation=(
                "Run: pip install openadapt-flow, then: python -m playwright install chromium."
            ),
            requirement="the Playwright browser driver",
            driver_name="playwright",
            driver_version=None,
        )
    version = _dist_version("playwright")
    if not _chromium_installed():
        return SurfaceCapability(
            surface="web",
            state="driver_required",
            detail=(
                "Playwright is installed but no Chromium build was found in the "
                "ms-playwright cache."
            ),
            remediation=_CHROMIUM_REMEDIATION,
            requirement="the Playwright Chromium browser build",
            driver_name="playwright",
            driver_version=version,
            blocking=False,
        )
    return SurfaceCapability(
        surface="web",
        state="available",
        detail="Playwright with a local Chromium build is ready.",
        driver_name="playwright",
        driver_version=version,
    )


def _check_windows() -> SurfaceCapability:
    if sys.platform == "win32":
        if not _find_spec("uiautomation"):
            return SurfaceCapability(
                surface="windows",
                state="driver_required",
                detail=(
                    "The WAA in-guest agent drives Windows through UI Automation, "
                    "and the uiautomation package is not importable on this host."
                ),
                remediation=(
                    "Run: pip install uiautomation (installs comtypes), then start "
                    "the WAA agent from openadapt-flow (openadapt_flow.backends.win_agent)."
                ),
                requirement="the uiautomation (UI Automation) driver",
                driver_name="uiautomation",
                driver_version=None,
            )
        return SurfaceCapability(
            surface="windows",
            state="available",
            detail=(
                "UI Automation (uiautomation) is importable; run the WAA agent "
                "on this host and point Desktop at its URL "
                "(for example http://localhost:5001)."
            ),
            driver_name="uiautomation",
            driver_version=_dist_version("uiautomation"),
        )
    if not _find_spec("requests"):
        return SurfaceCapability(
            surface="windows",
            state="driver_required",
            detail=(
                "The windows surface is driven through the in-guest WAA agent; "
                "the local requests HTTP client it needs is not importable."
            ),
            remediation="Run: pip install 'openadapt-flow[windows]'.",
            requirement="the requests HTTP client for the WAA agent",
            driver_name="requests",
            driver_version=None,
        )
    return SurfaceCapability(
        surface="windows",
        state="available",
        detail=(
            "The windows surface is driven through the in-guest WAA agent; "
            "nothing runs locally beyond the HTTP client. Provide the agent "
            "URL (for example http://localhost:5001 or your approved tunnel)."
        ),
        driver_name="requests",
        driver_version=_dist_version("requests"),
    )


def _check_macos() -> SurfaceCapability:
    if sys.platform != "darwin":
        return SurfaceCapability(
            surface="macos",
            state="unsupported_host",
            detail=(
                f"The macOS Accessibility (AX) surface cannot exist on "
                f"{platform.system() or sys.platform}."
            ),
            remediation=(
                "Run OpenAdapt Desktop on the Mac that owns the target window, "
                "or drive the remote Mac's client window through the RDP surface."
            ),
            requirement="a macOS host",
        )
    if not _find_spec("ApplicationServices"):
        return SurfaceCapability(
            surface="macos",
            state="driver_required",
            detail=(
                "The pyobjc ApplicationServices/Quartz frameworks used for the "
                "AX tree and window capture are not importable."
            ),
            remediation="Run: pip install 'openadapt-flow[macos]'.",
            requirement="the pyobjc ApplicationServices/Quartz frameworks",
            driver_name="pyobjc-framework-applicationservices",
            driver_version=None,
        )
    driver_version = _dist_version("pyobjc-framework-applicationservices")
    if _mac_accessibility_trusted() is False:
        return SurfaceCapability(
            surface="macos",
            state="permission_required",
            detail=(
                "macOS has not granted this app the Accessibility permission "
                "(AXIsProcessTrusted is false), so the AX tree cannot be read."
            ),
            remediation=_MAC_ACCESSIBILITY_REMEDIATION,
            requirement="the macOS Accessibility permission",
            driver_name="pyobjc-framework-applicationservices",
            driver_version=driver_version,
        )
    if _mac_screen_recording_granted() is False:
        return SurfaceCapability(
            surface="macos",
            state="permission_required",
            detail=(
                "macOS has not granted this app the Screen Recording permission, "
                "so window capture for verification cannot run."
            ),
            remediation=_MAC_SCREEN_RECORDING_REMEDIATION,
            requirement="the macOS Screen Recording permission",
            driver_name="pyobjc-framework-applicationservices",
            driver_version=driver_version,
        )
    return SurfaceCapability(
        surface="macos",
        state="available",
        detail=(
            "Accessibility and Screen Recording are granted; the native AX "
            "surface is ready."
        ),
        driver_name="pyobjc-framework-applicationservices",
        driver_version=driver_version,
    )


def _check_linux() -> SurfaceCapability:
    if not sys.platform.startswith("linux"):
        return SurfaceCapability(
            surface="linux",
            state="unsupported_host",
            detail=(
                f"The Linux AT-SPI surface cannot exist on "
                f"{platform.system() or sys.platform}."
            ),
            remediation=(
                "Run OpenAdapt Desktop on the Linux host that owns the target "
                "window, or drive it remotely through the RDP surface."
            ),
            requirement="a Linux host",
        )
    if not _find_spec("gi"):
        return SurfaceCapability(
            surface="linux",
            state="driver_required",
            detail="PyGObject (gi) for the AT-SPI bindings is not importable.",
            remediation=(
                "Run: pip install 'openadapt-flow[linux]' and install the system "
                "AT-SPI runtime (Debian/Ubuntu: sudo apt install python3-gi "
                "gir1.2-atspi-2.0 at-spi2-core)."
            ),
            requirement="the PyGObject AT-SPI bindings",
            driver_name="PyGObject",
            driver_version=None,
        )
    if not _atspi_typelib_available():
        return SurfaceCapability(
            surface="linux",
            state="driver_required",
            detail="PyGObject is installed but the Atspi 2.0 typelib is missing.",
            remediation=(
                "Install the AT-SPI runtime (Debian/Ubuntu: sudo apt install "
                "gir1.2-atspi-2.0 at-spi2-core), then run inside an interactive "
                "session with accessibility enabled."
            ),
            requirement="the AT-SPI 2.0 typelib and runtime",
            driver_name="PyGObject",
            driver_version=_dist_version("PyGObject"),
        )
    return SurfaceCapability(
        surface="linux",
        state="available",
        detail=(
            "PyGObject with the Atspi 2.0 typelib is ready; run inside an "
            "interactive session with accessibility (the AT-SPI bus) enabled."
        ),
        driver_name="PyGObject",
        driver_version=_dist_version("PyGObject"),
    )


def _atspi_typelib_available() -> bool:
    """True when ``gi.require_version("Atspi", "2.0")`` succeeds; never raises."""
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        return True
    except Exception:
        return False


def _rdp_network_note() -> str:
    aardwolf_version = _dist_version("aardwolf")
    if _find_spec("aardwolf"):
        return f"Network RDP (aardwolf {aardwolf_version or 'unknown version'}) is also ready."
    return "Network RDP additionally needs: pip install 'openadapt-flow[rdp]' (aardwolf)."


def _check_rdp() -> SurfaceCapability:
    aardwolf_version = _dist_version("aardwolf")
    if sys.platform == "darwin":
        if not _find_spec("Quartz"):
            return SurfaceCapability(
                surface="rdp",
                state="driver_required",
                detail=(
                    "The pyobjc Quartz framework that captures the local remote "
                    "desktop client window is not importable."
                ),
                remediation="Run: pip install 'openadapt-flow[macos]'.",
                requirement="the pyobjc Quartz window-capture framework",
                driver_name="pyobjc-framework-quartz",
                driver_version=None,
            )
        if _mac_screen_recording_granted() is False:
            return SurfaceCapability(
                surface="rdp",
                state="permission_required",
                detail=(
                    "macOS has not granted this app the Screen Recording "
                    "permission, so the remote desktop client window cannot be "
                    "captured."
                ),
                remediation=_MAC_SCREEN_RECORDING_REMEDIATION,
                requirement="the macOS Screen Recording permission",
                driver_name="aardwolf",
                driver_version=aardwolf_version,
            )
        return SurfaceCapability(
            surface="rdp",
            state="available",
            detail=(
                "Local client-window capture (Quartz) is ready. " + _rdp_network_note()
            ),
            driver_name="aardwolf",
            driver_version=aardwolf_version,
        )
    if sys.platform == "win32":
        return SurfaceCapability(
            surface="rdp",
            state="available",
            detail=(
                "Local client-window capture (Win32) is ready. " + _rdp_network_note()
            ),
            driver_name="aardwolf",
            driver_version=aardwolf_version,
        )
    if not _find_spec("aardwolf"):
        return SurfaceCapability(
            surface="rdp",
            state="driver_required",
            detail=(
                "The local client-window path runs on macOS and Windows hosts "
                "only; on this host RDP needs the aardwolf network client, "
                "which is not importable."
            ),
            remediation="Run: pip install 'openadapt-flow[rdp]'.",
            requirement="the aardwolf network RDP client",
            driver_name="aardwolf",
            driver_version=None,
        )
    return SurfaceCapability(
        surface="rdp",
        state="available",
        detail=(
            "Network RDP (aardwolf) is ready. The local client-window path "
            "runs on macOS and Windows hosts only."
        ),
        driver_name="aardwolf",
        driver_version=aardwolf_version,
    )


def _check_citrix() -> SurfaceCapability:
    if sys.platform not in ("darwin", "win32"):
        return SurfaceCapability(
            surface="citrix",
            state="unsupported_host",
            detail=(
                "Citrix Workspace window replay requires the native macOS "
                "(Quartz) or Windows (Win32) window client; on "
                f"{platform.system() or sys.platform} Flow requires an "
                "injected window client."
            ),
            remediation=(
                "Drive the Citrix Workspace session window from a macOS or "
                "Windows host."
            ),
            requirement="a macOS or Windows host",
        )
    if sys.platform == "darwin" and not _find_spec("Quartz"):
        return SurfaceCapability(
            surface="citrix",
            state="driver_required",
            detail=(
                "The pyobjc Quartz framework that captures the Citrix "
                "Workspace window is not importable."
            ),
            remediation="Run: pip install 'openadapt-flow[macos]'.",
            requirement="the pyobjc Quartz window-capture framework",
            driver_name="pyobjc-framework-quartz",
            driver_version=None,
        )
    installed, app_version = _citrix_workspace_installed()
    if not installed:
        owner = "'Citrix Viewer'" if sys.platform == "darwin" else "wfica32"
        return SurfaceCapability(
            surface="citrix",
            state="driver_required",
            detail="No Citrix Workspace client app was found on this host.",
            remediation=(
                "Install the Citrix Workspace app from citrix.com, sign in, and "
                f"open the session; OpenAdapt targets the {owner} window."
            ),
            requirement="the Citrix Workspace app",
            driver_name="Citrix Workspace",
            driver_version=None,
        )
    if sys.platform == "darwin" and _mac_screen_recording_granted() is False:
        return SurfaceCapability(
            surface="citrix",
            state="permission_required",
            detail=(
                "macOS has not granted this app the Screen Recording "
                "permission, so the Citrix Workspace window cannot be captured."
            ),
            remediation=_MAC_SCREEN_RECORDING_REMEDIATION,
            requirement="the macOS Screen Recording permission",
            driver_name="Citrix Workspace",
            driver_version=app_version,
        )
    owner = "'Citrix Viewer'" if sys.platform == "darwin" else "wfica32"
    return SurfaceCapability(
        surface="citrix",
        state="available",
        detail=(
            "Citrix Workspace is installed; OpenAdapt drives the already-open "
            f"{owner} session window."
        ),
        driver_name="Citrix Workspace",
        driver_version=app_version,
    )


_CHECKS = {
    "web": _check_web,
    "windows": _check_windows,
    "macos": _check_macos,
    "linux": _check_linux,
    "rdp": _check_rdp,
    "citrix": _check_citrix,
}


# ------------------------------------------------------------------- report


def detect_capability(surface: str) -> SurfaceCapability:
    """Detect one surface's capability state; never raises."""
    check = _CHECKS.get(surface)
    if check is None:
        return SurfaceCapability(
            surface=surface,
            state="unsupported_host",
            detail=f"Unknown execution surface {surface!r}.",
            remediation="Choose one of: " + ", ".join(SURFACES) + ".",
            requirement="a known execution surface",
        )
    try:
        return check()
    except Exception as exc:  # defensive: probes are guarded, but never raise
        return SurfaceCapability(
            surface=surface,
            state="driver_required",
            detail=f"Capability detection failed unexpectedly: {type(exc).__name__}.",
            remediation="Run: openadapt-desktop doctor for a full dependency check.",
            requirement="a working capability probe",
        )


def detect_capabilities() -> dict[str, SurfaceCapability]:
    """Detect every surface's capability state; never raises."""
    return {surface: detect_capability(surface) for surface in SURFACES}


def capability_report() -> dict:
    """The machine-readable capability report (schema v1); never raises."""
    from engine import __version__

    try:
        os_version = platform.mac_ver()[0] if sys.platform == "darwin" else platform.release()
    except Exception:
        os_version = ""
    return {
        "schema": CAPABILITY_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "host": {
            "os": platform.system() or sys.platform,
            "os_version": os_version or "",
            "arch": platform.machine() or "",
            "app_version": __version__,
        },
        "surfaces": {
            surface: capability.to_dict()
            for surface, capability in detect_capabilities().items()
        },
    }


# -------------------------------------------------------------- enforcement


def refusal_message(action: str, capability: SurfaceCapability) -> str:
    """Render the canonical refusal wording for a missing capability."""
    label = _SURFACE_LABELS.get(capability.surface, capability.surface)
    requirement = capability.requirement or "a missing capability"
    remediation = capability.remediation or capability.detail
    return f"{action} refused: {label} needs {requirement}. {remediation}"


def ensure_backend_capability(backend: str, *, action: str = "record") -> SurfaceCapability:
    """Fail fast, with precise remediation, when a chosen surface cannot run.

    Args:
        backend: The chosen ``TargetBackend`` name.
        action: The verb for the refusal message (``record`` / ``replay`` /
            ``run``).

    Returns:
        The detected capability when the surface can proceed.  A non-blocking
        gap (Chromium, which Flow auto-provisions on launch) passes through so
        the runtime's own installer can resolve it.

    Raises:
        CapabilityError: When the capability is missing; the message follows
            the pattern ``"<action> refused: <surface> needs <thing>.
            <exact remediation>"``.
    """
    capability = detect_capability(backend)
    if capability.state != "available" and capability.blocking:
        raise CapabilityError(refusal_message(action, capability), capability)
    return capability

"""Native OpenAdapt Desktop discovery and launch helpers."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

DESKTOP_APP_NAME = "OpenAdapt Desktop"
DESKTOP_BUNDLE_IDENTIFIER = "ai.openadapt.desktop"
LINUX_DESKTOP_IDENTITY = DESKTOP_APP_NAME
LINUX_NATIVE_EXECUTABLE = Path("/usr/bin/openadapt-desktop")


class DesktopLaunchError(RuntimeError):
    """The installed native Desktop application could not be launched."""


def _run_launcher(
    command: list[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess],
) -> bool:
    """Run an OS launcher and return whether it accepted the application."""
    try:
        result = runner(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _spawn_executable(
    executable: Path,
    *,
    spawner: Callable[..., subprocess.Popen],
) -> bool:
    """Start an exact native executable path without resolving a CLI name."""
    try:
        spawner(
            [str(executable)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return True


def _windows_candidates(environment: Mapping[str, str]) -> tuple[Path, ...]:
    """Return the native paths produced by Desktop's MSI and NSIS installers."""
    candidates: list[Path] = []
    for variable in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
        root = environment.get(variable)
        if root:
            candidates.append(Path(root) / DESKTOP_APP_NAME / "openadapt-desktop.exe")
    return tuple(candidates)


def launch_native_desktop(
    *,
    platform: str | None = None,
    environment: Mapping[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    spawner: Callable[..., subprocess.Popen] = subprocess.Popen,
    finder: Callable[[str], str | None] = shutil.which,
    is_file: Callable[[Path], bool] = Path.is_file,
) -> None:
    """Launch the installed native Desktop application for the current OS.

    The function never resolves the Python ``openadapt-desktop`` console script.
    It uses the macOS bundle identifier, a registered Linux desktop identity,
    or an exact path produced by a Windows native installer.
    """
    platform = platform or sys.platform
    if environment is None:
        environment = os.environ

    if platform == "darwin":
        if _run_launcher(
            ["open", "-b", DESKTOP_BUNDLE_IDENTIFIER],
            runner=runner,
        ):
            return
        raise DesktopLaunchError(
            f"macOS could not open {DESKTOP_APP_NAME} ({DESKTOP_BUNDLE_IDENTIFIER})."
        )

    if platform == "win32":
        for executable in _windows_candidates(environment):
            if is_file(executable) and _spawn_executable(
                executable,
                spawner=spawner,
            ):
                return
        raise DesktopLaunchError(
            f"Windows could not find an installed {DESKTOP_APP_NAME} application."
        )

    if platform.startswith("linux"):
        gtk_launch = finder("gtk-launch")
        if gtk_launch and _run_launcher(
            [gtk_launch, LINUX_DESKTOP_IDENTITY],
            runner=runner,
        ):
            return

        if is_file(LINUX_NATIVE_EXECUTABLE) and _spawn_executable(
            LINUX_NATIVE_EXECUTABLE,
            spawner=spawner,
        ):
            return

        appimage = environment.get("OPENADAPT_DESKTOP_APPIMAGE")
        if appimage:
            executable = Path(appimage).expanduser()
            if is_file(executable) and _spawn_executable(
                executable,
                spawner=spawner,
            ):
                return

        raise DesktopLaunchError(
            f"Linux could not open the registered {DESKTOP_APP_NAME} application."
        )

    raise DesktopLaunchError(f"Desktop launch is not supported on {platform}.")

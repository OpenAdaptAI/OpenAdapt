"""Tests for launching the installed native Desktop application."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from openadapt_tray.desktop import (
    DESKTOP_BUNDLE_IDENTIFIER,
    LINUX_NATIVE_EXECUTABLE,
    DesktopLaunchError,
    launch_native_desktop,
)


def _result(returncode: int) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode)


def test_macos_launches_by_bundle_identifier() -> None:
    runner = MagicMock(return_value=_result(0))

    launch_native_desktop(platform="darwin", runner=runner)

    assert runner.call_args.args[0] == [
        "open",
        "-b",
        DESKTOP_BUNDLE_IDENTIFIER,
    ]


def test_macos_launcher_failure_is_visible() -> None:
    runner = MagicMock(return_value=_result(1))

    with pytest.raises(DesktopLaunchError, match="macOS could not open"):
        launch_native_desktop(platform="darwin", runner=runner)


def test_windows_launches_an_exact_native_installer_path() -> None:
    spawner = MagicMock()
    expected = (
        Path(r"C:\Users\person\AppData\Local")
        / "OpenAdapt Desktop"
        / "openadapt-desktop.exe"
    )

    launch_native_desktop(
        platform="win32",
        environment={"LOCALAPPDATA": r"C:\Users\person\AppData\Local"},
        spawner=spawner,
        is_file=lambda path: path == expected,
    )

    assert spawner.call_args.args[0] == [str(expected)]


def test_windows_never_falls_back_to_the_python_console_script() -> None:
    with pytest.raises(DesktopLaunchError, match="Windows could not find"):
        launch_native_desktop(
            platform="win32",
            environment={},
            is_file=lambda _path: False,
        )


def test_linux_launches_by_registered_desktop_identity() -> None:
    runner = MagicMock(return_value=_result(0))

    launch_native_desktop(
        platform="linux",
        environment={},
        runner=runner,
        finder=lambda name: "/usr/bin/gtk-launch" if name == "gtk-launch" else None,
    )

    assert runner.call_args.args[0] == [
        "/usr/bin/gtk-launch",
        "OpenAdapt Desktop",
    ]


def test_linux_can_launch_the_exact_deb_executable() -> None:
    spawner = MagicMock()

    launch_native_desktop(
        platform="linux",
        environment={},
        spawner=spawner,
        finder=lambda _name: None,
        is_file=lambda path: path == LINUX_NATIVE_EXECUTABLE,
    )

    assert spawner.call_args.args[0] == [str(LINUX_NATIVE_EXECUTABLE)]


def test_linux_can_launch_an_explicit_native_appimage() -> None:
    spawner = MagicMock()
    appimage = Path("/opt/OpenAdapt Desktop.AppImage")

    launch_native_desktop(
        platform="linux",
        environment={"OPENADAPT_DESKTOP_APPIMAGE": str(appimage)},
        spawner=spawner,
        finder=lambda _name: None,
        is_file=lambda path: path == appimage,
    )

    assert spawner.call_args.args[0] == [str(appimage)]


def test_unknown_platform_does_not_use_linux_launchers() -> None:
    runner = MagicMock()

    with pytest.raises(DesktopLaunchError, match="not supported on freebsd"):
        launch_native_desktop(
            platform="freebsd",
            environment={},
            runner=runner,
            finder=lambda _name: "/usr/bin/gtk-launch",
        )

    runner.assert_not_called()

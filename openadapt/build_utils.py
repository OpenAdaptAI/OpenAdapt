"""Utility functions for the build process."""

import os
import pathlib
import sys


def get_path_to_preferences_folder(
    current_path: pathlib.Path = None,
) -> pathlib.Path:
    """Get the path to the preferences folder."""
    if not is_running_from_executable():
        return current_path
    if sys.platform == "darwin":
        # if macos, get the path to the /Users/username/Library/Preferences
        # and set the path for all user preferences
        path = pathlib.Path.home() / "Library" / "Preferences" / "openadapt"
        if not path.exists():
            path.mkdir()
        return path
    else:
        # if windows, get the path to the %APPDATA% directory and set the path
        # for all user preferences
        path = pathlib.Path.home() / "AppData" / "Roaming" / "openadapt"
        if not path.exists():
            path.mkdir()
        return path


def is_running_from_executable() -> bool:
    """Check if the script is running from an executable."""
    return getattr(sys, "frozen", False)


def override_stdout_stderr() -> object:
    """Override stdout and stderr to /dev/null when running from an executable."""

    class StdoutStderrOverride:
        def __init__(self) -> None:
            self.stdout = None
            self.stderr = None

        def __enter__(self) -> None:
            if is_running_from_executable():
                self.old_stdout = sys.stdout
                self.old_stderr = sys.stderr
                sys.stdout = open(os.devnull, "a")
                sys.stderr = open(os.devnull, "a")

        def __exit__(self, *args: tuple, **kwargs: dict) -> None:
            if is_running_from_executable():
                sys.stdout.close()
                sys.stderr.close()
                sys.stdout = self.old_stdout
                sys.stderr = self.old_stderr

    return StdoutStderrOverride()

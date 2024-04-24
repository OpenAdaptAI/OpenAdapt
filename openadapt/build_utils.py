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
            path.mkdir(parents=True, exist_ok=True)
        return path
    else:
        # if windows, get the path to the %APPDATA% directory and set the path
        # for all user preferences
        path = pathlib.Path.home() / "AppData" / "Roaming" / "openadapt"
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
        return path


def is_running_from_executable() -> bool:
    """Check if the script is running from an executable."""
    return getattr(sys, "frozen", False)


class RedirectOutput:
    """Context manager to redirect stdout and stderr to /dev/null."""

    def __enter__(self) -> "RedirectOutput":
        """Redirect stdout and stderr to /dev/null."""
        if is_running_from_executable():
            self.old_stdout = sys.stdout
            self.old_stderr = sys.stderr
            null_stream = open(os.devnull, "a")
            sys.stdout = sys.stderr = null_stream
            return self

    def __exit__(self, exc_type: type, exc_value: Exception, traceback: type) -> None:
        """Restore stdout and stderr."""
        if is_running_from_executable():
            sys.stdout.close()
            sys.stderr.close()
            sys.stdout = self.old_stdout
            sys.stderr = self.old_stderr


def redirect_stdout_stderr() -> RedirectOutput:
    """Get the RedirectOutput instance for use as a context manager."""
    return RedirectOutput()

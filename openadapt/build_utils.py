"""Utility functions for the build process."""

import importlib.metadata
import pathlib
import sys
import time
import shutil


def set_permissions(path):
    for root, dirs, files in os.walk(path):
        for dir in dirs:
            os.chmod(os.path.join(root, dir), 0o755)
        for file in files:
            os.chmod(os.path.join(root, file), 0o755)


def unzip_file(file_path: str) -> None:
    if os.path.exists(file_path):
        shutil.unpack_archive(file_path, os.path.dirname(file_path))
        set_permissions(os.path.dirname(file_path))
        print("Unzipped")


def get_root_dir_path() -> pathlib.Path:
    """Get the path to the project root directory."""
    if not is_running_from_executable():
        return pathlib.Path(__file__).parent
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
            log_file_path = get_log_file_path()
            log_stream = open(log_file_path, "a")
            self.old_stdout = sys.stdout
            self.old_stderr = sys.stderr
            sys.stdout = sys.stderr = log_stream
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


def get_log_file_path() -> str:
    """Get the path to the log file.

    Returns:
        str: The path to the log file.
    """
    version = importlib.metadata.version("openadapt")
    date = time.strftime("%Y-%m-%d")
    path = get_root_dir_path() / "data" / "logs" / version / date / "openadapt.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)

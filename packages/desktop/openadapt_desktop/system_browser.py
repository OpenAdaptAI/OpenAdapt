"""Hand web URLs to the OS default handler without forking the engine.

The Desktop engine may already have threads when sign-in opens a browser.
Python's ``webbrowser`` controllers may fork it (including macOS osascript).
Use the same no-fork child-process boundary as other engine launchers. Windows
uses ShellExecute through ``startfile``. No shell or BROWSER command is parsed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from urllib.parse import urlsplit

from openadapt_desktop import child_process

LAUNCH_TIMEOUT_S = 10.0


def _command() -> list[str] | None:
    if sys.platform == "darwin":
        return ["/usr/bin/open", "--"] if os.access("/usr/bin/open", os.X_OK) else None
    if sys.platform.startswith("linux"):
        program = shutil.which("xdg-open")
        return [os.path.abspath(program)] if program else None
    return None


def is_available() -> bool:
    """Check for a handler entry point without probing or opening a GUI."""
    if sys.platform == "win32":
        return callable(getattr(os, "startfile", None))
    return _command() is not None


def open_url(url: str) -> None:
    """Open a web URL; the caller must still enforce its destination policy.

    Successful dispatch is not proof that the browser or login succeeded.
    Neither launcher output nor its exception may disclose the login URL.
    """
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname is not None
            and parsed.username is None
            and parsed.password is None
            and not any(ord(char) <= 32 or ord(char) == 127 for char in url)
            and "\\" not in url
        )
        # Force port validation before any OS handoff.
        parsed.port
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise RuntimeError("Refusing to open an invalid web URL.")
    try:
        if sys.platform == "win32":
            startfile = getattr(os, "startfile", None)
            if startfile is None:
                raise OSError("system handler unavailable")
            startfile(url)
        else:
            command = _command()
            if command is None:
                raise OSError("system handler unavailable")
            child_process.run(
                [*command, url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=LAUNCH_TIMEOUT_S,
                check=True,
            )
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("OpenAdapt could not open the system browser.") from None

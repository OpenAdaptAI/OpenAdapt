"""Make a Chromium browser's page readable through the OS accessibility tree.

A recording of the person's everyday browser reads each clicked element
(role, accessible name) through the same OS accessibility API it uses for
native apps. Chromium builds its page tree only when it believes an
assistive tool is present. Measured on real Chrome
(benchmark/multi_surface_record/results/chrome-accessibility-probe-*):

- Windows: the UI Automation point query the recorder already makes turns
  it on. Nothing to do.
- macOS: setting ``AXEnhancedUserInterface`` on the browser's application
  element turns it on (``AXManualAccessibility`` is unsupported). The
  recorder sets it while recording and restores the previous value after.
- Linux: no runtime switch was found. Chrome must start with
  ``--force-renderer-accessibility``. :func:`enable_linux` adds that flag
  to the user's own launcher entries (``~/.local/share/applications``),
  never to system files, and :func:`disable_linux` removes exactly what it
  added. It takes effect the next time the browser starts.
"""

from __future__ import annotations

import configparser
import contextlib
import shutil
import sys
import threading
from pathlib import Path
from typing import Iterator, Optional

LINUX_FLAG = "--force-renderer-accessibility"
#: Marks a launcher entry this module wrote, so undo removes only those.
MARKER_KEY = "X-OpenAdapt-Accessibility"

#: Launcher entries of Chromium-family browsers, by file name.
LINUX_DESKTOP_FILES = (
    "google-chrome.desktop",
    "google-chrome-beta.desktop",
    "chromium.desktop",
    "chromium-browser.desktop",
    "microsoft-edge.desktop",
    "brave-browser.desktop",
)

#: macOS application names of Chromium-family browsers.
MACOS_BROWSER_APPS = frozenset(
    {
        "Google Chrome",
        "Google Chrome for Testing",
        "Chromium",
        "Microsoft Edge",
        "Brave Browser",
    }
)

_SYSTEM_DIRS = (
    Path("/usr/share/applications"),
    Path("/var/lib/flatpak/exports/share/applications"),
)


def _user_dir() -> Path:
    return Path.home() / ".local" / "share" / "applications"


def _with_flag(exec_line: str) -> str:
    if LINUX_FLAG in exec_line.split():
        return exec_line
    parts = exec_line.split(" ", 1)
    return parts[0] + " " + LINUX_FLAG + ((" " + parts[1]) if len(parts) > 1 else "")


def enable_linux(
    system_dirs: tuple[Path, ...] = _SYSTEM_DIRS, user_dir: Optional[Path] = None
) -> list[str]:
    """Write user launcher overrides that start each browser with the flag.

    Returns one line per change, naming the file. An entry the person
    already overrides in their own folder is left alone unless it is ours.
    """

    target = user_dir or _user_dir()
    changes: list[str] = []
    for name in LINUX_DESKTOP_FILES:
        source = next((d / name for d in system_dirs if (d / name).is_file()), None)
        if source is None:
            continue
        dest = target / name
        if dest.exists() and MARKER_KEY not in dest.read_text(errors="replace"):
            changes.append(f"left {dest} alone: you already override it")
            continue
        lines = []
        for line in source.read_text(errors="replace").splitlines():
            lines.append(
                "Exec=" + _with_flag(line[5:]) if line.startswith("Exec=") else line
            )
        text = "\n".join(lines) + "\n"
        # Mark the main section so undo can tell this file from the person's.
        text = text.replace(
            "[Desktop Entry]\n", f"[Desktop Entry]\n{MARKER_KEY}=true\n", 1
        )
        target.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
        changes.append(f"wrote {dest} (starts {source.stem} with {LINUX_FLAG})")
    return changes


def disable_linux(user_dir: Optional[Path] = None) -> list[str]:
    """Remove the launcher overrides :func:`enable_linux` wrote."""

    target = user_dir or _user_dir()
    removed = []
    for name in LINUX_DESKTOP_FILES:
        dest = target / name
        if dest.is_file() and f"{MARKER_KEY}=true" in dest.read_text(errors="replace"):
            dest.unlink()
            removed.append(f"removed {dest}")
    return removed


def linux_status(user_dir: Optional[Path] = None) -> list[str]:
    target = user_dir or _user_dir()
    out = []
    for name in LINUX_DESKTOP_FILES:
        dest = target / name
        if dest.is_file():
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                parser.read_string(dest.read_text(errors="replace"))
                exec_line = parser.get("Desktop Entry", "Exec", fallback="")
            except configparser.Error:
                exec_line = ""
            on = LINUX_FLAG in exec_line
            out.append(f"{dest}: {'on' if on else 'off'}")
    return out or ["no browser launcher overrides in " + str(target)]


@contextlib.contextmanager
def readable_browsers_while_recording(interval_s: float = 2.0) -> Iterator[None]:
    """On macOS, keep running Chromium browsers' pages readable while recording.

    Sets ``AXEnhancedUserInterface`` on each running Chromium-family app
    (also ones started during the recording) and restores each app's
    previous value at the end. Elsewhere it does nothing.
    """

    if sys.platform != "darwin":
        yield
        return
    try:
        import ApplicationServices as AX
        from AppKit import NSWorkspace
    except ImportError:
        yield
        return

    touched: dict[int, object] = {}
    stop = threading.Event()

    def sweep() -> None:
        for app in NSWorkspace.sharedWorkspace().runningApplications():
            name = app.localizedName()
            pid = int(app.processIdentifier())
            if name not in MACOS_BROWSER_APPS or pid in touched:
                continue
            element = AX.AXUIElementCreateApplication(pid)
            err, before = AX.AXUIElementCopyAttributeValue(
                element, "AXEnhancedUserInterface", None
            )
            touched[pid] = bool(before) if err == 0 else False
            # The call can report an error and still take effect; the
            # recorder's own point query is what proves the page is readable.
            AX.AXUIElementSetAttributeValue(element, "AXEnhancedUserInterface", True)

    def loop() -> None:
        while not stop.is_set():
            try:
                sweep()
            except Exception:
                pass
            stop.wait(interval_s)

    sweep()
    thread = threading.Thread(target=loop, name="browser-accessibility", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=5)
        for pid, before in touched.items():
            try:
                AX.AXUIElementSetAttributeValue(
                    AX.AXUIElementCreateApplication(pid),
                    "AXEnhancedUserInterface",
                    before,
                )
            except Exception:
                pass


def which_browser() -> Optional[str]:
    for name in (
        "google-chrome",
        "chromium",
        "chromium-browser",
        "microsoft-edge",
        "brave-browser",
    ):
        if shutil.which(name):
            return name
    return None

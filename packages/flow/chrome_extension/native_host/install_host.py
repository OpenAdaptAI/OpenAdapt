#!/usr/bin/env python3
"""Register the OpenAdapt extension's native-messaging host with Chrome.

Part of OpenAdapt#1872. Chrome starts a native host only when a manifest named
``com.openadapt.extension.json`` sits in a directory it searches and that
manifest's ``allowed_origins`` names the calling extension. This script writes
that manifest plus a small launcher that runs ``openadapt_extension_host.py``
with this Python, so an unpacked extension can reach the host without hand
editing.

Fail-closed rules:

- ``--extension-id`` is required and must be a real 32-letter (``a``-``p``)
  id. The manifest allows exactly that one origin; there is no wildcard.
- The launcher passes ``--recording`` only when one is given. Without it the
  host still refuses (``no_recording_dir``) unless the environment names one.
- macOS and Linux only. Windows registers hosts in the registry; that stays a
  manual step (see docs/CHROME_EXTENSION_NATIVE_HOST.md).

Targets: ``--user-data-dir DIR`` registers for one Chromium profile directory
(``DIR/NativeMessagingHosts``, what the tests use), or ``--browser
chrome|chromium`` registers for the current user's default browser directory.
No network, no remote endpoints, no store listing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Optional

HOST_NAME = "com.openadapt.extension"
MANIFEST_FILE = f"{HOST_NAME}.json"
LAUNCHER_FILE = f"{HOST_NAME}-host.sh"
HOST_SCRIPT = Path(__file__).resolve().parent / "openadapt_extension_host.py"
# Matched with ``fullmatch``: ``$`` alone would also accept a trailing newline.
EXTENSION_ID = re.compile(r"[a-p]{32}")

_USER_DIRS = {
    ("darwin", "chrome"): ("Library", "Application Support", "Google", "Chrome"),
    ("darwin", "chromium"): ("Library", "Application Support", "Chromium"),
    ("linux", "chrome"): (".config", "google-chrome"),
    ("linux", "chromium"): (".config", "chromium"),
}


def _platform_key(platform: str) -> str:
    if platform == "darwin":
        return "darwin"
    if platform.startswith("linux"):
        return "linux"
    raise RuntimeError(
        f"native host registration is not automated on {platform!r}; "
        "Windows uses the registry (see docs/CHROME_EXTENSION_NATIVE_HOST.md)"
    )


def user_manifest_dir(
    browser: str,
    *,
    home: Optional[Path] = None,
    platform: str = sys.platform,
) -> Path:
    """The per-user NativeMessagingHosts directory for ``browser``."""

    key = (_platform_key(platform), browser)
    if key not in _USER_DIRS:
        raise ValueError(f"unknown browser {browser!r} (chrome or chromium)")
    base = Path(home) if home is not None else Path.home()
    return base.joinpath(*_USER_DIRS[key], "NativeMessagingHosts")


def profile_manifest_dir(user_data_dir: Path | str) -> Path:
    """The NativeMessagingHosts directory inside one Chromium user data dir."""

    return Path(user_data_dir) / "NativeMessagingHosts"


def build_manifest(extension_id: str, launcher: Path) -> dict[str, Any]:
    """The host manifest allowing exactly one extension origin."""

    if not isinstance(extension_id, str) or not EXTENSION_ID.fullmatch(extension_id):
        raise ValueError("extension id must be 32 letters a-p")
    launcher = Path(launcher)
    if not launcher.is_absolute():
        raise ValueError("launcher path must be absolute")
    return {
        "name": HOST_NAME,
        "description": "OpenAdapt optional Chrome extension evidence host",
        "path": str(launcher),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{extension_id}/"],
    }


def launcher_text(python: str, *, recording: Optional[Path] = None) -> str:
    """A POSIX launcher that runs the host with ``python``."""

    argv = [python, str(HOST_SCRIPT)]
    if recording is not None:
        argv += ["--recording", str(Path(recording).resolve())]
    command = " ".join(shlex.quote(part) for part in argv)
    # Chrome appends the caller origin (and on some platforms a window id);
    # the host ignores unknown arguments.
    return f'#!/bin/sh\nexec {command} "$@"\n'


def install(
    manifest_dir: Path | str,
    extension_id: str,
    *,
    python: str = sys.executable,
    recording: Optional[Path | str] = None,
    platform: str = sys.platform,
) -> Path:
    """Write the launcher and manifest into ``manifest_dir``. Returns the manifest."""

    _platform_key(platform)
    if not python or not Path(python).is_absolute():
        raise ValueError("python must be an absolute interpreter path")
    target = Path(manifest_dir).resolve()
    launcher = target / LAUNCHER_FILE
    manifest = build_manifest(extension_id, launcher)  # validate before writing
    target.mkdir(parents=True, exist_ok=True)
    launcher.write_text(
        launcher_text(
            python, recording=Path(recording) if recording is not None else None
        ),
        encoding="utf-8",
    )
    os.chmod(launcher, 0o700)
    path = target / MANIFEST_FILE
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def uninstall(manifest_dir: Path | str) -> list[Path]:
    """Remove this host's manifest and launcher from ``manifest_dir``."""

    removed = []
    for name in (MANIFEST_FILE, LAUNCHER_FILE):
        path = Path(manifest_dir) / name
        if path.is_file():
            path.unlink()
            removed.append(path)
    return removed


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--user-data-dir", help="Chromium profile directory")
    where.add_argument("--browser", choices=("chrome", "chromium"))
    parser.add_argument("--extension-id", help="unpacked extension id")
    parser.add_argument("--recording", help="recording directory for the host")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args(argv)
    try:
        target = (
            profile_manifest_dir(args.user_data_dir)
            if args.user_data_dir
            else user_manifest_dir(args.browser)
        )
        if args.uninstall:
            for path in uninstall(target):
                print(f"removed {path}")
            return 0
        if not args.extension_id:
            parser.error("--extension-id is required")
        path = install(target, args.extension_id, recording=args.recording)
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"installed {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

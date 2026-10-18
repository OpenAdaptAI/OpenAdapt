"""Where the shadow buffer may live.

The buffer, its key record, and the files next to them belong in the
platform's local app-data folder: ``~/Library/Application Support`` on macOS,
``%LOCALAPPDATA%`` on Windows, and ``$XDG_DATA_HOME`` (or ``~/.local/share``)
on Linux. None of those is synced by default.

Folders that sync services copy off the computer are refused: Documents and
Desktop (iCloud Desktop and Documents, OneDrive folder backup), iCloud Drive,
the macOS ``CloudStorage`` folder that OneDrive, Dropbox, Google Drive and Box
use, and the Dropbox, OneDrive, Google Drive and Box folders on any platform.
The check follows symlinks, so a link from app data into a synced folder is
refused too.

The check can't see every sync or backup tool. An OS backup, a sync client
pointed at an unusual folder, or endpoint security software can still copy
local files. The notice says so.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

#: Folder names that sync clients create. A path with any of these as a
#: component is treated as synced. Matched case-insensitively; OneDrive also
#: matches "OneDrive - Company".
SYNC_FOLDER_NAMES: frozenset[str] = frozenset(
    {
        "dropbox",
        "onedrive",
        "google drive",
        "googledrive",
        "my drive",
        "icloud drive",
        "iclouddrive",
        "mobile documents",
        "cloudstorage",
        "box sync",
        "pcloud drive",
        "nextcloud",
        "owncloud",
    }
)

#: Windows environment variables that name OneDrive folders.
_ONEDRIVE_ENV = ("OneDrive", "OneDriveCommercial", "OneDriveConsumer")


class ShadowStorageRefused(ValueError):
    """The shadow buffer path is inside a folder that may be synced."""


def app_data_dir(app_id: str) -> Path:
    """The local, unsynced app-data folder for ``app_id`` on this platform.

    It reads ``LOCALAPPDATA`` on Windows and ``XDG_DATA_HOME`` on Linux,
    because those are where the platform puts app data. The result still
    goes through :func:`require_unsynced` before anything is written.
    """

    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / app_id
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        return (Path(local) if local else home / "AppData" / "Local") / app_id
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else home / ".local" / "share") / app_id


def _resolve(path: Path) -> Path:
    """Resolve symlinks, including those above a path that doesn't exist yet."""

    path = Path(path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve(strict=False)


def _dropbox_roots(home: Path) -> list[Path]:
    """The folders Dropbox's own ``info.json`` names, if it's installed."""

    roots: list[Path] = []
    candidates = [home / ".dropbox" / "info.json"]
    for env in ("APPDATA", "LOCALAPPDATA"):
        if base := os.environ.get(env):
            candidates.append(Path(base) / "Dropbox" / "info.json")
    for info in candidates:
        try:
            data = json.loads(info.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            for account in data.values():
                if isinstance(account, dict) and isinstance(account.get("path"), str):
                    roots.append(Path(account["path"]))
    return roots


def sync_roots(home: Path | None = None) -> list[Path]:
    """Known folders that sync clients may copy off this computer."""

    home = Path.home() if home is None else home
    roots = [
        home / "Documents",
        home / "Desktop",
        home / "Library" / "Mobile Documents",
        home / "Library" / "CloudStorage",
        home / "iCloudDrive",
        home / "Box",
    ]
    for env in _ONEDRIVE_ENV:
        if value := os.environ.get(env):
            roots.append(Path(value))
    roots.extend(_dropbox_roots(home))
    return roots


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def sync_root_for(path: Path, *, home: Path | None = None) -> Path | None:
    """The sync root ``path`` falls inside, or None if it's in none we know."""

    resolved = _resolve(path)
    for root in sync_roots(home):
        for candidate in {root, _resolve(root)}:
            if _within(resolved, candidate):
                return candidate
    for index, part in enumerate(resolved.parts):
        name = part.casefold()
        if name in SYNC_FOLDER_NAMES or name.startswith("onedrive"):
            return Path(*resolved.parts[: index + 1])
    return None


def require_unsynced(path: Path, *, home: Path | None = None) -> Path:
    """Return ``path`` resolved, or refuse it if it's inside a sync root.

    Raises:
        ShadowStorageRefused: The path, after following symlinks, is inside
            a folder that a sync client may copy off this computer.
    """

    root = sync_root_for(path, home=home)
    if root is not None:
        raise ShadowStorageRefused(
            "Shadow mode won't keep its buffer in a folder that may be synced "
            f"off this computer ({root}). Use the app-data folder instead."
        )
    return _resolve(path)

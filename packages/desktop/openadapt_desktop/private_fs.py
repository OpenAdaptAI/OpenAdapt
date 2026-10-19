"""Owner-only permissions for Desktop's local data.

Captures, run folders, reports and the index database can hold patient
details. On a shared workstation another local account must not be able to
list or read them, so Desktop keeps its data directories at 0700 and its files
at 0600. The engine also sets a 077 umask at startup, which covers files that
Flow and other child processes write inside those directories.

Windows has no POSIX permission bits; there these helpers only create the
directories and leave access control to the user profile's ACLs.
"""

from __future__ import annotations

import os
from pathlib import Path

PRIVATE_UMASK = 0o077
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def _posix() -> bool:
    return os.name != "nt"


def apply_private_umask() -> None:
    """Make every file and directory this process creates owner-only.

    Child processes (the Flow subprocess, the embedded Flow mode) inherit it.
    """

    if _posix():
        os.umask(PRIVATE_UMASK)


def ensure_private_dir(path: Path, *, root: Path | None = None) -> Path:
    """Create ``path`` if needed and make it owner-only.

    When ``root`` is given and contains ``path``, every directory from
    ``root`` down to ``path`` is tightened too, so an install made before this
    rule (0755 folders) is fixed the next time the engine touches it.
    Directories above ``root`` (such as the home folder) are never changed.
    """

    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    if not _posix():
        return path
    chain = [path]
    if root is not None:
        root = Path(root)
        try:
            relative = path.relative_to(root)
        except ValueError:
            relative = None
        if relative is not None:
            chain = [root]
            current = root
            for part in relative.parts:
                current = current / part
                chain.append(current)
    for directory in chain:
        os.chmod(directory, PRIVATE_DIR_MODE)
    return path


def make_private_file(path: Path) -> Path:
    """Make an existing file owner-only (0600)."""

    path = Path(path)
    if _posix() and path.exists():
        os.chmod(path, PRIVATE_FILE_MODE)
    return path


def secure_sqlite_files(db_path: Path) -> None:
    """Make a SQLite database and its WAL and shared-memory files owner-only."""

    db_path = Path(db_path)
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        make_private_file(candidate)

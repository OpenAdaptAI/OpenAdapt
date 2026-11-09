"""Cooperative mutation lock and durable publication for sealed-set maintenance.

The private lock directory is outside authenticated payload inventories.
Sealed-set writers and maintenance commits also share a POSIX directory lock,
independent of process lock-directory configuration. Inventory and revision
checks remain mandatory. These cooperative locks do not constrain other tools
or a privileged host.
"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class MaintenanceError(ValueError):
    """Maintenance could not preserve an exact source or durable publication."""


@contextmanager
def _root_directory_lock(root: Path, deadline: float) -> Iterator[None]:
    """Pin/lock the root inode across header replacement on the proven backend."""
    if os.name != "posix":
        yield
        return
    import fcntl

    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    acquired = False
    try:
        original = os.fstat(descriptor)
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except (BlockingIOError, OSError) as exc:
                if time.monotonic() >= deadline:
                    raise MaintenanceError("mutation_lock_busy") from exc
                time.sleep(0.02)

        def revalidate() -> None:
            current = root.lstat()
            if not stat.S_ISDIR(current.st_mode) or (
                current.st_dev,
                current.st_ino,
            ) != (original.st_dev, original.st_ino):
                raise MaintenanceError("source_directory_replaced")
            if any(parent.is_symlink() for parent in root.parents):
                raise MaintenanceError("source_directory_ancestry_changed")

        revalidate()
        yield
        revalidate()
    finally:
        try:
            if acquired:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


@contextmanager
def mutation_lock(root: Path, *, wait_s: float = 10.0) -> Iterator[None]:
    """Kernel-released, no-follow lock; a stale lock file is not ownership."""
    root = Path(os.path.normcase(os.path.realpath(root)))
    default = Path(tempfile.gettempdir()).resolve() / (
        "openadapt-sealed-mutation-"
        + hashlib.sha256(os.fsencode(Path.home())).hexdigest()[:16]
    )
    lock_root = Path(
        os.environ.get("OPENADAPT_SEALED_LOCK_DIR", str(default))
    ).absolute()
    if any(parent.is_symlink() for parent in (lock_root, *lock_root.parents)):
        raise MaintenanceError("unsafe_mutation_lock_directory")
    lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = lock_root.lstat()
    if not stat.S_ISDIR(directory.st_mode) or (
        os.name == "posix"
        and (directory.st_uid != os.geteuid() or directory.st_mode & 0o077)
    ):
        raise MaintenanceError("unsafe_mutation_lock_directory")
    path = lock_root / (hashlib.sha256(os.fsencode(root)).hexdigest() + ".lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    acquired = False
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or path.is_symlink():
            raise MaintenanceError("unsafe_mutation_lock")
        if os.name == "nt" and info.st_size == 0:
            os.write(descriptor, b"0")
            os.fsync(descriptor)
        deadline = time.monotonic() + wait_s
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except (BlockingIOError, OSError) as exc:
                if time.monotonic() >= deadline:
                    raise MaintenanceError("mutation_lock_busy") from exc
                time.sleep(0.02)
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise MaintenanceError("mutation_lock_replaced")
        with _root_directory_lock(root, deadline):
            yield
    finally:
        try:
            if acquired:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def sync_directory(path: Path) -> None:
    """Refuse unsupported directory durability rather than claim it."""
    if os.name != "posix":
        raise MaintenanceError("directory_durability_unsupported")
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _staging_parent(path: Path) -> Path:
    """Stage outside the target and all enclosing sealed ownership boundaries."""
    root = path.parent
    if any(parent.is_symlink() for parent in (root, *root.parents)):
        raise MaintenanceError("source_directory_ancestry_changed")
    staging = root.parent
    for ancestor in root.parents:
        try:
            (ancestor / path.name).lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise MaintenanceError("staging_ownership_unreadable") from exc
        if ancestor.parent == ancestor:
            raise MaintenanceError("outside_sealed_staging_unavailable")
        staging = ancestor.parent
    return staging


def durable_replace(
    path: Path, data: bytes, *, expected_parent: tuple[int, int]
) -> None:
    """Fsync encrypted header bytes outside sealed ancestors, then replace.

    A hard exit before rename must not strand an unindexed file in this set or
    an enclosing set. Cross-device staging refuses; no copy fallback is safe.
    Callers hold the set lock; no plaintext or unwrapped DEK is accepted here.
    """
    import secrets

    if os.name != "posix":
        raise MaintenanceError("directory_durability_unsupported")
    staging_path = _staging_parent(path)
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    staging: int | None = None
    temporary = f".openadapt-header-{secrets.token_hex(16)}.tmp"
    created = False
    try:
        info = os.fstat(parent)
        if (info.st_dev, info.st_ino) != expected_parent:
            raise MaintenanceError("source_directory_replaced")
        staging = os.open(staging_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stage_info = os.fstat(staging)
        current = staging_path.lstat()
        if (stage_info.st_dev, stage_info.st_ino) != (current.st_dev, current.st_ino):
            raise MaintenanceError("staging_directory_replaced")
        if stage_info.st_dev != info.st_dev:
            raise MaintenanceError("cross_device_staging")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=staging,
        )
        created = True
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Recheck ancestry/source immediately before publication. The retained
        # destination descriptor still names only the authorized root inode.
        if _staging_parent(path) != staging_path:
            raise MaintenanceError("staging_ownership_changed")
        current_root = path.parent.lstat()
        if (current_root.st_dev, current_root.st_ino) != expected_parent:
            raise MaintenanceError("source_directory_replaced")
        os.replace(temporary, path.name, src_dir_fd=staging, dst_dir_fd=parent)
        created = False
        os.fsync(parent)
        os.fsync(staging)
    finally:
        try:
            if staging is not None and created:
                os.unlink(temporary, dir_fd=staging)
        finally:
            if staging is not None:
                os.close(staging)
            os.close(parent)


def rename_claim(parent_fd: int, source: str, destination: str) -> None:
    """Linux descriptor-relative no-replace rename; never emulate by overwrite.

    Unsupported kernels/filesystems refuse with their native error. The small
    libc ABI wrapper uses no borrowed implementation or crypto primitive.
    """
    import ctypes
    import sys

    if sys.platform != "linux":
        raise MaintenanceError("no_replace_rename_unsupported")
    try:
        operation = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:
        raise MaintenanceError("no_replace_rename_unsupported") from exc
    operation.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    operation.restype = ctypes.c_int
    if (
        operation(
            parent_fd, os.fsencode(source), parent_fd, os.fsencode(destination), 1
        )
        != 0
    ):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))

#!/usr/bin/env python3
"""Extract a release tarball without allowing it to escape its staging dir."""

from __future__ import annotations

import os
import shutil
import sys
import tarfile
from pathlib import Path, PurePosixPath

MAX_MEMBERS = 50_000
MAX_ARCHIVE_BYTES = 10 * 1024**3
MAX_FILE_BYTES = 2 * 1024**3
MAX_TOTAL_BYTES = 8 * 1024**3


def _safe_name(name: str) -> PurePosixPath:
    if not name or "\\" in name or any(ord(char) < 32 for char in name):
        raise ValueError(f"unsafe archive member name: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"unsafe archive member path: {name!r}")
    return path


def extract(archive: Path, destination: Path) -> None:
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("release archive exceeds compressed size limit")
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    seen: set[PurePosixPath] = set()
    total = 0

    with tarfile.open(archive, mode="r:*") as tar:
        checked: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
        for member in tar:
            if len(checked) >= MAX_MEMBERS:
                raise ValueError(f"release archive has more than {MAX_MEMBERS} entries")
            path = _safe_name(member.name.rstrip("/"))
            if path in seen:
                raise ValueError(f"duplicate archive member: {member.name!r}")
            seen.add(path)
            if not (member.isdir() or member.isreg()):
                raise ValueError(
                    f"unsupported archive member type (links/devices are forbidden): {member.name!r}"
                )
            if member.size < 0 or member.size > MAX_FILE_BYTES:
                raise ValueError(f"archive member exceeds size limit: {member.name!r}")
            total += member.size
            if total > MAX_TOTAL_BYTES:
                raise ValueError("release archive exceeds total uncompressed size limit")
            checked.append((member, path))

        if not checked:
            raise ValueError("release archive is empty")

        for member, relative in checked:
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue

            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            source = tar.extractfile(member)
            if source is None:
                raise ValueError(f"could not read archive member: {member.name!r}")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(target, flags, 0o600)
            with source, os.fdopen(fd, "wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
                if output.tell() != member.size:
                    raise ValueError(f"archive member size changed while extracting: {member.name!r}")


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: safe-extract-release.py ARCHIVE DESTINATION", file=sys.stderr)
        return 2
    try:
        extract(Path(sys.argv[1]), Path(sys.argv[2]))
    except (OSError, tarfile.TarError, ValueError) as exc:
        print(f"safe-extract-release.py: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

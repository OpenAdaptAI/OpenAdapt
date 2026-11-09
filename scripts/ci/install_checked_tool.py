#!/usr/bin/env python3
"""Install one SHA256-pinned tar.gz member with bounded download retries."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import re
import subprocess
import tarfile
import tempfile
import time
import zlib
from pathlib import Path, PurePosixPath

ATTEMPTS = 3
BUDGET_SECONDS = 150
ATTEMPT_SECONDS = 45
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_EXPANDED_BYTES = 128 * 1024 * 1024


class InstallError(Exception):
    """The intended tool could not be verified and installed."""


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise InstallError("installation deadline exceeded")
    return remaining


def _member_name(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        bool(path.parts)
        and not path.is_absolute()
        and ".." not in path.parts
        and "\\" not in name
        and not any(ord(c) < 32 for c in name)
    )


def _unpack(archive: Path, member: str, scratch: Path, deadline: float) -> Path:
    # Read through gzip EOF to validate its trailer/CRC before publishing a file.
    expanded = scratch / "archive.tar"
    total = 0
    with gzip.open(archive, "rb") as compressed, expanded.open("xb") as output:
        while chunk := compressed.read(1024 * 1024):
            _remaining(deadline)
            total += len(chunk)
            if total > MAX_EXPANDED_BYTES:
                raise InstallError("expanded archive exceeds size limit")
            output.write(chunk)
    selected = None
    seen: set[str] = set()
    with tarfile.open(expanded, "r:") as package:
        for entry in package:
            _remaining(deadline)
            if len(seen) >= 1024 or entry.name in seen or not _member_name(entry.name):
                raise InstallError("invalid or duplicate archive path")
            seen.add(entry.name)
            if not (entry.isfile() or entry.isdir()):
                raise InstallError("archive contains a non-regular member")
            if entry.size < 0 or entry.size > MAX_EXPANDED_BYTES:
                raise InstallError("invalid archive member size")
            if entry.name == member:
                if not entry.isfile() or entry.size == 0 or not entry.mode & 0o111:
                    raise InstallError(
                        "tool member is not a nonempty regular executable"
                    )
                selected = entry
        if selected is None:
            raise InstallError("declared tool member is missing")
        stream = package.extractfile(selected)
        if stream is None:
            raise InstallError("declared tool member cannot be read")
        binary = scratch / "tool"
        with stream, binary.open("xb") as output:
            copied = 0
            while chunk := stream.read(1024 * 1024):
                _remaining(deadline)
                copied += len(chunk)
                output.write(chunk)
        if copied != selected.size:
            raise InstallError("tool member is truncated")
    binary.chmod(0o755)
    return binary


def install(url: str, sha256: str, member: str, destination: Path) -> None:
    """Install verified bytes; failed attempts never publish a partial tool."""
    if not re.fullmatch(r"[0-9a-f]{64}", sha256) or not _member_name(member):
        raise InstallError("invalid checksum or archive member")
    if destination.exists() or destination.is_symlink():
        raise InstallError("destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + BUDGET_SECONDS
    failure = "download unavailable"
    for attempt in range(1, ATTEMPTS + 1):
        try:
            remaining = min(ATTEMPT_SECONDS, _remaining(deadline))
            with tempfile.TemporaryDirectory(
                prefix=".checked-tool-", dir=destination.parent
            ) as temp:
                scratch = Path(temp)
                archive = scratch / "download.tar.gz"
                # No curlrc, implicit netrc, shell evaluation or payload logging.
                # Production callers pin HTTPS URLs. HTTP permits local synthetic tests;
                # redirects may only target HTTPS, never downgrade the release transfer.
                result = subprocess.run(
                    [
                        "curl",
                        "--disable",
                        "--fail",
                        "--silent",
                        "--show-error",
                        "--location",
                        "--max-redirs",
                        "5",
                        "--proto",
                        "=http,https",
                        "--proto-redir",
                        "=https",
                        "--connect-timeout",
                        str(min(10, remaining)),
                        "--max-time",
                        str(remaining),
                        "--max-filesize",
                        str(MAX_ARCHIVE_BYTES),
                        "--output",
                        str(archive),
                        "--write-out",
                        "%{http_code}",
                        url,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=remaining,
                    check=False,
                )
                if result.returncode or result.stdout.strip() != "200":
                    raise InstallError(
                        f"download failed (curl={result.returncode}, HTTP={result.stdout.strip()[:3]})"
                    )
                if (
                    not archive.is_file()
                    or not 0 < archive.stat().st_size <= MAX_ARCHIVE_BYTES
                ):
                    raise InstallError("invalid download size")
                if hashlib.sha256(archive.read_bytes()).hexdigest() != sha256:
                    raise InstallError("download checksum mismatch")
                binary = _unpack(archive, member, scratch, deadline)
                _remaining(deadline)
                # Atomic creation refuses a preexisting destination, including races.
                os.link(binary, destination)
                print(f"Installed verified tool: {destination.name}")
                return
        except (
            InstallError,
            OSError,
            EOFError,
            tarfile.TarError,
            zlib.error,
            subprocess.TimeoutExpired,
        ) as exc:
            failure = str(exc) if isinstance(exc, InstallError) else type(exc).__name__
            print(
                f"Tool installation attempt {attempt}/{ATTEMPTS} failed: {failure}",
                flush=True,
            )
        if attempt < ATTEMPTS:
            time.sleep(min(attempt, _remaining(deadline)))
    raise InstallError(f"tool installation failed after {ATTEMPTS} attempts: {failure}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--url", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--member", required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.url.startswith("https://"):
        parser.error("the release URL must use HTTPS")
    try:
        install(args.url, args.sha256, args.member, args.destination)
    except InstallError as exc:
        parser.exit(1, f"{exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

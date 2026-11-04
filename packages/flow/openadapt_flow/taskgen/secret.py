"""The secret that seeds every non-public split.

A public split is generated from public seeds, so anyone can regenerate it.
Every other split (held-out, private training) takes its seeds and task ids
from an HMAC over a salt kept privately (``TASKGEN_SALT_FILE``). Without the
salt the generator refuses to write a non-public split, and the public code
and manifests can't reproduce or confirm one.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path
from typing import Optional

SALT_ENV = "TASKGEN_SALT_FILE"
SALT_CHECK_ENV = "TASKGEN_SALT_CHECK_FILE"
CHECK_SCHEMA = "openadapt.taskgen-salt-check/v1"
CHECK_LABEL = b"openadapt.taskgen-salt-check/v1"
PUBLIC = "public"


class SaltMissing(RuntimeError):
    """A non-public split was asked for without the private salt."""


class SaltMismatch(SaltMissing):
    """The salt does not match the private check file."""


def _salt_path() -> str:
    path = os.environ.get(SALT_ENV)
    if not path:
        raise SaltMissing(
            f"a private split needs the private salt: set {SALT_ENV}. Without it "
            "only the public split can be generated."
        )
    return path


def _read_salt_fd(fd: int, path: str) -> bytes:
    chunks = []
    while chunk := os.read(fd, 65536):
        chunks.append(chunk)
    salt = b"".join(chunks).strip()
    if len(salt) < 32:
        raise SaltMissing(f"{path}: the salt must be at least 32 bytes")
    return salt


def _read_salt() -> bytes:
    path = _salt_path()
    fd = os.open(path, os.O_RDONLY)  # the salt is only ever opened read-only
    try:
        return _read_salt_fd(fd, path)
    finally:
        os.close(fd)


def fingerprint(salt: bytes) -> str:
    """An HMAC of a fixed label under ``salt``. It identifies the salt without
    revealing it."""
    return hmac.new(salt, CHECK_LABEL, hashlib.sha256).hexdigest()


def check_salt(salt: bytes) -> None:
    """Refuse unless ``salt`` matches the check file in ``TASKGEN_SALT_CHECK_FILE``.

    A missing, unreadable or malformed check file refuses too, so a split is
    never written with an unchecked salt."""

    path = os.environ.get(SALT_CHECK_ENV)
    if not path:
        raise SaltMismatch(
            f"set {SALT_CHECK_ENV} to the private salt check file, so a wrong "
            "salt can't silently produce a different split"
        )
    try:
        doc = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise SaltMismatch(f"{path}: can't read the salt check file") from exc
    expected = doc.get("hmac_sha256") if isinstance(doc, dict) else None
    if (
        not isinstance(doc, dict)
        or doc.get("schema") != CHECK_SCHEMA
        or not isinstance(expected, str)
        or len(expected) != 64
    ):
        raise SaltMismatch(f"{path}: not a {CHECK_SCHEMA} file")
    if not hmac.compare_digest(fingerprint(salt), expected.lower()):
        raise SaltMismatch(
            f"the salt in {SALT_ENV} does not match {path}: refusing, because a "
            "different salt would produce a different split"
        )


def load_salt(split: str) -> Optional[bytes]:
    """The salt for ``split``: None for the public split; else read from
    ``TASKGEN_SALT_FILE`` and checked against ``TASKGEN_SALT_CHECK_FILE``,
    refusing when either isn't set, the salt is short, or they don't match."""

    if split == PUBLIC:
        return None
    try:
        salt = _read_salt()
    except SaltMissing as exc:
        raise SaltMissing(f"split {split!r}: {exc}") from None
    check_salt(salt)
    return salt


class CheckExists(SaltMissing):
    """The check file can't be written without risking the salt or a pin."""


def write_check(path: Path) -> None:
    """Write a new check file for the salt in ``TASKGEN_SALT_FILE``.

    Only the HMAC is written; the salt is never printed or copied. The write
    never replaces anything: it refuses when ``path`` already exists (a file,
    a hard link or a symlink, even a dangling one) or names the salt itself,
    so the salt and any existing pin survive a mistake. A new pin needs a new
    path. The file is written to an exclusively created temporary file and
    then linked into place, which also fails if ``path`` appears meanwhile.
    """

    salt_path = _salt_path()
    out = Path(path)
    if os.path.lexists(out):
        raise CheckExists(
            f"{out} already exists: refusing to overwrite it. Choose a new path "
            "for a new check file."
        )
    if os.path.realpath(out) == os.path.realpath(salt_path):
        raise CheckExists(f"{out} is the salt file itself: refusing")
    fd = os.open(salt_path, os.O_RDONLY)  # read-only: the salt is never written
    try:
        salt = _read_salt_fd(fd, salt_path)
    finally:
        os.close(fd)
    doc = {"schema": CHECK_SCHEMA, "hmac_sha256": fingerprint(salt)}
    data = (json.dumps(doc, indent=2) + "\n").encode()
    tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        tfd = os.open(tmp, flags, 0o600)
    except FileExistsError as exc:
        raise CheckExists(f"{tmp} exists: refusing to touch it") from exc
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(tfd, view) :]
            os.fsync(tfd)
        finally:
            os.close(tfd)
        # link() never replaces an existing file, unlike rename().
        try:
            os.link(tmp, out)
        except FileExistsError as exc:
            raise CheckExists(f"{out} appeared while writing: refusing") from exc
    finally:
        os.unlink(tmp)  # only the temporary file this call created


def _mac(salt: bytes, label: str, name: str) -> bytes:
    return hmac.new(salt, f"{label}|{name}".encode(), hashlib.sha256).digest()


def seed(salt: Optional[bytes], name: str, public_seed: int) -> int:
    """A task's seed: the public counter, or an HMAC of its name."""
    if salt is None:
        return public_seed
    return int.from_bytes(_mac(salt, "seed", name)[:6], "big")


def task_id(salt: Optional[bytes], name: str, prefix: str) -> str:
    """An opaque task id. Public ids hash the name; private ids are an HMAC,
    so a public guess of the name can't confirm one."""
    if salt is None:
        digest = hashlib.sha256(name.encode()).hexdigest()
    else:
        digest = _mac(salt, "id", name).hex()
    return f"{prefix}-{digest[:12]}"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Write the private salt check file.")
    ap.add_argument(
        "--write-check",
        required=True,
        type=Path,
        help=f"where to write the check for the salt in {SALT_ENV}",
    )
    args = ap.parse_args(argv)
    try:
        write_check(args.write_check)
    except SaltMissing as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {args.write_check}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

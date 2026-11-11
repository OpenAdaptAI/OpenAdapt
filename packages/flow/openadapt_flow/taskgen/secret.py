"""The secret that seeds every non-public split.

A public split is generated from public seeds, so anyone can regenerate it.
Every other split (held-out, private training) takes its seeds and task ids
from an HMAC over a salt kept privately (``TASKGEN_SALT_FILE``). Without the
salt the generator refuses to write a non-public split, and the public code
and manifests can't reproduce or confirm one.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
from typing import Optional

SALT_ENV = "TASKGEN_SALT_FILE"
PUBLIC = "public"


class SaltMissing(RuntimeError):
    """A non-public split was asked for without the private salt."""


def load_salt(split: str) -> Optional[bytes]:
    """The salt for ``split``: None for the public split; else read from
    ``TASKGEN_SALT_FILE``, refusing when it isn't set or is short."""

    if split == PUBLIC:
        return None
    path = os.environ.get(SALT_ENV)
    if not path:
        raise SaltMissing(
            f"split {split!r} needs the private salt: set {SALT_ENV}. Without it "
            "only the public split can be generated."
        )
    salt = Path(path).read_bytes().strip()
    if len(salt) < 32:
        raise SaltMissing(f"{path}: the salt must be at least 32 bytes")
    return salt


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

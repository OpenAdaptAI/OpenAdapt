"""Principal identifiers that may leave this machine.

An admission names who admitted, reviewed, recorded and ran a program. Those
names stay in plaintext on this machine. A copy that goes to OpenAdapt Cloud
(``openadapt wrap publish``) carries a name in plaintext only when it is a
namespaced machine handle or a key fingerprint:

* ``ci:<name>``, ``agent:<name>``, ``service:<name>`` with ``<name>`` in
  ``[a-z0-9._-]``;
* ``sha256:<hex>`` or ``ed25519:<hex>`` (a key fingerprint);
* ``standard-change:<hex>`` and ``self-repair:probe`` (a repair's approver
  when a signed policy or a probe stands in for a person).

Anything else, such as ``jane.doe``, ``user:jane.doe`` or a record number, is
replaced by ``hmac:<hex>``: HMAC-SHA256 of the lower-cased, trimmed name,
keyed with this install's principal key. The key is 32 random bytes written
on first use to ``<key_dir>/keys/principal-hmac.key`` (mode 0600), next to
the admission signing key. It never leaves the machine, so the cloud cannot
reverse the value. The same name always gives the same value on one install,
so the cloud copy still shows when two roles are the same principal, and the
admitting install can say who a value names.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from pathlib import Path
from typing import Optional

__all__ = [
    "DISCLOSABLE_PRINCIPAL",
    "HMAC_KEY_NAME",
    "is_disclosable",
    "pseudonym",
]

#: A principal value that may appear in plaintext in an uploaded artifact.
DISCLOSABLE_PRINCIPAL = re.compile(
    r"(?:"
    r"(?:ci|agent|service):[a-z0-9._-]{1,120}"
    r"|(?:sha256|ed25519):[0-9a-f]{16,256}"
    r"|standard-change:[0-9a-f]{16,128}"
    r"|self-repair:probe"
    r"|hmac:[0-9a-f]{64}"
    r")\Z"
)
HMAC_KEY_NAME = "principal-hmac.key"


def is_disclosable(value: str) -> bool:
    """True when ``value`` may leave this machine as it is."""

    return bool(DISCLOSABLE_PRINCIPAL.fullmatch(value))


def _key(key_dir: Path) -> bytes:
    path = Path(key_dir) / "keys" / HMAC_KEY_NAME
    if path.is_symlink():
        raise ValueError("the principal key is a symbolic link")
    if path.is_file():
        raw = bytes.fromhex(path.read_text(encoding="utf-8").strip())
        if len(raw) < 32:
            raise ValueError("the principal key is too short")
        return raw
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = secrets.token_bytes(32)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(raw.hex() + "\n")
    tmp.replace(path)
    return raw


def pseudonym(value: Optional[str], key_dir: Path) -> Optional[str]:
    """``value`` as it may leave this machine (see the module docstring)."""

    if value is None or value == "":
        return value
    text = str(value)
    if is_disclosable(text):
        return text
    digest = hmac.new(
        _key(key_dir), text.strip().lower().encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return "hmac:" + digest

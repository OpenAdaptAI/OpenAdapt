"""Local Ed25519 key for self-signed Execute receipts.

The key lives under the operator's data directory. Nothing here talks to
OpenAdapt Cloud or to ``openadapt.ai``.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

_KEY_NAME = "ed25519.pem"
_PUB_NAME = "ed25519.pub"


def key_dir(data_dir: Path) -> Path:
    return data_dir / "keys"


def fingerprint_of(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def load_or_create_private_key(data_dir: Path) -> Ed25519PrivateKey:
    """Return the local signing key, generating it on first start."""

    directory = key_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / _KEY_NAME
    if path.is_file():
        loaded = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(loaded, Ed25519PrivateKey):
            raise ValueError(f"execute-ref key is not Ed25519: {path}")
        return loaded
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    _atomic_write(path, pem, mode=0o600)
    pub_hex = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
        .encode("ascii")
        + b"\n"
    )
    _atomic_write(directory / _PUB_NAME, pub_hex, mode=0o644)
    return key


def sign_bytes(key: Ed25519PrivateKey, payload: bytes) -> str:
    return "ed25519:" + key.sign(payload).hex()


def verify_signature(
    public_key: Ed25519PublicKey, payload: bytes, signature: str
) -> bool:
    if not signature.startswith("ed25519:"):
        return False
    try:
        public_key.verify(bytes.fromhex(signature.split(":", 1)[1]), payload)
    except (ValueError, TypeError, InvalidSignature):
        return False
    return True


def load_or_create_token(data_dir: Path, explicit: str | None = None) -> str:
    """Return the local bearer token, generating it on first start."""

    if explicit:
        token = explicit.strip()
        if not token:
            raise ValueError("explicit Execute token must not be empty")
        return token
    path = data_dir / "token"
    if path.is_file():
        token = path.read_text(encoding="utf-8").strip()
        if not token:
            raise ValueError(f"execute-ref token file is empty: {path}")
        return token
    token = os.urandom(24).hex()
    data_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, (token + "\n").encode("utf-8"), mode=0o600)
    return token


def _atomic_write(path: Path, data: bytes, *, mode: int) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, mode | stat.S_IRUSR | stat.S_IWUSR)
    tmp.replace(path)
    os.chmod(path, mode)

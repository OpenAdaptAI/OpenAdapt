"""Per-campaign local test keys.

A campaign signs its case results and fault receipts with Ed25519 keys made
for that one campaign. The private halves stay in memory and are never
written. The public halves are trusted only by the qualification project the
campaign writes. They are local test keys: nothing here creates, reads or
uses a production trust root, and a bundle certified with them proves the
local campaign and nothing more.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field


@dataclass(frozen=True)
class LocalTestKey:
    key_id: str
    public_key_base64: str
    private_key: bytes = field(repr=False)


def new_local_test_key(key_id: str) -> LocalTestKey:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    private_raw = key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return LocalTestKey(
        key_id=key_id,
        public_key_base64=base64.b64encode(public_raw).decode("ascii"),
        private_key=private_raw,
    )


__all__ = ["LocalTestKey", "new_local_test_key"]

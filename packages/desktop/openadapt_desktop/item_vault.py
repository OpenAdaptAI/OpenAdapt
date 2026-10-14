"""Encryption at rest for work-item values, with the key in the OS keychain.

A work item's input values, its label (the first input value) and the
expected and observed text of a right-record check can identify a patient.
The local index database stores them only as AES-256-GCM ciphertext. Each
value is bound to its item and column, so a value can't be moved to another
row unnoticed.

The key is 32 random bytes kept in the OS credential store (macOS Keychain,
Windows Credential Manager, Linux Secret Service) under the account
``work-item-key|<key id>``. The database keeps only the key id and a check
value that proves a key is the right one; neither reveals the key.

The duplicate check ("this item is already done") compares an HMAC of the
values under a key derived from the same key, so the stored digest can't be
used to confirm a guessed member ID.

Everything fails closed. Without a working credential store, or when the key
for this database is gone, Desktop can't store new items, so it doesn't run
them. A lost key is never replaced while the database still holds values
sealed with it: a new key would make every earlier item look new, and the
duplicate check would let an item that is already done be entered again.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from openadapt_desktop.auth import store as _store

ACCOUNT_PREFIX = "work-item-key|"
SEALED_PREFIX = "enc:v1:"
DIGEST_PREFIX = "h1:"
_CHECK_LABEL = b"openadapt.desktop/work-item-key-check/v1"
_DIGEST_LABEL = b"openadapt.desktop/work-item-digest/v1"
_NONCE_BYTES = 12

#: Columns of ``work_items`` that can identify a patient.
SEALED_COLUMNS = ("label", "inputs_json", "expected", "observed")

UNAVAILABLE = (
    "This computer's protected credential store isn't available, so "
    "OpenAdapt can't keep this item's values private. It didn't start the item."
)
KEY_LOST = (
    "The key that protects this computer's saved items is no longer in its "
    "protected credential store, so OpenAdapt can't tell which items are "
    "already done. It didn't start the item. Ask your administrator to "
    "restore the key from a backup of this computer's keychain."
)


class ItemVaultError(RuntimeError):
    """Item values can't be sealed or opened. The message is safe to show."""


def is_sealed(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(SEALED_PREFIX)


def _check_value(key: bytes) -> str:
    return hmac.new(key, _CHECK_LABEL, hashlib.sha256).hexdigest()


def _account(key_id: str) -> str:
    return f"{ACCOUNT_PREFIX}{key_id}"


class ItemVault:
    """Seals and opens one database's item values with its keychain key."""

    def __init__(self, key: bytes) -> None:
        self._aead = AESGCM(key)
        self._digest_key = hmac.new(key, _DIGEST_LABEL, hashlib.sha256).digest()

    def seal(self, text: str, *, item_id: str, column: str) -> str:
        nonce = secrets.token_bytes(_NONCE_BYTES)
        ciphertext = self._aead.encrypt(
            nonce, text.encode("utf-8"), f"{item_id}\x00{column}".encode("utf-8")
        )
        return SEALED_PREFIX + base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")

    def open(self, token: str, *, item_id: str, column: str) -> str:
        try:
            blob = base64.urlsafe_b64decode(token[len(SEALED_PREFIX):].encode("ascii"))
            plain = self._aead.decrypt(
                blob[:_NONCE_BYTES],
                blob[_NONCE_BYTES:],
                f"{item_id}\x00{column}".encode("utf-8"),
            )
        except (InvalidTag, ValueError) as exc:
            raise ItemVaultError("A saved item value can't be read.") from exc
        return plain.decode("utf-8")

    def digest(self, workflow_id: str, values: dict[str, Any]) -> str:
        canonical = json.dumps(
            {"workflow_id": workflow_id, "inputs": values},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        mac = hmac.new(self._digest_key, canonical.encode("utf-8"), hashlib.sha256)
        return DIGEST_PREFIX + mac.hexdigest()


def open_vault(key_id: str | None, key_check: str | None) -> tuple[ItemVault, str, str]:
    """Return this database's vault, with its key id and check value.

    ``key_id`` and ``key_check`` come from the database; both are None for a
    database that has never sealed a value, and only then is a key created.
    """

    # Looked up on the module so a test's in-memory keyring applies here too.
    kr = _store._keyring()
    if key_id is not None:
        encoded = _store._kr_get(kr, _account(key_id))
        if not encoded:
            raise ItemVaultError(KEY_LOST)
        try:
            key = base64.urlsafe_b64decode(encoded.encode("ascii"))
        except ValueError as exc:
            raise ItemVaultError(KEY_LOST) from exc
        if (
            len(key) != 32
            or not key_check
            or not hmac.compare_digest(_check_value(key), key_check)
        ):
            raise ItemVaultError(KEY_LOST)
        return ItemVault(key), key_id, key_check

    key_id = secrets.token_hex(8)
    key = secrets.token_bytes(32)
    encoded = base64.urlsafe_b64encode(key).decode("ascii")
    account = _account(key_id)
    if not _store._kr_set(kr, account, encoded) or _store._kr_get(kr, account) != encoded:
        raise ItemVaultError(UNAVAILABLE)
    return ItemVault(key), key_id, _check_value(key)


__all__ = [
    "DIGEST_PREFIX",
    "KEY_LOST",
    "SEALED_COLUMNS",
    "UNAVAILABLE",
    "ItemVault",
    "ItemVaultError",
    "is_sealed",
    "open_vault",
]

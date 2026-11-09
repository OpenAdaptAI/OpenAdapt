"""The shadow-mining key: kept in the OS credential store, destroyed daily.

One random 32-byte key covers one 24-hour epoch. It is stored in the OS
credential store (macOS Keychain, Windows Credential Manager, Linux Secret
Service) through ``keyring``, as OpenAdapt Desktop's item vault does, together
with the time it was created. A pointer account (``shadow-key|active``) names
the current key id, so the same key comes back after a restart within its
epoch.

Two subkeys are derived from it with HMAC-SHA256 and fixed labels: one keys
the digests of typed values and labels, and one is the AES-256-GCM key for the
on-disk ring buffer.

When the epoch ends (at most 24 hours after the key was created), the key is
deleted from the credential store and the buffer sealed with it is purged.
Short identifiers (a 9-digit SSN, a 7- to 10-digit record number) have few
enough values to try every one against a keyed digest, but only while the key
exists. Destroying the key with the buffer means no digest outlives the means
to check it, and no guess can be tested later.

A key whose recorded creation time is in the future (the clock moved back) is
treated as expired, so a clock change can't stretch an epoch.

When no credential store works (no ``keyring`` package, no backend, a headless
session, or a write that doesn't read back), the key lives in memory only and
``ShadowKeys.persisted`` is False. An encrypted buffer from an earlier run then
can't be read, so it's discarded.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from loguru import logger

SERVICE = "openadapt-capture-shadow"
ACCOUNT_PREFIX = "shadow-key|"
ACTIVE_ACCOUNT = f"{ACCOUNT_PREFIX}active"
#: Ids of keys whose deletion failed. Every later destroy retries them, so a
#: key that couldn't be deleted once isn't orphaned when a new key replaces it.
RETIRED_ACCOUNT = f"{ACCOUNT_PREFIX}retired"
#: The longest a key may live. The buffer's time cap is the same 24 hours.
MAX_KEY_LIFETIME_S = 24 * 3600.0
#: How far ahead of the clock a creation time may be before it's distrusted.
_CLOCK_SKEW_S = 300.0
_RECORD_VERSION = 2
_CHECK_LABEL = b"openadapt.capture/shadow-key-check/v1"
_DIGEST_LABEL = b"openadapt.capture/shadow-digest/v1"
_STORE_LABEL = b"openadapt.capture/shadow-store/v1"


@runtime_checkable
class KeyStore(Protocol):
    """The operations the key needs from a credential store."""

    def get(self, account: str) -> str | None: ...

    def set(self, account: str, value: str) -> bool: ...

    def delete(self, account: str) -> bool: ...


class MemoryKeyStore:
    """An in-process store. Used by tests and as the fallback."""

    persistent = False

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def get(self, account: str) -> str | None:
        return self._values.get(account)

    def set(self, account: str, value: str) -> bool:
        self._values[account] = value
        return True

    def delete(self, account: str) -> bool:
        self._values.pop(account, None)
        return True


class KeyringStore:
    """The OS credential store through ``keyring``. Every failure degrades to
    "no value" (get) or False (set, delete), as Desktop's credential store does."""

    persistent = True

    def __init__(self, service: str = SERVICE, backend: object | None = None) -> None:
        self.service = service
        self._backend = backend

    def _kr(self) -> object | None:
        if self._backend is not None:
            return self._backend
        try:
            import keyring
        except Exception:
            return None
        return keyring

    def get(self, account: str) -> str | None:
        kr = self._kr()
        if kr is None:
            return None
        try:
            return kr.get_password(self.service, account)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.warning("credential store read unavailable: {}", type(exc).__name__)
            return None

    def set(self, account: str, value: str) -> bool:
        kr = self._kr()
        if kr is None:
            return False
        try:
            kr.set_password(self.service, account, value)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.warning("credential store write unavailable: {}", type(exc).__name__)
            return False
        return True

    def delete(self, account: str) -> bool:
        kr = self._kr()
        if kr is None:
            return False
        try:
            kr.delete_password(self.service, account)  # type: ignore[attr-defined]
        except Exception as exc:
            # keyring raises PasswordDeleteError for an entry that isn't there.
            if type(exc).__name__ == "PasswordDeleteError":
                return True
            logger.warning("credential store delete unavailable: {}", type(exc).__name__)
            return False
        return True


def check_value(key: bytes) -> str:
    return hmac.new(key, _CHECK_LABEL, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class ShadowKeys:
    """The master key of one epoch and its derived subkeys."""

    key_id: str
    master: bytes
    created_at: float
    lifetime_s: float
    persisted: bool

    @property
    def expires_at(self) -> float:
        return self.created_at + self.lifetime_s

    def expired(self, now: float) -> bool:
        return now >= self.expires_at or self.created_at > now + _CLOCK_SKEW_S

    @property
    def check(self) -> str:
        return check_value(self.master)

    @property
    def digest_key(self) -> bytes:
        return hmac.new(self.master, _DIGEST_LABEL, hashlib.sha256).digest()

    @property
    def store_key(self) -> bytes:
        return hmac.new(self.master, _STORE_LABEL, hashlib.sha256).digest()

    def __repr__(self) -> str:  # never print key material
        return (
            f"ShadowKeys(key_id={self.key_id!r}, persisted={self.persisted}, "
            f"expires_at={self.expires_at:.0f})"
        )


def _check_lifetime(lifetime_s: float) -> float:
    lifetime = float(lifetime_s)
    if not 0 < lifetime <= MAX_KEY_LIFETIME_S:
        raise ValueError("the shadow key lifetime must be positive and at most 24 hours")
    return lifetime


def _decode(encoded: str | None) -> tuple[bytes, float] | None:
    if not encoded:
        return None
    try:
        record = json.loads(encoded)
        if not isinstance(record, dict) or record.get("v") != _RECORD_VERSION:
            return None
        key = base64.urlsafe_b64decode(str(record["k"]).encode("ascii"))
        created = float(record["c"])
    except (ValueError, KeyError, TypeError):
        return None
    return (key, created) if len(key) == 32 else None


def _encode(key: bytes, created: float) -> str:
    return json.dumps(
        {"v": _RECORD_VERSION, "k": base64.urlsafe_b64encode(key).decode("ascii"), "c": created}
    )


def _retired(store: KeyStore) -> list[str]:
    try:
        ids = json.loads(store.get(RETIRED_ACCOUNT) or "[]")
    except ValueError:
        return []
    return [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else []


def destroy_keys(store: KeyStore | None = None, *, also: tuple[str, ...] = ()) -> bool:
    """Delete the active key, its pointer, and any key a past destroy missed.

    ``also`` names key ids to delete as well (the key an observer holds, in
    case the pointer no longer names it). A key that can't be deleted is
    remembered and retried by every later call. True when every key is gone.
    """

    store = store if store is not None else KeyringStore()
    key_id = store.get(ACTIVE_ACCOUNT)
    wanted = list(dict.fromkeys([*_retired(store), *([key_id] if key_id else []), *also]))
    left = [k for k in wanted if not store.delete(f"{ACCOUNT_PREFIX}{k}")]
    pointer_gone = store.delete(ACTIVE_ACCOUNT)
    if left:
        store.set(RETIRED_ACCOUNT, json.dumps(left))
        logger.warning("{} shadow key(s) could not be deleted; will retry", len(left))
    else:
        store.delete(RETIRED_ACCOUNT)
    return not left and pointer_gone


def load_or_create_keys(
    store: KeyStore | None = None,
    *,
    lifetime_s: float = MAX_KEY_LIFETIME_S,
    now: float | None = None,
) -> ShadowKeys:
    """Load the active key from ``store``, or create and save one.

    An expired key is deleted first, and a new one takes its place. With
    ``store=None`` the OS credential store is used. If it can't hold the key,
    the key stays in memory and ``persisted`` is False.
    """

    lifetime = _check_lifetime(lifetime_s)
    now = time.time() if now is None else float(now)
    store = store if store is not None else KeyringStore()
    persistent = bool(getattr(store, "persistent", True))
    key_id = store.get(ACTIVE_ACCOUNT)
    if key_id:
        decoded = _decode(store.get(f"{ACCOUNT_PREFIX}{key_id}"))
        if decoded is not None:
            key, created = decoded
            keys = ShadowKeys(key_id, key, created, lifetime, persistent)
            if not keys.expired(now):
                return keys
            logger.info("the shadow key's 24-hour epoch ended; destroying it")
        else:
            logger.warning("the active shadow key is missing or unreadable; replacing it")
        destroy_keys(store)
    return create_keys(store, lifetime_s=lifetime, now=now)


def create_keys(
    store: KeyStore | None = None,
    *,
    lifetime_s: float = MAX_KEY_LIFETIME_S,
    now: float | None = None,
) -> ShadowKeys:
    """Create a fresh key for a new epoch and try to save it to ``store``."""

    lifetime = _check_lifetime(lifetime_s)
    now = time.time() if now is None else float(now)
    store = store if store is not None else KeyringStore()
    persistent = bool(getattr(store, "persistent", True))
    key_id = secrets.token_hex(8)
    key = secrets.token_bytes(32)
    encoded = _encode(key, now)
    account = f"{ACCOUNT_PREFIX}{key_id}"
    saved = (
        store.set(account, encoded)
        and store.get(account) == encoded
        and store.set(ACTIVE_ACCOUNT, key_id)
        and store.get(ACTIVE_ACCOUNT) == key_id
    )
    if not saved:
        logger.warning("no credential store for the shadow key; keeping it in memory")
        store.delete(account)
        return ShadowKeys(key_id, key, now, lifetime, False)
    return ShadowKeys(key_id, key, now, lifetime, persistent)

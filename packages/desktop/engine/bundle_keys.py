"""OS-protected keys for sealed local workflow artifacts.

The bundle keeps only ciphertext and integrity metadata.  The corresponding
passphrase is referenced by the Desktop bundle id and remains in the platform
credential store; it is injected into a child Flow process only for the exact
operation that needs it.
"""

from __future__ import annotations

import secrets

from engine.auth.store import _keyring, _kr_delete, _kr_get, _kr_set

_ACCOUNT_PREFIX = "bundle-key|"


class BundleKeyError(RuntimeError):
    """A sealed bundle key could not be stored or recovered safely."""


def _account(bundle_id: str) -> str:
    if not bundle_id or any(char in bundle_id for char in "\r\n"):
        raise BundleKeyError("Invalid workflow identifier for protected key storage")
    return f"{_ACCOUNT_PREFIX}{bundle_id}"


def load_bundle_key(bundle_id: str) -> str | None:
    """Return the protected key for ``bundle_id``, if this machine owns it."""

    return _kr_get(_keyring(), _account(bundle_id))


def store_bundle_key(bundle_id: str, key: str) -> None:
    """Persist one key or fail closed when no OS credential store is available."""

    if not key:
        raise BundleKeyError("A non-empty bundle key is required")
    if not _kr_set(_keyring(), _account(bundle_id), key):
        raise BundleKeyError(
            "This computer's protected credential store is unavailable; "
            "the workflow was not sealed."
        )


def generate_bundle_key(bundle_id: str) -> str:
    """Create and persist a high-entropy passphrase for a new sealed artifact."""

    key = secrets.token_urlsafe(48)
    store_bundle_key(bundle_id, key)
    return key


def copy_bundle_key(source_bundle_id: str, destination_bundle_id: str) -> None:
    """Give an exact encrypted version access to its source artifact key."""

    key = load_bundle_key(source_bundle_id)
    if key is None:
        raise BundleKeyError(
            "The encrypted source workflow key is unavailable on this computer."
        )
    store_bundle_key(destination_bundle_id, key)


def delete_bundle_key(bundle_id: str) -> None:
    """Remove a key created for a version that failed before publication."""

    _kr_delete(_keyring(), _account(bundle_id))


def bundle_key_environment(bundle_id: str) -> dict[str, str]:
    """Return the narrow Flow environment override for an encrypted bundle."""

    key = load_bundle_key(bundle_id)
    return {"OPENADAPT_BUNDLE_KEY": key} if key else {}

"""Opt-in authenticated encryption-at-rest for bundles and durable checkpoints.

A compiled bundle (``workflow.json`` + its manifest) and the durable
checkpoints written during a run are **persistent records** produced from a
recording of a real (in a healthcare deployment, patient) screen. The at-rest
posture doc (``docs/phi_at_rest.md``) called for a real cryptographic control
on top of the salted-hash identity template and the governance guards; this
module is that control's substrate.

Design (see ``docs/phi_at_rest.md`` "Target design"):

* **AEAD, not home-rolled crypto.** Payloads are sealed with AES-256-GCM
  (authenticated encryption) from the audited :mod:`cryptography` library. The
  GCM tag makes a *wrong key* and a *tampered ciphertext* indistinguishable and
  both fail LOUDLY (:class:`DecryptionError`) -- there is no partial / silent
  decrypt.
* **Key from a passphrase.** The caller supplies a passphrase (an explicit
  argument or the ``OPENADAPT_BUNDLE_KEY`` environment variable); a per-payload
  random salt + scrypt KDF stretches it into the 256-bit data key. The salt is
  stored in the container (it is not secret); the passphrase never is.
* **Self-describing container.** The ciphertext is wrapped in a small JSON
  envelope carrying the format tag, cipher/KDF parameters, salt and nonce, so a
  reader needs only the passphrase to decrypt. The envelope's ``format`` tag
  lets :func:`is_encrypted` cheaply tell an encrypted payload from a plaintext
  one, so the *unencrypted default path is untouched*.
* **Domain separation.** A caller passes a domain label as the GCM associated
  data (``BUNDLE_AAD`` / ``CHECKPOINT_AAD``) so a ciphertext sealed for one
  purpose cannot be substituted for another even under the same key.

This module is deliberately import-light (stdlib + :mod:`cryptography`) and
brings in no OCR / cv2 / model dependencies, so it is safe to import on every
bundle load. Encryption is ALWAYS opt-in: nothing here runs unless a caller
explicitly asks for it, and the default serialization stays plaintext.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
from typing import Final, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

#: Environment variable holding the at-rest passphrase (a fallback for an
#: explicit ``key`` argument). Empty / unset means "no key configured".
ENV_KEY: Final[str] = "OPENADAPT_BUNDLE_KEY"

#: Format tag stamped into every container; also the marker :func:`is_encrypted`
#: keys on to distinguish an encrypted payload from plaintext JSON.
_MAGIC: Final[str] = "openadapt-flow-enc"
_FORMAT_VERSION: Final[int] = 1

# scrypt work factors (RFC 7914). N=2**15 is a strong interactive setting
# (~32 MiB, tens of ms) that keeps a low-entropy passphrase expensive to brute
# force. Stored in the container so a future hardening can raise them without
# breaking old bundles.
_SCRYPT_N: Final[int] = 2**15
_SCRYPT_R: Final[int] = 8
_SCRYPT_P: Final[int] = 1

_KEY_LEN: Final[int] = 32  # AES-256
_SALT_LEN: Final[int] = 16
_NONCE_LEN: Final[int] = 12  # 96-bit GCM nonce (recommended)

#: Domain-separation associated data. Passed as the AEAD associated data so a
#: bundle ciphertext cannot be swapped for a checkpoint ciphertext (or vice
#: versa) even when both were sealed with the same passphrase.
BUNDLE_AAD: Final[bytes] = b"openadapt-flow/bundle"
CHECKPOINT_AAD: Final[bytes] = b"openadapt-flow/checkpoint"


class CryptoError(Exception):
    """Base class for at-rest encryption failures."""


class MissingKeyError(CryptoError):
    """Encryption/decryption was requested but no passphrase is configured.

    Raised when neither an explicit ``key`` nor the ``OPENADAPT_BUNDLE_KEY``
    environment variable is set. Fails LOUDLY and safely -- an encrypted bundle
    is never partially loaded, and an encrypt request never silently falls back
    to writing plaintext.
    """


class DecryptionError(CryptoError):
    """A container could not be authentically decrypted.

    Covers a WRONG passphrase and a TAMPERED / corrupted ciphertext alike: AEAD
    makes them indistinguishable, and both must fail closed with no partial
    plaintext returned.
    """


def resolve_key(key: Optional[str]) -> Optional[str]:
    """Return the effective passphrase: the explicit ``key`` if non-empty, else
    the ``OPENADAPT_BUNDLE_KEY`` environment variable, else ``None`` (no key
    configured -- the plaintext default)."""
    if key:
        return key
    env = os.environ.get(ENV_KEY)
    return env or None


def require_key(key: Optional[str]) -> str:
    """Resolve the passphrase or raise :class:`MissingKeyError`.

    Used on the encrypt / decrypt paths where a key is mandatory: an encrypted
    bundle cannot be read, and an encrypt request cannot proceed, without one.
    """
    resolved = resolve_key(key)
    if resolved is None:
        raise MissingKeyError(
            "no encryption passphrase configured: pass an explicit key or set "
            f"the {ENV_KEY} environment variable. An encrypted bundle/checkpoint "
            "cannot be read (and encryption cannot proceed) without it."
        )
    return resolved


def is_encrypted(data: bytes) -> bool:
    """Whether ``data`` is one of this module's encrypted containers.

    A cheap, allocation-light check: an encrypted payload is a JSON object whose
    ``format`` field is the module magic. Plaintext ``workflow.json`` / a
    checkpoint JSON fails this, so the unencrypted path is never misrouted.
    """
    try:
        obj = json.loads(data)
    except (ValueError, TypeError):
        return False
    return isinstance(obj, dict) and obj.get("format") == _MAGIC


def _derive(passphrase: str, salt: bytes, *, n: int, r: int, p: int) -> bytes:
    kdf = Scrypt(salt=salt, length=_KEY_LEN, n=n, r=r, p=p)
    return kdf.derive(passphrase.encode("utf-8"))


def encrypt_bytes(plaintext: bytes, key: Optional[str], *, aad: bytes) -> bytes:
    """Seal ``plaintext`` into a self-describing AES-256-GCM container.

    ``key`` is the passphrase (or ``None`` to fall back to the environment
    variable; a missing key raises :class:`MissingKeyError`). ``aad`` is the
    domain-separation associated data (:data:`BUNDLE_AAD` /
    :data:`CHECKPOINT_AAD`) authenticated -- but not encrypted -- alongside the
    ciphertext. Returns the serialized JSON container as bytes.
    """
    passphrase = require_key(key)
    salt = secrets.token_bytes(_SALT_LEN)
    nonce = secrets.token_bytes(_NONCE_LEN)
    dek = _derive(passphrase, salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    ciphertext = AESGCM(dek).encrypt(nonce, plaintext, aad)
    container = {
        "format": _MAGIC,
        "version": _FORMAT_VERSION,
        "cipher": "AES-256-GCM",
        "kdf": "scrypt",
        "kdf_n": _SCRYPT_N,
        "kdf_r": _SCRYPT_R,
        "kdf_p": _SCRYPT_P,
        "salt": base64.b64encode(salt).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "aad": aad.decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }
    return json.dumps(container, indent=2).encode("utf-8")


def decrypt_bytes(container: bytes, key: Optional[str], *, aad: bytes) -> bytes:
    """Authenticate and decrypt a container produced by :func:`encrypt_bytes`.

    Raises :class:`MissingKeyError` when no passphrase is configured,
    :class:`DecryptionError` when the passphrase is wrong or the ciphertext /
    container was tampered with. Never returns partial plaintext.
    """
    passphrase = require_key(key)
    try:
        obj = json.loads(container)
        if not (isinstance(obj, dict) and obj.get("format") == _MAGIC):
            raise DecryptionError("not an openadapt-flow encrypted container")
        salt = base64.b64decode(obj["salt"])
        nonce = base64.b64decode(obj["nonce"])
        ciphertext = base64.b64decode(obj["ciphertext"])
        n = int(obj.get("kdf_n", _SCRYPT_N))
        r = int(obj.get("kdf_r", _SCRYPT_R))
        p = int(obj.get("kdf_p", _SCRYPT_P))
    except DecryptionError:
        raise
    except (ValueError, TypeError, KeyError) as exc:
        raise DecryptionError(f"malformed encrypted container: {exc}") from exc
    dek = _derive(passphrase, salt, n=n, r=r, p=p)
    try:
        return AESGCM(dek).decrypt(nonce, ciphertext, aad)
    except InvalidTag as exc:
        raise DecryptionError(
            "decryption failed: wrong key or the ciphertext was tampered with "
            "(authentication tag mismatch). The payload was NOT loaded."
        ) from exc

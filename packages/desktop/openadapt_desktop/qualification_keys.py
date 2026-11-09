"""Local Ed25519 identity for qualification-case attestations."""

from __future__ import annotations

from base64 import b64decode, b64encode

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# Look the keyring helpers up on the module at call time. Binding them by name
# would bypass the test suite's in-memory keyring and reach the OS keychain.
from openadapt_desktop.auth import store as _store

_ACCOUNT = "qualification-runner-ed25519-v1"
KEY_ID = "openadapt-desktop-local-v1"
RUNNER_ID = "openadapt-desktop-local"


class QualificationKeyError(RuntimeError):
    """The local qualification signer is unavailable."""


def qualification_signer() -> tuple[bytes, str]:
    """Return the protected raw private key and its base64 public key.

    The first call creates the key in the OS credential store.  No private key
    is written into a workflow, run directory, report, or application config.
    """

    kr = _store._keyring()
    encoded = _store._kr_get(kr, _ACCOUNT)
    if encoded:
        try:
            private_raw = b64decode(encoded, validate=True)
            private = Ed25519PrivateKey.from_private_bytes(private_raw)
        except (ValueError, TypeError) as exc:
            raise QualificationKeyError(
                "The protected qualification signing key is invalid."
            ) from exc
    else:
        private = Ed25519PrivateKey.generate()
        private_raw = private.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        if not _store._kr_set(kr, _ACCOUNT, b64encode(private_raw).decode("ascii")):
            raise QualificationKeyError(
                "This computer's protected credential store is unavailable; "
                "qualification evidence was not signed."
            )
    public_raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private_raw, b64encode(public_raw).decode("ascii")


"""The approver keys that sign a person's answer to a save.

Flow counts an answer from Desktop's Run it, or from the paired phone through
the decision portal, as a person's only when it carries an Ed25519 signature
by a key on Flow's signed approver roster, and the run pins that roster's
root (the deployment config's ``approvers.root_public_key``, or
``--approver-root``; see Flow's ``approval_mode/approver_roster``). Desktop
pins only the root kept in this Mac's Keychain.
Desktop keeps one such key
for each route (``desktop`` for Run it, ``portal`` for the phone's answers,
which the Flow console it starts signs). For each:

* the key itself, encrypted (PKCS#8, the same format as ``openadapt-flow jit
  keygen``), in an owner-only file under ``~/.openadapt/approver``;
* its random passphrase in this computer's protected credential store (the
  OS keychain), never in a file or on a command line.

:func:`setup_approver_key` creates both and returns the public key. A person
then starts the roster with the Desktop key as its root (``jit approver
init``), adds the portal key with the Desktop key's signature (``jit
approver add``), and pins the root for Desktop's runs. Without
a key and passphrase, :func:`approver_key_environment` returns ``None`` and
Desktop records no answer at all rather than an unsigned one.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# Look the keyring helpers up on the module at call time. Binding them by name
# would bypass the test suite's in-memory keyring and reach the OS keychain.
from openadapt_desktop.auth import store as _store
from openadapt_desktop.private_fs import ensure_private_dir

#: Where the encrypted keys live. Read at call time so tests can move it.
KEY_DIR = Path.home() / ".openadapt" / "approver"
#: The environment variable Flow reads a key's passphrase from.
PASSPHRASE_ENV = "OPENADAPT_JIT_KEY_PASSPHRASE"
#: The environment variable Flow's console reads the portal's key path from.
PORTAL_KEY_ENV = "OPENADAPT_PORTAL_APPROVER_KEY"

Route = Literal["desktop", "portal"]
#: route -> (key file, keychain account, suggested approver name)
_ROUTES: dict[str, tuple[str, str, str]] = {
    "desktop": (
        "desktop-approver.pem",
        "desktop-approver-key-passphrase-v1",
        "desktop-approver",
    ),
    "portal": (
        "portal-approver.pem",
        "portal-approver-key-passphrase-v1",
        "decision-portal",
    ),
}
DEFAULT_PRINCIPAL = _ROUTES["desktop"][2]


class ApproverKeyError(RuntimeError):
    """The Desktop approver key can't be created or opened."""


@dataclass(frozen=True)
class ApproverKeySetup:
    """A new or existing approver key, and how a person registers it."""

    key_path: Path
    public_key: str
    created: bool
    route: Route = "desktop"

    def register_command(
        self, principal: str | None = None, *, root: "ApproverKeySetup | None" = None
    ) -> str:
        """The command that puts this key on the approver roster.

        The Desktop key starts the roster as its root. Any other key (the
        portal's) is added with the root key's signature, so ``root`` is the
        Desktop key's setup.
        """

        name = principal or _ROUTES[self.route][2]
        if root is None:
            return (
                f"openadapt-flow jit approver init --principal {name} "
                f"--key {self.key_path}"
            )
        return (
            f"openadapt-flow jit approver add --principal {name} "
            f"--public-key {self.public_key} --signer-key {root.key_path} "
            f"--approver-root {root.public_key}"
        )


def approver_key_path(route: Route = "desktop") -> Path:
    return Path(KEY_DIR) / _ROUTES[route][0]


def _public_hex(key: Ed25519PrivateKey) -> str:
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        .hex()
    )


def _open(path: Path, passphrase: str) -> Ed25519PrivateKey:
    try:
        key = serialization.load_pem_private_key(
            path.read_bytes(), password=passphrase.encode("utf-8")
        )
    except (OSError, TypeError, ValueError) as exc:
        raise ApproverKeyError(
            "The Desktop approver key can't be opened with the passphrase in "
            "this computer's credential store."
        ) from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise ApproverKeyError("The Desktop approver key isn't an Ed25519 key.")
    return key


def setup_approver_key(route: Route = "desktop") -> ApproverKeySetup:
    """Create the approver key for ``route``, or return the one set up already.

    Fails closed when the OS credential store is unavailable: a key whose
    passphrase sits next to it would protect nothing.
    """

    path = approver_key_path(route)
    account = _ROUTES[route][1]
    kr = _store._keyring()
    passphrase = _store._kr_get(kr, account)
    if path.exists():
        if not passphrase:
            raise ApproverKeyError(
                f"{path} exists but its passphrase isn't in this computer's "
                "credential store. Move the file aside and set up a new key."
            )
        return ApproverKeySetup(path, _public_hex(_open(path, passphrase)), False, route)
    passphrase = secrets.token_urlsafe(32)
    if not _store._kr_set(kr, account, passphrase):
        raise ApproverKeyError(
            "This computer's protected credential store is unavailable, so no "
            "approver key was created."
        )
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(passphrase.encode("utf-8")),
    )
    ensure_private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(pem)
    return ApproverKeySetup(path, _public_hex(key), True, route)


def approver_key_environment(
    route: Route = "desktop",
) -> tuple[Path, dict[str, str]] | None:
    """The key path and the child-process environment that opens it.

    ``None`` when the key or its passphrase is missing. The passphrase goes
    only into the Flow child's environment, never onto its command line.
    """

    path = approver_key_path(route)
    if not path.is_file():
        return None
    # The keychain holds the passphrase of a key Desktop created. A key made
    # at a terminal with ``openadapt-flow jit keygen`` uses the passphrase in
    # Desktop's own environment, as the Flow CLI does.
    passphrase = _store._kr_get(_store._keyring(), _ROUTES[route][1]) or os.environ.get(
        PASSPHRASE_ENV
    )
    if not passphrase:
        return None
    return path, {PASSPHRASE_ENV: passphrase}


def sign_with_file_key(message: bytes, route: Route = "desktop") -> tuple[str, str] | None:
    """Sign one message with the terminal-made key, or ``None`` without one.

    Returns ``(public_key_hex, "ed25519:<hex>")``. The key is opened for this
    one signature and dropped.
    """

    found = approver_key_environment(route)
    if found is None:
        return None
    path, env = found
    key = _open(path, env[PASSPHRASE_ENV])
    try:
        return _public_hex(key), "ed25519:" + key.sign(message).hex()
    finally:
        del key


def setup_instructions(route: Route = "desktop") -> str:
    """The one-time terminal steps that make this computer's answers count.

    For computers where the app can't keep the key behind a presence check
    (Windows, Linux, a Mac without a Secure Enclave). On a Mac that can, the
    key is set up in OpenAdapt and a file key isn't used.
    """

    path = approver_key_path(route)
    name = _ROUTES[route][2]
    return (
        "Set up the approver key once, at a terminal: with "
        f"{PASSPHRASE_ENV} set, run `openadapt-flow jit keygen --out {path}`, "
        "then start the approver roster with it: "
        f"`openadapt-flow jit approver init --principal {name} --key {path}`. "
        "Pin the public key keygen printed as approvers.root_public_key in "
        "the deployment config you pass to `openadapt-flow run --config`, "
        f"with the same {PASSPHRASE_ENV}; or run "
        "`openadapt-desktop approver-key setup` instead of keygen to keep the "
        "passphrase in this computer's keychain. Desktop counts a person's "
        "answer only with a root pinned in this Mac's Keychain, which "
        "OpenAdapt sets up behind Touch ID."
    )


def portal_approver_environment() -> dict[str, str]:
    """The Flow console's environment for signing the phone's answers.

    Empty when the portal key isn't set up: the console then records no
    answer to a save (Flow refuses it), and everything else works as before.
    """

    found = approver_key_environment("portal")
    if found is None:
        return {}
    path, env = found
    return {PORTAL_KEY_ENV: str(path), **env}


__all__ = [
    "ApproverKeyError",
    "ApproverKeySetup",
    "approver_key_environment",
    "approver_key_path",
    "portal_approver_environment",
    "setup_approver_key",
    "setup_instructions",
    "sign_with_file_key",
]

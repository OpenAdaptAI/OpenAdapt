"""This Mac's approver key, behind Touch ID.

Flow counts an answer from Run it as a person's only when an Ed25519 key
registered under ``approvers`` signs it
(:mod:`openadapt_desktop.approver_key`). On a Mac, Desktop keeps that key
like this instead of in a file:

* A Secure Enclave key (P-256) with a user-presence access control: every use
  needs Touch ID or the login password, and the Secure Enclave enforces it, so
  a program running as this user can't use the key without the person seeing
  the prompt. The key never leaves the Secure Enclave; Desktop keeps only the
  Secure Enclave's encrypted handle to it.
* The Ed25519 key, sealed to that Secure Enclave key (ECIES, AES-GCM). It
  opens only on this Mac, and only after the prompt.
* Both, with the public key, as one item in the login Keychain.

Setup (:meth:`ApproverKeychain.setup`) is one screen and one prompt: Desktop
makes both keys, opens the sealed key once to prove the round trip and that a
person is there, and registers the public key under ``approvers`` with Flow's
own :func:`openadapt_flow.jit.authority.register_principal`, the function
``openadapt-flow jit register`` calls. Each answer
(:meth:`ApproverKeychain.sign`) prompts again, opens the key, signs one
message and drops it. The key is never written to disk unsealed, never logged
and never handed to Flow: Flow asks for the signature over a pipe
(``--approver-signer stdio``).

Everything fails closed (:class:`ApproverKeychainError`): no key, a cancelled
prompt, a Mac without a Secure Enclave or a Keychain error means no signature
and no answer.

The Keychain's own access-control flags would do the same job without the
Secure Enclave, but macOS honours them only for a signed app with a
keychain-access-groups entitlement (``errSecMissingEntitlement`` otherwise),
and the engine runs as a Python sidecar. The Secure Enclave needs no
entitlement.

Windows and Linux keep the terminal setup (``openadapt-desktop approver-key
setup``). The same design would need Windows Hello (``KeyCredentialManager``,
a TPM-backed key that asks for the PIN or face on each use) and, on Linux, a
Secret Service item plus a presence check, which Secret Service alone doesn't
give.
"""

from __future__ import annotations

import base64
import json
import secrets
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# Look the keyring helpers up on the module at call time, as approver_key
# does, so the test suite's in-memory keyring is used.
from openadapt_desktop.auth import store as _store

#: The login Keychain item (service ``ai.openadapt.desktop``) for Run it.
ACCOUNT = "desktop-approver-enclave-v1"
DEFAULT_PRINCIPAL = "desktop-approver"
#: Flow signs a person's answer under this domain
#: (``openadapt_flow.approval_mode.human_attestation.ANSWER_DOMAIN``).
ANSWER_DOMAIN = b"openadapt-flow.approval.human-answer/v1\x00"
#: What setup signs to prove the key opens behind a presence prompt.
SETUP_CHALLENGE = b"openadapt-desktop.approver-setup/v1\x00"
_SCHEMA = 1

Code = Literal[
    "approver_key_missing",
    "approver_presence_refused",
    "approver_key_unusable",
    "approver_keychain_unsupported",
    "approver_payload_refused",
]


class ApproverKeychainError(RuntimeError):
    """No signature: ``code`` says why, the message says what to do."""

    def __init__(self, code: Code, message: str) -> None:
        super().__init__(message)
        self.code: Code = code


class Enclave(Protocol):
    """The Secure Enclave operations Desktop needs (faked in tests)."""

    def available(self) -> bool: ...

    def create(self, *, require_presence: bool) -> tuple[bytes, bytes]:
        """A new key: (its encrypted handle, its public key in X9.63)."""
        ...

    def seal(self, public_key: bytes, plaintext: bytes) -> bytes: ...

    def open(
        self, handle: bytes, sealed: bytes, *, reason: str, interactive: bool = True
    ) -> bytearray:
        """Unseal, asking for presence. Raises :class:`ApproverKeychainError`."""
        ...


@dataclass(frozen=True)
class StoredKey:
    """The Keychain item: nothing in it opens without the Secure Enclave."""

    public_key: str
    principal: str
    handle: bytes
    sealed: bytes
    presence: bool
    created_at: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema": _SCHEMA,
                "public_key": self.public_key,
                "principal": self.principal,
                "enclave_key": base64.b64encode(self.handle).decode("ascii"),
                "sealed_key": base64.b64encode(self.sealed).decode("ascii"),
                "presence": self.presence,
                "created_at": self.created_at,
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, text: str) -> "StoredKey":
        data = json.loads(text)
        if not isinstance(data, dict) or data.get("schema") != _SCHEMA:
            raise ValueError("unknown approver key format")
        return cls(
            public_key=str(data["public_key"]),
            principal=str(data["principal"]),
            handle=base64.b64decode(data["enclave_key"], validate=True),
            sealed=base64.b64decode(data["sealed_key"], validate=True),
            presence=bool(data["presence"]),
            created_at=str(data["created_at"]),
        )


#: ``sign(message) -> "ed25519:<hex>"`` with an approver key, behind presence.
Sign = Callable[[bytes], str]
#: The login Keychain item holding the approver root this Mac pins.
ROOT_ACCOUNT = "desktop-approver-root-v1"
#: The roster head (seq, hash) after this Mac's last change to the roster. A
#: roster that doesn't extend it (an older copy put back) is refused.
HEAD_ACCOUNT = "desktop-approver-roster-head-v1"
ROOT_ENV = "OPENADAPT_APPROVER_ROOT"


class Roster(Protocol):
    """Flow's signed approver roster, as Desktop uses it (faked in tests)."""

    def root(self) -> str | None:
        """The root this Mac pins, or ``None``."""
        ...

    def is_empty(self) -> bool: ...

    def lookup(self, public_key: str) -> str | None:
        """The approver name the pinned root vouches for this key under."""
        ...

    def init_root(self, principal: str, public_key: str, sign: Sign) -> None: ...

    def add(self, principal: str, public_key: str, signed_by: str, sign: Sign) -> None: ...

    def remove(self, principal: str, public_key: str, signed_by: str, sign: Sign) -> None: ...


def _flow_roster():
    try:
        from openadapt_flow.approval_mode import approver_roster
        from openadapt_flow.jit import lifecycle
    except ImportError as exc:
        raise ApproverKeychainError(
            "approver_key_unusable",
            "This Desktop's OpenAdapt Flow can't keep a signed approver roster. "
            "Update Desktop.",
        ) from exc
    return approver_roster, lifecycle.default_key_dir()


class FlowRoster:
    """The roster in Flow's key directory, pinned by this Mac.

    The pin is the root Desktop set up itself, behind Touch ID, kept in the
    login Keychain. An administrator's root in Desktop's own deployment config
    (``approvers.root_public_key`` in ``deployment.json``) counts for a Mac
    that didn't start the roster, or whose Keychain pin is gone; one that
    differs from the Keychain's is refused. ``OPENADAPT_APPROVER_ROOT`` never
    counts.
    Desktop never adopts the root of a roster it finds: that would trust
    whoever wrote the file first. The roster must also extend the head this
    Mac recorded after its own last change.
    """

    def root(self) -> str | None:
        stored = _store._kr_get(_store._keyring(), ROOT_ACCOUNT)
        kept = stored.strip().lower() if stored else None
        admin = _admin_root()
        if kept and admin and admin != kept:
            raise ApproverKeychainError(
                "approver_root_conflict",
                "Desktop's deployment config names another approver root than "
                "the one this Mac set up. Nothing was approved.",
            )
        # The environment never counts: whoever starts a process sets it. A
        # pin deleted from the Keychain leaves only an administrator's.
        return kept or admin

    def _head(self) -> tuple[int, str] | None:
        raw = _store._kr_get(_store._keyring(), HEAD_ACCOUNT)
        try:
            seq, digest = json.loads(raw) if raw else (None, None)
        except (ValueError, TypeError):
            raise ApproverKeychainError(
                "approver_key_unusable", "The recorded approver roster head is unreadable."
            ) from None
        return (int(seq), str(digest)) if seq else None

    def _record_head(self) -> None:
        roster, key_dir = _flow_roster()
        seq, digest = roster.head(key_dir, self.root())
        if not _store._kr_set(_store._keyring(), HEAD_ACCOUNT, json.dumps([seq, digest])):
            raise ApproverKeychainError(
                "approver_key_unusable", "The login Keychain didn't save the roster head."
            )

    def is_empty(self) -> bool:
        roster, key_dir = _flow_roster()
        try:
            return not roster.load(key_dir).entries
        except roster.RosterError:
            return False  # unreadable: never start a new root over it

    def lookup(self, public_key: str) -> str | None:
        try:
            roster, key_dir = _flow_roster()
            found = roster.verified_approvers(key_dir, self.root(), at_least=self._head())
        except (ApproverKeychainError, ValueError, OSError):
            return None
        return next((pid for pid, value in found.items() if value == public_key), None)

    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        roster, key_dir = _flow_roster()
        try:
            return getattr(roster, name)(key_dir, *args, **kwargs)
        except (roster.RosterError, OSError, ValueError) as exc:
            raise ApproverKeychainError(
                "approver_key_unusable", f"Flow refused the roster change: {exc}"
            ) from exc

    def init_root(self, principal: str, public_key: str, sign: Sign) -> None:
        if self.root():
            raise ApproverKeychainError(
                "approver_root_conflict",
                "This Mac already pins an approver root; it doesn't start another.",
            )
        self._call("init_root", principal=principal, public_key=public_key, sign=sign)
        # This Mac set up the root itself, behind presence: pin it.
        if not _store._kr_set(_store._keyring(), ROOT_ACCOUNT, public_key):
            raise ApproverKeychainError(
                "approver_key_unusable",
                "The login Keychain didn't save the approver root.",
            )
        self._record_head()

    def add(self, principal: str, public_key: str, signed_by: str, sign: Sign) -> None:
        self._call(
            "add_approver",
            self.root(),
            principal=principal,
            public_key=public_key,
            signed_by=signed_by,
            sign=sign,
            at_least=self._head(),
        )
        self._record_head()

    def remove(self, principal: str, public_key: str, signed_by: str, sign: Sign) -> None:
        self._call(
            "remove_approver",
            self.root(),
            principal=principal,
            public_key=public_key,
            signed_by=signed_by,
            sign=sign,
            at_least=self._head(),
        )
        self._record_head()


def _admin_root() -> str | None:
    """An administrator's approver root from Desktop's own deployment config.

    ``approvers.root_public_key`` in ``<data_dir>/deployment.json``: for a Mac
    whose key an approver added to a roster it didn't start. Unreadable
    means refused, not ignored.
    """

    from openadapt_desktop.config import EngineConfig

    path = Path(EngineConfig().data_dir) / "deployment.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ApproverKeychainError(
            "approver_key_unusable", f"Desktop's deployment config is unreadable: {exc}"
        ) from exc
    section = data.get("approvers") if isinstance(data, dict) else None
    value = section.get("root_public_key") if isinstance(section, dict) else None
    return str(value).strip().lower() if value else None


def pinned_root() -> str | None:
    """The approver root this Mac pins for its Flow runs, or ``None``.

    Raises :class:`ApproverKeychainError` when ``OPENADAPT_APPROVER_ROOT``
    disagrees with the Keychain's pin: Desktop then starts no run that a
    person would answer.
    """

    return FlowRoster().root()


def pinned_head() -> tuple[int, str] | None:
    """The roster head this Mac recorded after its own last roster change."""

    return FlowRoster()._head()


def registered_name(public_key: str) -> str | None:
    """The approver name the pinned roster has for this key, or ``None``."""

    return FlowRoster().lookup(public_key)


def _public_hex(key: Ed25519PrivateKey) -> str:
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        .hex()
    )


def _wipe(buffer: bytearray) -> None:
    for index in range(len(buffer)):
        buffer[index] = 0


class ApproverKeychain:
    """Set up, use, rotate and remove this Mac's approver key.

    ``require_presence=False`` exists only for the real-Keychain test, which
    can't touch the sensor. A key stored that way can't be used by an instance
    that requires presence, which is every instance Desktop makes.
    """

    def __init__(
        self,
        enclave: Enclave | None = None,
        *,
        account: str = ACCOUNT,
        require_presence: bool = True,
        roster: Roster | None = None,
    ) -> None:
        self._enclave = enclave if enclave is not None else default_enclave()
        self._account = account
        self._presence = require_presence
        self._roster: Roster = roster if roster is not None else FlowRoster()
        self._lock = threading.Lock()

    # -- state ------------------------------------------------------------------

    def supported(self) -> bool:
        return self._enclave.available()

    def stored(self) -> StoredKey | None:
        raw = _store._kr_get(_store._keyring(), self._account)
        if not raw:
            return None
        try:
            return StoredKey.from_json(raw)
        except (ValueError, KeyError, TypeError):
            return None

    def status(self) -> dict:
        """What the setup screen shows. Never includes the key itself."""

        supported = self.supported()
        stored = self.stored() if supported else None
        registered = (
            stored is not None and self._roster.lookup(stored.public_key) == stored.principal
        )
        return {
            "supported": supported,
            "set_up": stored is not None and registered,
            "stored": stored is not None,
            "registered": registered,
            "principal": stored.principal if stored else None,
            "fingerprint": stored.public_key[:16] if stored else None,
        }

    # -- setup, rotation, removal -------------------------------------------------

    def setup(self, principal: str = DEFAULT_PRINCIPAL, *, replace: bool = False) -> StoredKey:
        """Make the key and put it on the signed roster behind presence.

        * No roster yet: the new key signs the roster's root, behind the one
          Touch ID prompt, and this Mac pins that root. The signature is the
          proof that a person was present.
        * ``replace`` (rotation): the old key signs the new one onto the
          roster (a prompt each for the new and the old key), then the old
          one is removed unless it is the root.
        * A roster this Mac didn't start: an approver on it adds this key.
          The key is kept, and the refusal says how.
        """

        with self._lock:
            if not self.supported():
                raise ApproverKeychainError(
                    "approver_keychain_unsupported",
                    "This Mac has no Secure Enclave, so Desktop can't keep the "
                    "approver key behind Touch ID. Set it up at a terminal "
                    "instead.",
                )
            old = self.stored()
            if old is not None and not replace:
                if self._roster.lookup(old.public_key) != old.principal:
                    # Stored but not on the roster. A Keychain item alone
                    # proves nothing (anything that can write it could have
                    # put its own key there): only a signature made behind a
                    # presence prompt starts a roster.
                    if self._presence and not old.presence:
                        raise ApproverKeychainError(
                            "approver_key_unusable",
                            "The stored approver key doesn't ask for Touch ID. "
                            "Set up a new one.",
                        )
                    self._join(old, signer=None)
                return old
            new = self._make(principal if old is None else f"{principal}-{secrets.token_hex(3)}")
            if not _store._kr_set(_store._keyring(), self._account, new.to_json()):
                raise ApproverKeychainError(
                    "approver_key_unusable",
                    "The login Keychain didn't save the approver key. Nothing was set up.",
                )
            signer = old if old is not None and self._roster.lookup(old.public_key) else None
            try:
                self._join(new, signer=signer)
            except ApproverKeychainError as exc:
                if exc.code != "approver_needs_adding":
                    if old is not None:
                        _store._kr_set(_store._keyring(), self._account, old.to_json())
                    else:
                        _store._kr_delete(_store._keyring(), self._account)
                raise
            if signer is not None and self._roster.root() != signer.public_key:
                # Retire the old key; the root can't be removed, it just stops
                # being used here.
                self._roster.remove(
                    signer.principal,
                    signer.public_key,
                    new.principal,
                    self._signer(new, "retire your old approver key"),
                )
            return new

    def _make(self, principal: str) -> StoredKey:
        handle, enclave_public = self._enclave.create(require_presence=self._presence)
        key = Ed25519PrivateKey.generate()
        public = _public_hex(key)
        seed = bytearray(
            key.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
        )
        del key
        try:
            sealed = self._enclave.seal(enclave_public, bytes(seed))
        finally:
            _wipe(seed)
        return StoredKey(
            public_key=public,
            principal=principal,
            handle=handle,
            sealed=sealed,
            presence=self._presence,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def _signer(self, stored: StoredKey, reason: str) -> Sign:
        return lambda message: self._sign_with(stored, message, reason=reason)

    def _join(self, key: StoredKey, *, signer: StoredKey | None) -> None:
        """Put ``key`` on the roster: as its root, or added by ``signer``."""

        if self._roster.is_empty():
            # The one prompt: the key signs the roster's root, which proves it
            # opens behind presence and that a person is here.
            self._roster.init_root(
                key.principal, key.public_key, self._signer(key, "set up your approver key")
            )
            return
        if signer is None:
            root = self._roster.root()
            raise ApproverKeychainError(
                "approver_needs_adding",
                "This computer's approver roster already has approvers, so one "
                "of them adds this Mac's key: openadapt-flow jit approver add "
                f"--principal {key.principal} --public-key {key.public_key} "
                "--signer-key THEIR_KEY"
                + (f" --approver-root {root}" if root else " --approver-root ROOT_HEX")
                + ". Nothing was approved with it yet.",
            )
        # Prove the new key opens behind presence, then the old key adds it.
        self._sign_with(
            key,
            SETUP_CHALLENGE + key.public_key.encode(),
            reason="set up your new approver key",
        )
        self._roster.add(
            key.principal,
            key.public_key,
            signer.principal,
            self._signer(signer, "approve your new approver key"),
        )

    def remove(self) -> bool:
        """Retire the key: take it off the roster (its own signature, behind
        presence), then delete it from the Keychain. The roster's root can't
        be removed; its key is only deleted here."""

        with self._lock:
            old = self.stored()
            if old is None:
                return False
            if (
                self._roster.lookup(old.public_key) == old.principal
                and self._roster.root() != old.public_key
            ):
                self._roster.remove(
                    old.principal,
                    old.public_key,
                    old.principal,
                    self._signer(old, "remove your approver key"),
                )
            _store._kr_delete(_store._keyring(), self._account)
            return True

    # -- signing ------------------------------------------------------------------

    def sign(self, message: bytes, *, reason: str) -> tuple[str, str]:
        """Sign one message after the presence prompt.

        Returns ``(public_key_hex, "ed25519:<hex>")``. Raises
        :class:`ApproverKeychainError`.
        """

        with self._lock:
            stored = self.stored()
            if stored is None:
                raise ApproverKeychainError(
                    "approver_key_missing",
                    "Set up your approver key to approve saves on this Mac.",
                )
            if self._presence and not stored.presence:
                raise ApproverKeychainError(
                    "approver_key_unusable",
                    "The stored approver key doesn't ask for Touch ID. Set up a new one.",
                )
            return stored.public_key, self._sign_with(stored, message, reason=reason)

    def _sign_with(self, stored: StoredKey, message: bytes, *, reason: str) -> str:
        seed = self._enclave.open(stored.handle, stored.sealed, reason=reason)
        try:
            if len(seed) != 32:
                raise ApproverKeychainError(
                    "approver_key_unusable", "The approver key didn't open."
                )
            key = Ed25519PrivateKey.from_private_bytes(seed)
        finally:
            _wipe(seed)
        try:
            if _public_hex(key) != stored.public_key:
                raise ApproverKeychainError(
                    "approver_key_unusable",
                    "The approver key in the Keychain doesn't match its public key.",
                )
            return "ed25519:" + key.sign(message).hex()
        finally:
            del key


_DEFAULT: list[ApproverKeychain] = []


def default_keychain() -> ApproverKeychain:
    """The engine's one approver keychain (one lock for every caller)."""

    if not _DEFAULT:
        _DEFAULT.append(ApproverKeychain())
    return _DEFAULT[0]


# -- the Secure Enclave, through Security.framework --------------------------------


def default_enclave() -> Enclave:
    if sys.platform == "darwin":
        return MacEnclave()
    return NoEnclave()


class NoEnclave:
    """Windows and Linux: no in-app key (see the module docstring)."""

    def available(self) -> bool:
        return False

    def create(self, *, require_presence: bool) -> tuple[bytes, bytes]:
        raise ApproverKeychainError(
            "approver_keychain_unsupported",
            "In-app approver keys are on macOS only. Set it up at a terminal.",
        )

    def seal(self, public_key: bytes, plaintext: bytes) -> bytes:
        raise ApproverKeychainError("approver_keychain_unsupported", "not on this OS")

    def open(
        self, handle: bytes, sealed: bytes, *, reason: str, interactive: bool = True
    ) -> bytearray:
        raise ApproverKeychainError("approver_keychain_unsupported", "not on this OS")


# LocalAuthentication and Security error codes that mean the person didn't
# confirm: cancelled, failed, locked out, no UI allowed, or no passcode.
_PRESENCE_REFUSED = frozenset({-1, -2, -4, -5, -6, -7, -8, -9, -1004, -128})
#: errSecInteractionNotAllowed: the Mac is locked (or no prompt can show).
_INTERACTION_NOT_ALLOWED = -25308


class MacEnclave:
    """Security.framework through ctypes.

    PyObjC can't wrap a Secure Enclave key reference (its CF type lookup
    raises inside the call), so the key calls go through ctypes and handle
    Core Foundation objects as pointers. Every object this creates is
    released; the unsealed key's buffer is zeroed before release.
    """

    _SEC = "/System/Library/Frameworks/Security.framework/Security"
    _CF = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    _OBJC = "/usr/lib/libobjc.A.dylib"
    _LA = "/System/Library/Frameworks/LocalAuthentication.framework/LocalAuthentication"
    _PRIVATE_KEY_USAGE = 1 << 30
    _USER_PRESENCE = 1 << 0

    def __init__(self) -> None:
        self._lib = None
        self._available: bool | None = None

    # ctypes setup is lazy so importing this module costs nothing elsewhere.
    def _l(self):
        if self._lib is None:
            self._lib = _MacLib(self._SEC, self._CF, self._OBJC, self._LA)
        return self._lib

    def available(self) -> bool:
        if self._available is None:
            try:
                handle, _public = self.create(require_presence=False)
                self._available = bool(handle)
            except ApproverKeychainError as exc:
                if exc.code == "approver_presence_refused":
                    # The Mac is locked: the Secure Enclave is there but won't
                    # make a key now. Don't remember that; ask again later.
                    return True
                self._available = False
            except Exception:  # no Secure Enclave, or Security.framework differs
                self._available = False
        return self._available

    def create(self, *, require_presence: bool) -> tuple[bytes, bytes]:
        lib = self._l()
        flags = self._PRIVATE_KEY_USAGE | (self._USER_PRESENCE if require_presence else 0)
        with lib.scope() as keep:
            access = keep(
                lib.call(
                    "SecAccessControlCreateWithFlags",
                    None,
                    lib.const("kSecAttrAccessibleWhenUnlockedThisDeviceOnly"),
                    flags,
                )
            )
            private = keep(
                lib.dictionary(
                    [
                        (lib.const("kSecAttrIsPermanent"), lib.false),
                        (lib.const("kSecAttrAccessControl"), access),
                    ]
                )
            )
            attrs = keep(
                lib.dictionary(lib.key_attrs(keep) + [(lib.const("kSecPrivateKeyAttrs"), private)])
            )
            key = keep(lib.call("SecKeyCreateRandomKey", attrs))
            info = keep(lib.raw("SecKeyCopyAttributes", key))
            handle = lib.data_bytes(lib.dict_get(info, keep(lib.string("toid"))))
            public_ref = keep(lib.raw("SecKeyCopyPublicKey", key))
            public = lib.data_bytes(keep(lib.call("SecKeyCopyExternalRepresentation", public_ref)))
        if not handle or not public:
            raise ApproverKeychainError(
                "approver_key_unusable", "The Secure Enclave didn't make a key."
            )
        return handle, public

    def seal(self, public_key: bytes, plaintext: bytes) -> bytes:
        lib = self._l()
        with lib.scope() as keep:
            attrs = keep(
                lib.dictionary(
                    [
                        (
                            lib.const("kSecAttrKeyType"),
                            lib.const("kSecAttrKeyTypeECSECPrimeRandom"),
                        ),
                        (lib.const("kSecAttrKeyClass"), lib.const("kSecAttrKeyClassPublic")),
                        (lib.const("kSecAttrKeySizeInBits"), keep(lib.number(256))),
                    ]
                )
            )
            public = keep(lib.call("SecKeyCreateWithData", keep(lib.data(public_key)), attrs))
            sealed = keep(
                lib.call(
                    "SecKeyCreateEncryptedData",
                    public,
                    lib.algorithm,
                    keep(lib.data(plaintext)),
                )
            )
            return lib.data_bytes(sealed)

    def open(
        self, handle: bytes, sealed: bytes, *, reason: str, interactive: bool = True
    ) -> bytearray:
        lib = self._l()
        with lib.scope() as keep:
            context = lib.la_context(reason, interactive)
            keep(context)
            private = keep(lib.dictionary([(lib.const("kSecAttrIsPermanent"), lib.false)]))
            attrs = keep(
                lib.dictionary(
                    lib.key_attrs(keep)
                    + [
                        (keep(lib.string("toid")), keep(lib.data(handle))),
                        (lib.const("kSecUseAuthenticationContext"), context),
                        (lib.const("kSecPrivateKeyAttrs"), private),
                    ]
                )
            )
            key = keep(lib.call("SecKeyCreateRandomKey", attrs))
            plain = lib.call(
                "SecKeyCreateDecryptedData", key, lib.algorithm, keep(lib.data(sealed))
            )
            try:
                return bytearray(lib.data_bytes(plain))
            finally:
                lib.wipe_data(plain)
                lib.release(plain)


class _MacLib:
    """The few Core Foundation, Security and Objective-C calls MacEnclave makes."""

    def __init__(self, sec_path: str, cf_path: str, objc_path: str, la_path: str) -> None:
        import ctypes

        self.ctypes = ctypes
        vp = ctypes.c_void_p
        self.sec = ctypes.CDLL(sec_path)
        self.cf = ctypes.CDLL(cf_path)
        self.objc = ctypes.CDLL(objc_path)
        ctypes.CDLL(la_path)  # registers LAContext with the runtime
        err = ctypes.POINTER(vp)
        self._sigs = {
            "SecAccessControlCreateWithFlags": (self.sec, [vp, vp, ctypes.c_ulong, err]),
            "SecKeyCreateRandomKey": (self.sec, [vp, err]),
            "SecKeyCopyExternalRepresentation": (self.sec, [vp, err]),
            "SecKeyCreateWithData": (self.sec, [vp, vp, err]),
            "SecKeyCreateEncryptedData": (self.sec, [vp, vp, vp, err]),
            "SecKeyCreateDecryptedData": (self.sec, [vp, vp, vp, err]),
            "SecKeyCopyAttributes": (self.sec, [vp]),
            "SecKeyCopyPublicKey": (self.sec, [vp]),
        }
        for name, (lib, args) in self._sigs.items():
            fn = getattr(lib, name)
            fn.restype, fn.argtypes = vp, args
        for name, res, args in (
            ("CFRelease", None, [vp]),
            ("CFDataCreate", vp, [vp, ctypes.c_char_p, ctypes.c_long]),
            ("CFDataGetLength", ctypes.c_long, [vp]),
            ("CFDataGetBytePtr", vp, [vp]),
            ("CFDictionaryCreateMutable", vp, [vp, ctypes.c_long, vp, vp]),
            ("CFDictionarySetValue", None, [vp, vp, vp]),
            ("CFDictionaryGetValue", vp, [vp, vp]),
            ("CFNumberCreate", vp, [vp, ctypes.c_long, vp]),
            ("CFStringCreateWithCString", vp, [vp, ctypes.c_char_p, ctypes.c_uint32]),
            ("CFErrorGetCode", ctypes.c_long, [vp]),
        ):
            fn = getattr(self.cf, name)
            fn.restype, fn.argtypes = res, args
        self.objc.objc_getClass.restype = vp
        self.objc.objc_getClass.argtypes = [ctypes.c_char_p]
        self.objc.sel_registerName.restype = vp
        self.objc.sel_registerName.argtypes = [ctypes.c_char_p]
        address = ctypes.cast(self.objc.objc_msgSend, vp).value
        self._send = ctypes.CFUNCTYPE(vp, vp, vp)(address)
        self._send_bool = ctypes.CFUNCTYPE(None, vp, vp, ctypes.c_bool)(address)
        self._send_ptr = ctypes.CFUNCTYPE(None, vp, vp, vp)(address)
        self._keys = ctypes.addressof(
            ctypes.c_char.in_dll(self.cf, "kCFTypeDictionaryKeyCallBacks")
        )
        self._values = ctypes.addressof(
            ctypes.c_char.in_dll(self.cf, "kCFTypeDictionaryValueCallBacks")
        )
        self.false = vp.in_dll(self.cf, "kCFBooleanFalse").value
        self.algorithm = self.const(
            "kSecKeyAlgorithmECIESEncryptionCofactorVariableIVX963SHA256AESGCM"
        )

    def const(self, name: str):
        return self.ctypes.c_void_p.in_dll(self.sec, name).value

    def release(self, ref) -> None:
        if ref:
            self.cf.CFRelease(ref)

    def scope(self):
        lib = self

        class _Scope:
            def __enter__(self):
                self.refs: list = []

                def keep(ref):
                    self.refs.append(ref)
                    return ref

                return keep

            def __exit__(self, *_exc):
                for ref in reversed(self.refs):
                    lib.release(ref)

        return _Scope()

    def call(self, name: str, *args):
        """A Security call that reports a CFError; raises on failure."""

        error = self.ctypes.c_void_p()
        result = getattr(self.sec, name)(*args, self.ctypes.byref(error))
        if not result:
            code = self.cf.CFErrorGetCode(error) if error.value else 0
            self.release(error.value)
            if code == _INTERACTION_NOT_ALLOWED:
                raise ApproverKeychainError(
                    "approver_presence_refused",
                    "This Mac is locked or can't show the Touch ID prompt right "
                    "now. Unlock it and try again. Nothing changed.",
                )
            if code in _PRESENCE_REFUSED:
                raise ApproverKeychainError(
                    "approver_presence_refused",
                    "Touch ID or your password wasn't confirmed, so nothing was approved.",
                )
            raise ApproverKeychainError(
                "approver_key_unusable",
                f"The Secure Enclave refused ({name}, error {code}).",
            )
        return result

    def raw(self, name: str, *args):
        result = getattr(self.sec, name)(*args)
        if not result:
            raise ApproverKeychainError(
                "approver_key_unusable", f"The Secure Enclave refused ({name})."
            )
        return result

    def key_attrs(self, keep) -> list:
        return [
            (self.const("kSecAttrKeyType"), self.const("kSecAttrKeyTypeECSECPrimeRandom")),
            (self.const("kSecAttrKeySizeInBits"), keep(self.number(256))),
            (self.const("kSecAttrTokenID"), self.const("kSecAttrTokenIDSecureEnclave")),
        ]

    def dictionary(self, pairs: list):
        ref = self.cf.CFDictionaryCreateMutable(None, 0, self._keys, self._values)
        for key, value in pairs:
            self.cf.CFDictionarySetValue(ref, key, value)
        return ref

    def dict_get(self, ref, key):
        return self.cf.CFDictionaryGetValue(ref, key)

    def number(self, value: int):
        number = self.ctypes.c_int32(value)
        return self.cf.CFNumberCreate(None, 3, self.ctypes.byref(number))

    def string(self, text: str):
        return self.cf.CFStringCreateWithCString(None, text.encode("utf-8"), 0x08000100)

    def data(self, value: bytes):
        return self.cf.CFDataCreate(None, value, len(value))

    def data_bytes(self, ref) -> bytes:
        if not ref:
            return b""
        return self.ctypes.string_at(self.cf.CFDataGetBytePtr(ref), self.cf.CFDataGetLength(ref))

    def wipe_data(self, ref) -> None:
        if ref:
            self.ctypes.memset(self.cf.CFDataGetBytePtr(ref), 0, self.cf.CFDataGetLength(ref))

    def la_context(self, reason: str, interactive: bool):
        """An LAContext with the prompt's reason; +1, released by the scope."""

        sel = self.objc.sel_registerName
        cls = self.objc.objc_getClass(b"LAContext")
        context = self._send(self._send(cls, sel(b"alloc")), sel(b"init"))
        if not context:
            raise ApproverKeychainError(
                "approver_key_unusable", "LocalAuthentication isn't available."
            )
        self._send_bool(context, sel(b"setInteractionNotAllowed:"), not interactive)
        text = self.string(reason)
        self._send_ptr(context, sel(b"setLocalizedReason:"), text)
        self.release(text)
        return context


__all__ = [
    "ACCOUNT",
    "ANSWER_DOMAIN",
    "ApproverKeychain",
    "ApproverKeychainError",
    "DEFAULT_PRINCIPAL",
    "Enclave",
    "MacEnclave",
    "NoEnclave",
    "StoredKey",
    "default_enclave",
    "default_keychain",
    "FlowRoster",
    "HEAD_ACCOUNT",
    "ROOT_ACCOUNT",
    "Roster",
    "pinned_head",
    "pinned_root",
    "registered_name",
]

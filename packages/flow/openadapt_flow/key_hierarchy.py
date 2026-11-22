"""Who can open an encrypted run: the key hierarchy behind sealed-set v3.

See docs/phi_at_rest.md ("Key hierarchy"). The layers::

    data key (DEK, 256-bit, one per sealed set)
      wrapped by HPKE (P-256, HKDF-SHA256, AES-256-GCM) to
    a bucket key pair (P-256, one per set)
      whose private half is wrapped, in a *bucket record*, to
        the device runtime key   (only while the set is being written)
        the device viewer key    (user presence on hardware)
        organization key(s)      (P-256 HPKE or RSA-OAEP-3072; required in
                                  organization mode)
        a recovery key           (solo mode, optional)

The sealed-set header carries only the DEK wrapped to the bucket's public
key. The bucket record is what crypto-shredding destroys:

* **Organization mode** (``records: archive``): the record lives in
  ``$OPENADAPT_HOME/keys/buckets/`` and a copy goes to the organization's key
  archive. Shredding deletes both; backups of the evidence then hold only
  ciphertext no key opens.
* **Solo mode** (``records: inline``): the record travels inside the header,
  so a recovery key can open backups. Shredding reaches only copies you can
  delete.

Device keys come from :class:`DeviceKeyProvider` implementations. The
hardware providers (Secure Enclave, TPM, Touch ID or Windows Hello) are
interfaces here with test doubles; they report themselves unavailable until
they are built and checked on real hardware, and Flow then uses a *software*
P-256 key kept in the OS keychain (or a 0600 file), labelled as such in
``at-rest status``. ``require_hardware_device_key`` in the policy refuses
that fallback.

No key, wrapped key or recovery material ever goes to OpenAdapt Cloud, and
the organization's public key comes only from a machine policy (MDM, Group
Policy, a root-owned file), never from Cloud.

Primitives: HPKE, ECDH P-256, RSA-OAEP, HKDF and AES-GCM from the
``cryptography`` package. Nothing here is a new primitive.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import stat
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Iterator, Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

BUCKET_SCHEMA: Final[str] = "openadapt.ai/bucket-record/v1"
POLICY_SCHEMA: Final[str] = "openadapt.ai/policy/v1"
SHRED_SCHEMA: Final[str] = "openadapt.ai/shred-record/v1"
HPKE_P256: Final[str] = "hpke-p256"
RSA_OAEP_3072: Final[str] = "rsa-oaep-3072-sha256"
STANZA_TYPE: Final[str] = "hpke-p256-aes256gcm"

POLICY_ENV: Final[str] = "OPENADAPT_POLICY"
#: A user-owned policy file may set organization mode only with this set
#: (development and tests); machine policies never need it.
TRUST_USER_POLICY_ENV: Final[str] = "OPENADAPT_POLICY_TRUST_USER_FILE"
RECOVERY_CODE_ENV: Final[str] = "OPENADAPT_RECOVERY_CODE"
#: ``1``: the person is restoring backups with the recovery code, so a set
#: that only the code opens may be read (``at-rest restore`` rewraps one to
#: this computer instead). Never set by Flow itself.
RECOVERY_RESTORE_ENV: Final[str] = "OPENADAPT_RECOVERY_RESTORE"
#: A PEM private key of an organization or break-glass recipient, for a
#: reviewer opening evidence without a KMS (KMS unwrap is not built yet).
ORG_KEY_FILE_ENV: Final[str] = "OPENADAPT_ORG_KEY_FILE"

ROLE_RUNTIME: Final[str] = "runtime"
ROLE_VIEWER: Final[str] = "viewer"
ROLE_DEVICE_SOFTWARE: Final[str] = "device-software"
ROLE_ORG: Final[str] = "org"
ROLE_BREAK_GLASS: Final[str] = "break-glass"
ROLE_RECOVERY: Final[str] = "recovery"


def _platform_policy_sources(platform: str) -> list[str]:
    """The machine-scope policy sources this platform trusts. Each platform
    reads only its own administrator-controlled sources: a macOS managed
    preference path means nothing on Windows, where a user could create it."""
    if platform == "darwin":
        return [
            "/Library/Managed Preferences/ai.openadapt.policy.plist",
            "/etc/openadapt/policy.json",
        ]
    if platform == "win32":
        return ["HKLM\\SOFTWARE\\Policies\\OpenAdapt"]
    return ["/etc/openadapt/policy.json"]


#: Machine-scope policy locations, first match wins. Tests replace this.
MACHINE_POLICY_SOURCES: list[str] = _platform_policy_sources(sys.platform)
#: The file an administrator puts at the root of ``key_archive``. Flow
#: treats the archive as reachable only when it is there, so an unmounted
#: share (an empty local folder at the mount point) never counts as the
#: archive, and a record missing from it never counts as deleted.
ARCHIVE_MARKER: Final[str] = ".openadapt-key-archive"

_SUITE_LABEL: Final[bytes] = b"openadapt.ai/bucket/v1/wrap\0"
_RECOVERY_SALT: Final[bytes] = b"openadapt.ai/recovery-key/v1"


class KeyHierarchyError(ValueError):
    """A policy, recipient or bucket record is missing, malformed or refused."""


class NoRecipientKey(KeyHierarchyError):
    """No key this computer holds opens the bucket record."""


class RecoveryRestoreRequired(NoRecipientKey):
    """Only the recovery code opens this set, and it was not written on this
    computer (or its device key is gone): it opens only through an explicit
    restore (``at-rest restore``), never silently."""


# -- primitives --------------------------------------------------------------


def _suite() -> Any:
    from cryptography.hazmat.primitives import hpke

    return hpke.Suite(hpke.KEM.P256, hpke.KDF.HKDF_SHA256, hpke.AEAD.AES_256_GCM)


def _spki(public: Any) -> bytes:
    return public.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def fingerprint(public: Any) -> str:
    return "sha256:" + hashlib.sha256(_spki(public)).hexdigest()


def hpke_seal(
    public: ec.EllipticCurvePublicKey, plaintext: bytes, info: bytes
) -> bytes:
    return bytes(_suite().encrypt(plaintext, public, info))


def hpke_open(private: ec.EllipticCurvePrivateKey, sealed: bytes, info: bytes) -> bytes:
    return bytes(_suite().decrypt(sealed, private, info))


def _scalar(private: ec.EllipticCurvePrivateKey) -> bytes:
    return private.private_numbers().private_value.to_bytes(32, "big")


def _from_scalar(raw: bytes) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: Any) -> bytes:
    if not isinstance(text, str):
        raise KeyHierarchyError("a bucket record field is not base64 text")
    return base64.b64decode(text, validate=True)


# -- recipients ----------------------------------------------------------------


@dataclass(frozen=True)
class Recipient:
    """A public key the bucket's private half is wrapped to."""

    role: str
    type: str
    public_key: Any
    status: str = "active"
    kms_uri: Optional[str] = None

    @property
    def fpr(self) -> str:
        return fingerprint(self.public_key)

    def wrap(self, secret: bytes, info: bytes) -> bytes:
        if self.type == HPKE_P256:
            return hpke_seal(self.public_key, secret, info)
        if self.type == RSA_OAEP_3072:
            return bytes(
                self.public_key.encrypt(
                    secret,
                    padding.OAEP(
                        mgf=padding.MGF1(hashes.SHA256()),
                        algorithm=hashes.SHA256(),
                        label=info,
                    ),
                )
            )
        raise KeyHierarchyError(f"unknown recipient type {self.type!r}")

    @classmethod
    def from_policy(cls, entry: Any) -> "Recipient":
        if not isinstance(entry, dict):
            raise KeyHierarchyError("a policy recipient is not an object")
        kind = entry.get("type")
        try:
            public = serialization.load_der_public_key(_unb64(entry.get("public_key")))
        except (ValueError, KeyHierarchyError) as exc:
            raise KeyHierarchyError(
                "a policy recipient's public_key is not SPKI"
            ) from exc
        if kind == HPKE_P256:
            ok = isinstance(public, ec.EllipticCurvePublicKey) and isinstance(
                public.curve, ec.SECP256R1
            )
        elif kind == RSA_OAEP_3072:
            ok = isinstance(public, rsa.RSAPublicKey) and public.key_size >= 3072
        else:
            ok = False
        if not ok:
            raise KeyHierarchyError(
                f"a policy recipient must be {HPKE_P256} (P-256) or "
                f"{RSA_OAEP_3072} (RSA 3072+), not {kind!r}"
            )
        recipient = cls(
            role=str(entry.get("role") or ROLE_ORG),
            type=str(kind),
            public_key=public,
            status=str(entry.get("status") or "active"),
            kms_uri=entry.get("kms_uri")
            if isinstance(entry.get("kms_uri"), str)
            else None,
        )
        stated = entry.get("fingerprint")
        if stated is not None and stated != recipient.fpr:
            raise KeyHierarchyError(
                f"a policy recipient's fingerprint {stated} does not match its key "
                f"({recipient.fpr})"
            )
        return recipient


class Unwrapper:
    """Something that can open a stanza wrapped to its public key."""

    role: str = ""
    label: str = ""

    def public(self) -> Any:  # pragma: no cover - interface
        raise NotImplementedError

    @property
    def fpr(self) -> str:
        return fingerprint(self.public())

    def unwrap(self, sealed: bytes, info: bytes) -> bytes:  # pragma: no cover
        raise NotImplementedError


class SoftwareKey(Unwrapper):
    """A P-256 key held in software (keychain, DPAPI, a file, or a reviewer's
    PEM). Same-user code can use it: status labels it ``software``."""

    def __init__(self, private: Any, role: str, label: str = "software") -> None:
        self._private = private
        self.role = role
        self.label = label

    def public(self) -> Any:
        return self._private.public_key()

    def recipient(self) -> Recipient:
        kind = (
            HPKE_P256
            if isinstance(self._private, ec.EllipticCurvePrivateKey)
            else RSA_OAEP_3072
        )
        return Recipient(self.role, kind, self.public())

    def unwrap(self, sealed: bytes, info: bytes) -> bytes:
        if isinstance(self._private, rsa.RSAPrivateKey):
            return bytes(
                self._private.decrypt(
                    sealed,
                    padding.OAEP(
                        mgf=padding.MGF1(hashes.SHA256()),
                        algorithm=hashes.SHA256(),
                        label=info,
                    ),
                )
            )
        return hpke_open(self._private, sealed, info)


# -- device key providers ------------------------------------------------------


class DeviceKeyProvider(Unwrapper):
    """A device key in secure hardware. ``available()`` says whether this
    computer has it; ``ensure()`` creates it on first use. The private half
    never leaves the hardware: ``unwrap`` asks the hardware to open a stanza
    (and, for a viewer key, the person to approve)."""

    name = "hardware"
    presence = False

    def available(self) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def ensure(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def recipient(self) -> Recipient:
        return Recipient(self.role, HPKE_P256, self.public())


class _NotBuilt(DeviceKeyProvider):
    """A hardware provider whose platform binding isn't built yet. It reports
    itself unavailable, so Flow falls back to the labelled software key."""

    why = "not built yet; needs a check on real hardware"

    def available(self) -> bool:
        return False

    def ensure(self) -> None:
        raise KeyHierarchyError(f"{self.name}: {self.why}")

    def public(self) -> Any:
        raise KeyHierarchyError(f"{self.name}: {self.why}")

    def unwrap(self, sealed: bytes, info: bytes) -> bytes:
        raise KeyHierarchyError(f"{self.name}: {self.why}")


class SecureEnclaveRuntimeKey(_NotBuilt):
    """macOS: a Secure Enclave P-256 key, ``privateKeyUsage``,
    ``WhenUnlockedThisDeviceOnly``, no presence (runtime role)."""

    name = "macOS Secure Enclave runtime key"
    role = ROLE_RUNTIME


class SecureEnclaveViewerKey(_NotBuilt):
    """macOS: a Secure Enclave P-256 key that needs Touch ID or the login
    password (``userPresence``) for every unwrap (viewer role)."""

    name = "macOS Secure Enclave viewer key (Touch ID)"
    role = ROLE_VIEWER
    presence = True


class TpmRuntimeKey(_NotBuilt):
    """Windows: a non-exportable TPM P-256 key through the Microsoft Platform
    Crypto Provider; Linux: a tpm2-tss key (runtime role)."""

    name = "TPM runtime key"
    role = ROLE_RUNTIME


class WindowsHelloViewerKey(_NotBuilt):
    """Windows: a Windows Hello (Passport) key, or a TPM key with a UI policy
    (viewer role)."""

    name = "Windows Hello viewer key"
    role = ROLE_VIEWER
    presence = True


class HardwareDouble(DeviceKeyProvider):
    """A stand-in for a hardware key in tests: a software key that counts
    unwraps and, for a viewer key, the presence prompts it would have shown.
    ``deny`` makes the next prompt fail, as a cancelled Touch ID would."""

    def __init__(self, role: str, *, presence: bool = False, name: str = "") -> None:
        self.role = role
        self.presence = presence
        self.name = name or f"test {role} key"
        self.label = "hardware (test double)"
        self._private: Optional[ec.EllipticCurvePrivateKey] = None
        self.unwraps = 0
        self.prompts = 0
        self.deny = False

    def available(self) -> bool:
        return True

    def ensure(self) -> None:
        if self._private is None:
            self._private = ec.generate_private_key(ec.SECP256R1())

    def public(self) -> Any:
        self.ensure()
        assert self._private is not None
        return self._private.public_key()

    def unwrap(self, sealed: bytes, info: bytes) -> bytes:
        self.ensure()
        assert self._private is not None
        if self.presence:
            self.prompts += 1
            if self.deny:
                raise KeyHierarchyError(f"{self.name}: the person cancelled the prompt")
        self.unwraps += 1
        return hpke_open(self._private, sealed, info)


def _platform_hardware() -> tuple[DeviceKeyProvider, DeviceKeyProvider]:
    if sys.platform == "darwin":
        return SecureEnclaveRuntimeKey(), SecureEnclaveViewerKey()
    if sys.platform == "win32":
        return TpmRuntimeKey(), WindowsHelloViewerKey()
    return TpmRuntimeKey(), TpmRuntimeKey()


_injected: Optional[tuple[Unwrapper, Unwrapper]] = None


def use_device_keys(
    runtime: Optional[Unwrapper], viewer: Optional[Unwrapper] = None
) -> None:
    """Replace the device keys (tests); ``None`` restores the platform's."""
    global _injected
    _injected = None if runtime is None else (runtime, viewer or runtime)


def _software_device(create: bool) -> Optional[SoftwareKey]:
    from openadapt_flow import at_rest

    text = at_rest.device_secret(create=create)
    if text is None:
        return None
    raw = base64.urlsafe_b64decode(text[len(at_rest.SOFTWARE_KEY_PREFIX) :].encode())
    return SoftwareKey(_from_scalar(raw), ROLE_DEVICE_SOFTWARE)


def device_keys(*, create: bool) -> Optional[tuple[Unwrapper, Unwrapper]]:
    """``(runtime, viewer)``: hardware keys where this computer has them,
    else one software key in both roles. None when there is none yet and
    ``create`` is False."""
    if _injected is not None:
        for key in _injected:
            if isinstance(key, DeviceKeyProvider) and create:
                key.ensure()
        return _injected
    runtime, viewer = _platform_hardware()
    if runtime.available() and viewer.available():
        if create:
            runtime.ensure()
            viewer.ensure()
        return runtime, viewer
    software = _software_device(create)
    if software is None:
        return None
    return software, software


def is_software(key: Unwrapper) -> bool:
    return isinstance(key, SoftwareKey)


# -- recovery key --------------------------------------------------------------


def _hpke_derive_p256(ikm: bytes) -> ec.EllipticCurvePrivateKey:
    """RFC 9180 section 7.1.3 DeriveKeyPair for DHKEM(P-256, HKDF-SHA256)."""
    import hmac

    suite_id = b"KEM" + (0x0010).to_bytes(2, "big")

    def extract(salt: bytes, label: bytes, ikm_: bytes) -> bytes:
        return hmac.new(
            salt or b"\0" * 32, b"HPKE-v1" + suite_id + label + ikm_, hashlib.sha256
        ).digest()

    def expand(prk: bytes, label: bytes, info: bytes, length: int) -> bytes:
        labeled = length.to_bytes(2, "big") + b"HPKE-v1" + suite_id + label + info
        out, block, counter = b"", b"", 1
        while len(out) < length:
            block = hmac.new(
                prk, block + labeled + bytes([counter]), hashlib.sha256
            ).digest()
            out += block
            counter += 1
        return out[:length]

    order = int("FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16)
    dkp_prk = extract(b"", b"dkp_prk", ikm)
    for counter in range(256):
        candidate = bytearray(expand(dkp_prk, b"candidate", bytes([counter]), 32))
        value = int.from_bytes(candidate, "big")
        if 0 < value < order:
            return ec.derive_private_key(value, ec.SECP256R1())
    raise KeyHierarchyError("recovery key derivation failed")  # pragma: no cover


def recovery_code_to_key(code: str) -> SoftwareKey:
    cleaned = "".join(ch for ch in code.upper() if ch.isalnum())
    try:
        raw = base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))
    except ValueError as exc:
        raise KeyHierarchyError("that is not an OpenAdapt recovery code") from exc
    if len(raw) != 32:
        raise KeyHierarchyError("that is not an OpenAdapt recovery code")
    return SoftwareKey(_hpke_derive_p256(raw), ROLE_RECOVERY, label="recovery")


def new_recovery_code() -> tuple[str, Recipient]:
    """A new recovery code (52 characters) and its public recipient. The code
    is shown once and never stored."""
    raw = secrets.token_bytes(32)
    code = base64.b32encode(raw).decode("ascii").rstrip("=")
    grouped = "-".join(code[i : i + 4] for i in range(0, len(code), 4))
    return grouped, recovery_code_to_key(code).recipient()


def _home() -> Path:
    from openadapt_flow import at_rest

    return at_rest._home()


def recovery_path() -> Path:
    return _home() / "keys" / "recovery.pub.json"


def save_recovery_recipient(recipient: Recipient) -> Path:
    path = recovery_path()
    _write_private(
        path,
        json.dumps(
            {
                "schema": "openadapt.ai/recovery-recipient/v1",
                "public_key": _b64(_spki(recipient.public_key)),
                "fingerprint": recipient.fpr,
            },
            indent=2,
        ).encode(),
    )
    return path


def recovery_recipient() -> Optional[Recipient]:
    path = recovery_path()
    if not path.is_file():
        return None
    entry = json.loads(path.read_text(encoding="utf-8"))
    return Recipient.from_policy({**entry, "type": HPKE_P256, "role": ROLE_RECOVERY})


# -- policy ---------------------------------------------------------------------


@dataclass
class Policy:
    """The organization policy, or the solo default (no file)."""

    mode: str = "solo"
    records: str = "inline"
    recipients: list[Recipient] = field(default_factory=list)
    key_archive: Optional[str] = None
    require_hardware_device_key: bool = False
    max_unarchived: int = 50
    source: str = "default (no policy)"
    org: Optional[str] = None

    @property
    def active_org(self) -> list[Recipient]:
        return [
            r
            for r in self.recipients
            if r.status == "active" and r.role in (ROLE_ORG, ROLE_BREAK_GLASS)
        ]


def _read_registry(subkey: str) -> Optional[dict]:  # pragma: no cover - Windows
    """The ``Policy`` value under ``HKLM\\<subkey>``. None only when the key
    does not exist; access denied or a key without the value is an error, so
    a policy the account can't read never falls back to solo mode."""
    import winreg

    try:
        handle = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, subkey)
    except FileNotFoundError:
        return None
    with handle:
        value, _ = winreg.QueryValueEx(handle, "Policy")
    return json.loads(value)


def _read_trusted_file(path: Path) -> tuple[bytes, os.stat_result]:
    """Read ``path`` once and return its bytes with the status of the file
    actually read (not of whatever the path names a moment later)."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks), info


def _root_only(info: os.stat_result) -> bool:
    """Owned by root and writable by nobody else (POSIX)."""
    return info.st_uid == 0 and not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)


def _read_machine_source(source: str) -> Optional[dict]:
    if source.startswith("HKLM\\"):
        if sys.platform != "win32":
            return None
        return _read_registry(source[5:])  # pragma: no cover - Windows only
    path = Path(source)
    if not path.is_file():
        return None
    if os.name != "posix":
        # A file path is a machine policy only where ownership can be checked.
        raise KeyHierarchyError(
            f"{path} can't be checked for administrator ownership on this "
            "platform, so it is not trusted as a machine policy. Nothing was "
            "written."
        )
    raw, info = _read_trusted_file(path)
    if not _root_only(info):
        raise KeyHierarchyError(
            f"{path} must be owned by root and writable by root only; it is "
            "not, so the machine policy can't be trusted. Nothing was written."
        )
    if path.suffix == ".plist":
        import plistlib

        data = plistlib.loads(raw)
        body = data.get("Policy", data) if isinstance(data, dict) else None
        return json.loads(body) if isinstance(body, (str, bytes)) else body
    return json.loads(raw.decode("utf-8"))


def load_policy() -> Policy:
    """The first policy found: machine sources first, then ``OPENADAPT_POLICY``.
    A user-writable file never overrides a machine one, and can set
    organization mode only when it is owned by root and writable by root
    only (or the trust variable is set, for development)."""
    for source in MACHINE_POLICY_SOURCES:
        try:
            raw = _read_machine_source(source)
        except (OSError, ValueError) as exc:
            raise KeyHierarchyError(
                f"the machine policy {source} can't be read: {exc}"
            ) from exc
        if raw is not None:
            return _parse_policy(raw, f"machine: {source}")
    configured = os.environ.get(POLICY_ENV)
    if not configured:
        return Policy()
    path = Path(configured)
    if not path.is_file():
        raise KeyHierarchyError(
            f"{POLICY_ENV} names {path}, which is not a file. A configured policy "
            "that can't be read never falls back to solo mode. Nothing was written."
        )
    try:
        data, info = _read_trusted_file(path)
        raw = json.loads(data.decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise KeyHierarchyError(f"{path} is not a readable policy file") from exc
    policy = _parse_policy(raw, f"file: {path}")
    # Ownership proves only on POSIX; elsewhere an org policy must come from
    # a machine source (Group Policy, the registry), never a user file. The
    # check is on the file actually read, and a root-owned file that others
    # can write is no better than a user's.
    if policy.mode == "org" and (os.name != "posix" or not _root_only(info)):
        if os.environ.get(TRUST_USER_POLICY_ENV) != "1":
            raise KeyHierarchyError(
                f"{path} sets organization mode but is not owned by root and "
                "writable by root only, so it can't be trusted (a user could "
                "swap the organization's key). Install it as a machine policy. "
                "Nothing was written."
            )
        policy.source += " (user-owned, trusted for development)"
    return policy


def _max_unarchived(value: Any, source: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise KeyHierarchyError(
            f"{source}: max_unarchived must be a whole number, 0 or more"
        )
    return value


def _parse_policy(raw: Any, source: str) -> Policy:
    if not isinstance(raw, dict) or raw.get("schema") != POLICY_SCHEMA:
        raise KeyHierarchyError(f"{source} is not an {POLICY_SCHEMA} policy")
    mode = raw.get("mode", "solo")
    if mode not in ("solo", "org"):
        raise KeyHierarchyError(f"{source}: mode must be 'solo' or 'org'")
    records = raw.get("records", "archive" if mode == "org" else "inline")
    if records not in ("archive", "inline"):
        raise KeyHierarchyError(f"{source}: records must be 'archive' or 'inline'")
    if mode == "org" and records != "archive":
        # Organization mode promises that shredding reaches every tracked
        # copy, and an organization key never opens a record a header
        # supplies (anyone who knows its public key could forge one).
        raise KeyHierarchyError(
            f"{source}: organization mode keeps key records in the archive "
            "(records: archive); records: inline is solo mode only. Recording "
            "and running stop until it does."
        )
    entries = raw.get("recipients", [])
    if not isinstance(entries, list):
        raise KeyHierarchyError(f"{source}: recipients must be a list")
    policy = Policy(
        mode=mode,
        records=records,
        recipients=[Recipient.from_policy(entry) for entry in entries],
        key_archive=raw.get("key_archive"),
        require_hardware_device_key=bool(raw.get("require_hardware_device_key", False)),
        max_unarchived=_max_unarchived(raw.get("max_unarchived", 50), source),
        source=source,
        org=raw.get("org"),
    )
    if mode == "org":
        if not policy.active_org:
            raise KeyHierarchyError(
                f"{source} sets organization mode but names no active organization "
                "recipient. Recording and running stop until it does."
            )
        if records == "archive" and not policy.key_archive:
            raise KeyHierarchyError(
                f"{source} sets organization mode with records: archive but no "
                "key_archive. Recording and running stop until it does."
            )
    return policy


# -- bucket records ---------------------------------------------------------------


def _stanza_info(bucket_id: str, role: str, fpr: str) -> bytes:
    return (
        _SUITE_LABEL + bucket_id.encode() + b"\0" + role.encode() + b"\0" + fpr.encode()
    )


def buckets_dir() -> Path:
    return _home() / "keys" / "buckets"


def _write_private(path: Path, data: bytes) -> None:
    from openadapt_flow import at_rest

    at_rest._private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    from openadapt_flow.private_file import open_binary

    fd = open_binary(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


@dataclass
class Bucket:
    """A bucket key pair and its record (the private half, wrapped)."""

    bucket_id: str
    private: Optional[ec.EllipticCurvePrivateKey]
    public: ec.EllipticCurvePublicKey
    record: dict[str, Any]

    @property
    def fpr(self) -> str:
        return fingerprint(self.public)

    def key_ref(self) -> dict[str, str]:
        return {"bucket_id": self.bucket_id, "bucket_pub_fpr": self.fpr}


def _stanza(recipient: Recipient, bucket_id: str, secret: bytes) -> dict[str, str]:
    info = _stanza_info(bucket_id, recipient.role, recipient.fpr)
    return {
        "role": recipient.role,
        "type": recipient.type,
        "fpr": recipient.fpr,
        "ct": _b64(recipient.wrap(secret, info)),
    }


_BUCKET_THREAD_LOCK = threading.RLock()
_bucket_depth = threading.local()


@contextmanager
def bucket_lock() -> Iterator[None]:
    """Serialize every change to bucket records (create, archive, close,
    shred) across threads and processes, so an archive worker can't write
    back a record a shred just destroyed, and two writers can't both take
    the last free slot under ``max_unarchived``. Re-entrant in a thread."""
    with _BUCKET_THREAD_LOCK:
        depth = getattr(_bucket_depth, "n", 0)
        if depth:
            _bucket_depth.n = depth + 1
            try:
                yield
            finally:
                _bucket_depth.n = depth
        else:
            from openadapt_flow import at_rest
            from openadapt_flow.sealed_maintenance import mutation_lock

            at_rest._private_dir(buckets_dir())
            _bucket_depth.n = 1
            try:
                with mutation_lock(buckets_dir()):
                    yield
            finally:
                _bucket_depth.n = 0


def _device_for(policy: Policy) -> tuple[Unwrapper, Unwrapper]:
    device = device_keys(create=True)
    assert device is not None
    runtime, viewer = device
    if policy.require_hardware_device_key and (
        is_software(runtime) or is_software(viewer)
    ):
        raise KeyHierarchyError(
            "the policy requires a hardware device key, and this computer has "
            "only a software key. Nothing was written."
        )
    return runtime, viewer


def preflight(policy: Optional[Policy] = None) -> None:
    """Refuse now what :func:`new_bucket` would refuse later, without making
    a record: for a capture whose raw session is plaintext while it records,
    so a policy that won't let it be sealed stops it before it starts."""
    policy = policy or load_policy()
    _device_for(policy)
    if policy.records != "archive":
        return
    try:
        _archive_root(policy.key_archive)
        return
    except KeyHierarchyError as exc:
        unreachable = exc
    if unarchived_count() + 1 > policy.max_unarchived:
        raise KeyHierarchyError(
            f"{unreachable}; {unarchived_count()} encrypted sets are waiting for "
            f"it and the policy allows {policy.max_unarchived}. Nothing was "
            "recorded."
        )


def new_bucket(kind: str, policy: Optional[Policy] = None) -> Bucket:
    """A fresh bucket for one sealed set, wrapped to the policy's recipients.

    Organization mode refuses without its active organization recipient and
    archive (fail closed). Its record is stored locally and sent to the
    archive at once; when the archive can't take it and more than
    ``max_unarchived`` records would then be waiting (0 means every record
    must reach the archive first), the record is destroyed again and the set
    is refused before anything is written.
    """
    policy = policy or load_policy()
    runtime, viewer = _device_for(policy)
    private = ec.generate_private_key(ec.SECP256R1())
    bucket_id = secrets.token_hex(16)
    secret = _scalar(private)
    recipients: list[Recipient] = []
    seen: set[str] = set()

    def add(recipient: Recipient) -> None:
        if recipient.fpr not in seen:
            seen.add(recipient.fpr)
            recipients.append(recipient)

    if not is_software(runtime):
        add(Recipient(ROLE_RUNTIME, HPKE_P256, runtime.public()))
    add(Recipient(viewer.role or ROLE_VIEWER, HPKE_P256, viewer.public()))
    for item in policy.active_org:
        add(item)
    recovery = recovery_recipient()
    if recovery is not None:
        add(recovery)
    record: dict[str, Any] = {
        "schema": BUCKET_SCHEMA,
        "bucket_id": bucket_id,
        "kind": kind,
        "bucket_pub": _b64(_spki(private.public_key())),
        "bucket_pub_fpr": fingerprint(private.public_key()),
        "mode": policy.mode,
        "records": policy.records,
        "created_at": time.time(),
        "archived": False,
        "stanzas": [_stanza(r, bucket_id, secret) for r in recipients],
    }
    bucket = Bucket(bucket_id, private, private.public_key(), record)
    if policy.records != "archive":
        return bucket
    # The archive this record goes to is fixed now: archiving and shredding
    # later use it, whatever the policy says by then.
    record["key_archive"] = policy.key_archive
    with bucket_lock():
        _store_local(record)
        if _archive_locked(bucket):
            if unarchived_count():
                _archive_pending_locked()  # the archive is back: send the rest
            return bucket
        waiting = unarchived_count()
        if waiting > policy.max_unarchived:
            _archive_pending_locked()  # the archive may be back
            waiting = unarchived_count()
        if waiting > policy.max_unarchived:
            _destroy_local(bucket_id)
            raise KeyHierarchyError(
                f"{waiting - 1} encrypted sets have not reached the organization "
                f"key archive ({policy.key_archive}), and the policy allows "
                f"{policy.max_unarchived} waiting. Reconnect the archive (it "
                f"needs its {ARCHIVE_MARKER} file) and run `openadapt-flow "
                "at-rest archive`. Nothing was written."
            )
    return bucket


def _record_path(bucket_id: str) -> Path:
    if not all(ch in "0123456789abcdef" for ch in bucket_id) or len(bucket_id) != 32:
        raise KeyHierarchyError("a bucket id is malformed")
    return buckets_dir() / f"{bucket_id}.json"


def _store_local(record: dict[str, Any]) -> None:
    _write_private(
        _record_path(record["bucket_id"]), json.dumps(record, indent=2).encode()
    )


def _read_local(bucket_id: str) -> Optional[dict[str, Any]]:
    path = _record_path(bucket_id)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("bucket_id") != bucket_id:
        raise KeyHierarchyError(f"the local key record {path} is malformed")
    return data


def _destroy_local(bucket_id: str) -> bool:
    """Overwrite, then delete, this computer's record. True if there was one."""
    path = _record_path(bucket_id)
    if not path.is_file():
        return False
    size = path.stat().st_size
    with open(path, "r+b") as handle:
        handle.write(b"\0" * size)
        handle.flush()
        os.fsync(handle.fileno())
    path.unlink()
    return True


def _archive_root(location: Optional[str]) -> Path:
    """The archive folder, only when it is reachable: it must already exist
    and hold :data:`ARCHIVE_MARKER`. Flow never creates it, so a missing
    mount is never mistaken for an empty archive."""
    target = location or ""
    if target.startswith("file://"):
        target = target[len("file://") :]
    if not target:
        raise KeyHierarchyError("no key_archive is recorded for this bucket")
    if "://" in target:
        raise KeyHierarchyError(
            f"key_archive {location}: only a local or mounted path "
            "(file://) is supported yet; s3:// and smb:// are not built."
        )
    root = Path(target)
    if not (root / ARCHIVE_MARKER).is_file():
        raise KeyHierarchyError(
            f"the key archive {location} is not reachable (no {ARCHIVE_MARKER} "
            "file at its root: unmounted or not set up)"
        )
    return root


def _intended_archive(
    record: dict[str, Any], policy: Optional[Policy]
) -> Optional[str]:
    location = record.get("key_archive")
    if isinstance(location, str) and location:
        return location
    return policy.key_archive if policy is not None else None


def _archive_locked(bucket: Bucket, policy: Optional[Policy] = None) -> bool:
    on_disk = _read_local(bucket.bucket_id)
    if on_disk is None:
        # Shredded (or never stored): never write a destroyed key back.
        return False
    record = dict(bucket.record)
    location = _intended_archive(on_disk, policy) or _intended_archive(record, None)
    copy = dict(record)
    copy["key_archive"] = location
    copy["archived"] = True
    copy["stanzas"] = [s for s in copy["stanzas"] if s["role"] != ROLE_RUNTIME]
    try:
        root = _archive_root(location)
        target = root / f"{bucket.bucket_id}.json"
        tmp = root / f".{bucket.bucket_id}.{secrets.token_hex(4)}.tmp"
        try:
            with open(tmp, "xb") as handle:
                handle.write(json.dumps(copy, indent=2).encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)  # never a truncated archive record
        finally:
            if os.path.lexists(tmp):
                os.unlink(tmp)
    except (OSError, KeyHierarchyError):
        return False
    record["key_archive"] = location
    record["archived"] = True
    bucket.record.update(record)
    _store_local(bucket.record)
    return True


def archive(bucket: Bucket, policy: Optional[Policy] = None) -> bool:
    """Copy the bucket record to the archive recorded for it (atomically).
    The runtime stanza is never archived. Returns False (and keeps the
    record local, as unarchived) when that archive is unreachable, and when
    the record was shredded meanwhile (it is never written back)."""
    if bucket.record.get("records") != "archive":
        return False
    with bucket_lock():
        return _archive_locked(bucket, policy)


def close(bucket: Bucket) -> None:
    """The set is written: drop the runtime stanza, so only the viewer,
    organization or recovery key opens it from now on. An archive-mode
    record that was shredded while the set was written is not brought back:
    that refuses."""
    bucket.record["stanzas"] = [
        s for s in bucket.record["stanzas"] if s["role"] != ROLE_RUNTIME
    ]
    if bucket.record.get("records") != "archive":
        return
    with bucket_lock():
        on_disk = _read_local(bucket.bucket_id)
        if on_disk is None:
            raise KeyHierarchyError(
                "this set's key record was shredded while the set was being "
                "written; it is not written back"
            )
        bucket.record["archived"] = bool(on_disk.get("archived"))
        if on_disk.get("key_archive"):
            bucket.record["key_archive"] = on_disk["key_archive"]
        _store_local(bucket.record)
        if not bucket.record["archived"]:
            _archive_locked(bucket)


def unarchived_count() -> int:
    folder = buckets_dir()
    if not folder.is_dir():
        return 0
    count = 0
    for path in folder.glob("*.json"):
        try:
            if not json.loads(path.read_text(encoding="utf-8")).get("archived"):
                count += 1
        except (OSError, ValueError):
            count += 1  # unreadable: count it as waiting, never as archived
    return count


def find_record(
    key_ref: dict[str, Any], inline: Optional[dict[str, Any]]
) -> dict[str, Any]:
    """This computer's record for the bucket, else the one the header
    carries. A record from a header is opened only by this computer's
    device keys or a recovery key (:func:`open_bucket`)."""
    bucket_id = str(key_ref.get("bucket_id"))
    local = _read_local(bucket_id)
    if local is not None:
        return local
    if inline is not None:
        return inline
    raise NoRecipientKey(
        f"this computer has no key record for bucket {bucket_id}. Open it as the "
        "organization (copy its archive record into "
        f"{buckets_dir()} and set {ORG_KEY_FILE_ENV}), or it was shredded."
    )


def _candidates(
    *, from_header: bool
) -> tuple[list[Unwrapper], Optional[BaseException]]:
    """The keys this computer can try, and the error reading the device key
    raised (kept, so a lost keychain entry doesn't hide a valid recovery or
    organization key). An organization key never opens a record a header
    supplied: anyone who knows the organization's public key could have
    made it."""
    out: list[Unwrapper] = []
    deferred: Optional[BaseException] = None
    try:
        device = device_keys(create=False)
    except (KeyHierarchyError, RuntimeError) as exc:
        device, deferred = None, exc
    if device is not None:
        runtime, viewer = device
        out.append(runtime)
        if viewer is not runtime:
            out.append(viewer)
    code = os.environ.get(RECOVERY_CODE_ENV)
    if code:
        out.append(recovery_code_to_key(code))
    org_file = os.environ.get(ORG_KEY_FILE_ENV)
    if org_file and not from_header:
        try:
            private = serialization.load_pem_private_key(
                Path(org_file).read_bytes(), password=None
            )
        except (OSError, ValueError) as exc:
            raise KeyHierarchyError(
                f"{ORG_KEY_FILE_ENV}: {org_file} can't be read"
            ) from exc
        out.append(SoftwareKey(private, ROLE_ORG, label="organization (file)"))
    return out, deferred


_HEADER_ROLES: Final[frozenset[str]] = frozenset(
    {ROLE_RUNTIME, ROLE_VIEWER, ROLE_DEVICE_SOFTWARE, ROLE_RECOVERY}
)


def open_bucket(
    record: dict[str, Any],
    *,
    prefer_runtime: bool = True,
    from_header: bool = False,
    restore: bool = False,
) -> Bucket:
    """Unwrap a bucket's private half with the first key this computer holds.

    ``from_header``: the record came from the set's own header, not from
    this computer's key store. Only this computer's device key opens it then,
    which binds the set to the device that wrote it: its public key is never
    written out, so nobody else can wrap a bucket to it. The recovery public
    key is on disk, so a set that only the recovery code opens could have
    been made by anyone who read that file. It opens only with ``restore``
    (``at-rest restore DIR``, the person's explicit choice), and otherwise
    raises :class:`RecoveryRestoreRequired`.
    """
    if record.get("schema") != BUCKET_SCHEMA:
        raise KeyHierarchyError("not a bucket record")
    if record.get("shredded"):
        raise NoRecipientKey("this bucket was shredded; nobody can open it")
    bucket_id = str(record["bucket_id"])
    public = serialization.load_der_public_key(_unb64(record["bucket_pub"]))
    if fingerprint(public) != record.get("bucket_pub_fpr"):
        raise KeyHierarchyError(
            "the bucket record's public key does not match its fingerprint"
        )
    stanzas = {s.get("fpr"): s for s in record.get("stanzas") or []}
    keys, deferred = _candidates(from_header=from_header)
    needs_restore = False
    for key in keys:
        try:
            fpr = key.fpr
        except KeyHierarchyError:
            continue
        stanza = stanzas.get(fpr)
        if stanza is None:
            continue
        if from_header and str(stanza.get("role")) not in _HEADER_ROLES:
            continue
        if (
            from_header
            and stanza.get("role") == ROLE_RECOVERY
            and not (restore or os.environ.get(RECOVERY_RESTORE_ENV) == "1")
        ):
            needs_restore = True
            continue
        info = _stanza_info(bucket_id, str(stanza["role"]), fpr)
        try:
            secret = key.unwrap(_unb64(stanza["ct"]), info)
        except KeyHierarchyError:
            raise
        except Exception as exc:  # noqa: BLE001 - wrong key or damaged stanza
            raise KeyHierarchyError(
                f"the {stanza['role']} stanza does not open"
            ) from exc
        private = _from_scalar(secret)
        if fingerprint(private.public_key()) != record["bucket_pub_fpr"]:
            raise KeyHierarchyError(
                "the unwrapped bucket key does not match the record"
            )
        return Bucket(bucket_id, private, private.public_key(), record)  # type: ignore[arg-type]
    if needs_restore:
        raise RecoveryRestoreRequired(
            "this encrypted set opens only with the recovery code: this computer's "
            "device key does not open it, so Flow can't tell it was written here "
            "rather than made by someone who read the recovery public key. If it "
            "is your backup, restore it with the code: `openadapt-flow at-rest "
            f"restore DIR --yes`, or set {RECOVERY_RESTORE_ENV}=1 to read your "
            "backups with it. Nothing was read."
        )
    if deferred is not None:
        raise deferred
    raise NoRecipientKey(
        "no key on this computer opens this encrypted set: it is wrapped for "
        + ", ".join(f"{s.get('role')} {s.get('fpr')}" for s in stanzas.values())
        + ". Open it with its organization key, or set "
        f"{RECOVERY_CODE_ENV}. Nothing was read."
    )


def device_stanza(bucket: Bucket) -> dict[str, str]:
    """A stanza wrapping ``bucket`` to this computer's viewer key (made if
    missing), for a set restored with the recovery code."""
    device = device_keys(create=True)
    assert device is not None and bucket.private is not None
    viewer = device[1]
    recipient = Recipient(viewer.role or ROLE_VIEWER, HPKE_P256, viewer.public())
    return _stanza(recipient, bucket.bucket_id, _scalar(bucket.private))


def _pending_dir() -> Path:
    return _home() / "keys" / "shred-pending"


def _pending_path(bucket_id: str) -> Path:
    return _pending_dir() / f"{_record_path(bucket_id).stem}.json"


def _delete_archive_copy(bucket_id: str, location: Optional[str]) -> bool:
    """Delete the archive's copy. True only when the archive is reachable
    (its marker is there) and the copy is gone from it."""
    try:
        target = _archive_root(location) / f"{bucket_id}.json"
        if target.is_file():
            target.unlink()
        return not os.path.lexists(target)
    except (OSError, KeyHierarchyError):
        return False


def shred(
    bucket_id: str,
    *,
    records: Optional[str],
    key_archive: Optional[str],
) -> dict[str, Any]:
    """Destroy every tracked copy of a bucket record: the local one
    (overwritten, then deleted) and the copy in the archive it was sent to.

    ``records`` and ``key_archive`` come from the set's authenticated header
    (the local record's own archive wins). The current policy plays no part:
    a later switch to another archive or to solo mode must not leave the
    original archive's copy behind.

    Returns the shred record (bucket id, time, copies destroyed; no key).
    ``complete`` is False while the archive copy could not be deleted (an
    offline, unmounted or read-only archive): a pending marker in this
    computer's key store keeps the bucket id and that archive, and
    :func:`retry_shred` (``at-rest shred`` again, or ``at-rest archive``)
    finishes it.
    """
    _record_path(bucket_id)  # validates the id
    removed: list[str] = []
    with bucket_lock():
        local = _read_local(bucket_id)
        if local is not None:
            records = str(local.get("records") or records)
            key_archive = _intended_archive(local, None) or key_archive
        if _destroy_local(bucket_id):
            removed.append("local")
        complete = True
        if records == "archive":
            if key_archive and _delete_archive_copy(bucket_id, key_archive):
                removed.append("archive")
            else:
                complete = False
                _write_private(
                    _pending_path(bucket_id),
                    json.dumps(
                        {"bucket_id": bucket_id, "key_archive": key_archive}
                    ).encode(),
                )
    return {
        "schema": SHRED_SCHEMA,
        "bucket_id": bucket_id,
        "shredded_at": time.time(),
        "copies_destroyed": removed,
        "records": records,
        "key_archive": key_archive,
        "complete": complete,
    }


def retry_shred(bucket_id: str) -> bool:
    """Finish a shred whose archive deletion failed, in the archive its
    pending marker names. True when that copy is gone. The marker stays
    until :func:`clear_pending_shred`, so a crash before the caller records
    completion is retried rather than forgotten. Without a marker on this
    computer nothing is deleted and the answer is False: completion is never
    assumed."""
    with bucket_lock():
        marker = _pending_path(bucket_id)
        if not marker.is_file():
            return False
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        location = data.get("key_archive") if isinstance(data, dict) else None
        if not isinstance(location, str) or not location:
            return False
        return _delete_archive_copy(bucket_id, location)


def clear_pending_shred(bucket_id: str) -> None:
    with bucket_lock():
        marker = _pending_path(bucket_id)
        if marker.is_file():
            marker.unlink()


def has_pending_shred(bucket_id: str) -> bool:
    return _pending_path(bucket_id).is_file()


def pending_shreds() -> list[str]:
    folder = _pending_dir()
    return sorted(p.stem for p in folder.glob("*.json")) if folder.is_dir() else []


def _archive_pending_locked() -> tuple[int, int]:
    done = failed = 0
    folder = buckets_dir()
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("archived") or record.get("records") != "archive":
                continue
            bucket = Bucket(
                str(record["bucket_id"]),
                None,
                serialization.load_der_public_key(_unb64(record["bucket_pub"])),  # type: ignore[arg-type]
                record,
            )
        except (OSError, ValueError, KeyError, KeyHierarchyError):
            failed += 1
            continue
        if _archive_locked(bucket):
            done += 1
        else:
            failed += 1
    return done, failed


def archive_pending(policy: Optional[Policy] = None) -> dict[str, int]:
    """Archive every local record not yet archived (each to the archive
    recorded for it), and finish pending shreds. Idempotent: run it after
    the archive comes back. A shred finished here is complete in the key
    store; ``at-rest shred DIR`` again also marks the set's header."""
    with bucket_lock():
        done, failed = _archive_pending_locked()
        for bucket_id in pending_shreds():
            retry_shred(bucket_id)
        still = [b for b in pending_shreds() if not _archive_copy_gone(b)]
    return {"archived": done, "failed": failed, "shreds_pending": len(still)}


def _archive_copy_gone(bucket_id: str) -> bool:
    try:
        data = json.loads(_pending_path(bucket_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    location = data.get("key_archive") if isinstance(data, dict) else None
    if not isinstance(location, str) or not location:
        return False
    try:
        return not os.path.lexists(_archive_root(location) / f"{bucket_id}.json")
    except KeyHierarchyError:
        return False


def describe(policy: Policy) -> dict[str, Any]:
    device = device_keys(create=False)
    runtime_label = viewer_label = None
    if device is not None:
        runtime_label = getattr(device[0], "label", "")
        viewer_label = getattr(device[1], "label", "")
    recovery = recovery_recipient()
    return {
        "mode": policy.mode,
        "records": policy.records,
        "policy_source": policy.source,
        "org_recipients": [
            {"role": r.role, "type": r.type, "fingerprint": r.fpr, "status": r.status}
            for r in policy.recipients
        ],
        "key_archive": policy.key_archive,
        "unarchived": unarchived_count() if policy.records == "archive" else 0,
        "device_runtime_key": runtime_label,
        "device_viewer_key": viewer_label,
        "recovery_key": recovery.fpr if recovery else None,
    }


__all__ = [
    "Bucket",
    "DeviceKeyProvider",
    "HardwareDouble",
    "KeyHierarchyError",
    "NoRecipientKey",
    "RecoveryRestoreRequired",
    "Policy",
    "Recipient",
    "SoftwareKey",
    "ARCHIVE_MARKER",
    "archive",
    "archive_pending",
    "bucket_lock",
    "clear_pending_shred",
    "close",
    "describe",
    "device_keys",
    "find_record",
    "fingerprint",
    "hpke_open",
    "hpke_seal",
    "load_policy",
    "new_bucket",
    "new_recovery_code",
    "open_bucket",
    "recovery_code_to_key",
    "save_recovery_recipient",
    "shred",
    "use_device_keys",
]

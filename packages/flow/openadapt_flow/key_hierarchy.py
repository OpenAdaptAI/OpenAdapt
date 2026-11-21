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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Optional

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
#: A PEM private key of an organization or break-glass recipient, for a
#: reviewer opening evidence without a KMS (KMS unwrap is not built yet).
ORG_KEY_FILE_ENV: Final[str] = "OPENADAPT_ORG_KEY_FILE"

ROLE_RUNTIME: Final[str] = "runtime"
ROLE_VIEWER: Final[str] = "viewer"
ROLE_DEVICE_SOFTWARE: Final[str] = "device-software"
ROLE_ORG: Final[str] = "org"
ROLE_BREAK_GLASS: Final[str] = "break-glass"
ROLE_RECOVERY: Final[str] = "recovery"

#: Machine-scope policy locations, first match wins. Tests replace this.
MACHINE_POLICY_SOURCES: list[str] = [
    "/Library/Managed Preferences/ai.openadapt.policy.plist",
    "HKLM\\SOFTWARE\\Policies\\OpenAdapt",
    "/etc/openadapt/policy.json",
]

_SUITE_LABEL: Final[bytes] = b"openadapt.ai/bucket/v1/wrap\0"
_RECOVERY_SALT: Final[bytes] = b"openadapt.ai/recovery-key/v1"


class KeyHierarchyError(ValueError):
    """A policy, recipient or bucket record is missing, malformed or refused."""


class NoRecipientKey(KeyHierarchyError):
    """No key this computer holds opens the bucket record."""


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


def _read_machine_source(source: str) -> Optional[dict]:
    if source.startswith("HKLM\\"):
        if sys.platform != "win32":
            return None
        try:  # pragma: no cover - Windows only
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, source[5:]) as handle:
                value, _ = winreg.QueryValueEx(handle, "Policy")
            return json.loads(value)
        except OSError:  # pragma: no cover
            return None
    path = Path(source)
    if not path.is_file():
        return None
    if path.suffix == ".plist":
        import plistlib

        data = plistlib.loads(path.read_bytes())
        body = data.get("Policy", data) if isinstance(data, dict) else None
        return json.loads(body) if isinstance(body, (str, bytes)) else body
    if os.name == "posix":
        info = path.stat()
        if info.st_uid != 0 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise KeyHierarchyError(
                f"{path} must be owned by root and writable by root only; it is "
                "not, so the machine policy can't be trusted. Nothing was written."
            )
    return json.loads(path.read_text(encoding="utf-8"))


def load_policy() -> Policy:
    """The first policy found: machine sources first, then ``OPENADAPT_POLICY``.
    A user-writable file never overrides a machine one, and can set
    organization mode only when it is root-owned (or the trust variable is
    set, for development)."""
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
        return Policy(source=f"{POLICY_ENV} names no file")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise KeyHierarchyError(f"{path} is not a readable policy file") from exc
    policy = _parse_policy(raw, f"file: {path}")
    if policy.mode == "org" and os.name == "posix" and path.stat().st_uid != 0:
        if os.environ.get(TRUST_USER_POLICY_ENV) != "1":
            raise KeyHierarchyError(
                f"{path} sets organization mode but is not owned by root, so it "
                "can't be trusted (a user could swap the organization's key). "
                "Install it as a machine policy. Nothing was written."
            )
        policy.source += " (user-owned, trusted for development)"
    return policy


def _parse_policy(raw: Any, source: str) -> Policy:
    if not isinstance(raw, dict) or raw.get("schema") != POLICY_SCHEMA:
        raise KeyHierarchyError(f"{source} is not an {POLICY_SCHEMA} policy")
    mode = raw.get("mode", "solo")
    if mode not in ("solo", "org"):
        raise KeyHierarchyError(f"{source}: mode must be 'solo' or 'org'")
    records = raw.get("records", "archive" if mode == "org" else "inline")
    if records not in ("archive", "inline"):
        raise KeyHierarchyError(f"{source}: records must be 'archive' or 'inline'")
    entries = raw.get("recipients", [])
    if not isinstance(entries, list):
        raise KeyHierarchyError(f"{source}: recipients must be a list")
    policy = Policy(
        mode=mode,
        records=records,
        recipients=[Recipient.from_policy(entry) for entry in entries],
        key_archive=raw.get("key_archive"),
        require_hardware_device_key=bool(raw.get("require_hardware_device_key", False)),
        max_unarchived=int(raw.get("max_unarchived", 50)),
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
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
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


def new_bucket(kind: str, policy: Optional[Policy] = None) -> Bucket:
    """A fresh bucket for one sealed set, wrapped to the policy's recipients.

    Organization mode refuses without its active organization recipient and
    archive (fail closed), and when ``max_unarchived`` records have not
    reached the archive yet.
    """
    policy = policy or load_policy()
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
    if policy.mode == "org" and policy.records == "archive":
        pending = unarchived_count()
        if pending >= policy.max_unarchived:
            raise KeyHierarchyError(
                f"{pending} encrypted sets have not reached the organization key "
                f"archive ({policy.key_archive}); the policy allows "
                f"{policy.max_unarchived}. Reconnect so they archive. Nothing was "
                "written."
            )
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
    record = {
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
    if policy.records == "archive":
        _store_local(record)
    return bucket


def _record_path(bucket_id: str) -> Path:
    if not all(ch in "0123456789abcdef" for ch in bucket_id) or len(bucket_id) != 32:
        raise KeyHierarchyError("a bucket id is malformed")
    return buckets_dir() / f"{bucket_id}.json"


def _store_local(record: dict[str, Any]) -> None:
    _write_private(
        _record_path(record["bucket_id"]), json.dumps(record, indent=2).encode()
    )


def _archive_dir(policy: Policy) -> Path:
    target = policy.key_archive or ""
    if target.startswith("file://"):
        target = target[len("file://") :]
    if "://" in target:
        raise KeyHierarchyError(
            f"key_archive {policy.key_archive}: only a local or mounted path "
            "(file://) is supported yet; s3:// and smb:// are not built."
        )
    return Path(target)


def archive(bucket: Bucket, policy: Optional[Policy] = None) -> bool:
    """Copy the bucket record to the organization's archive. The runtime
    stanza is never archived. Returns False (and keeps the record local, as
    unarchived) when the archive is unreachable."""
    policy = policy or load_policy()
    if policy.records != "archive":
        return False
    copy = dict(bucket.record)
    copy["stanzas"] = [s for s in copy["stanzas"] if s["role"] != ROLE_RUNTIME]
    try:
        folder = _archive_dir(policy)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{bucket.bucket_id}.json"
        target.write_text(json.dumps(copy, indent=2), encoding="utf-8")
    except OSError:
        return False
    bucket.record["archived"] = True
    _store_local(bucket.record)
    return True


def close(bucket: Bucket) -> None:
    """The set is written: drop the runtime stanza, so only the viewer,
    organization or recovery key opens it from now on."""
    bucket.record["stanzas"] = [
        s for s in bucket.record["stanzas"] if s["role"] != ROLE_RUNTIME
    ]
    if bucket.record.get("records") == "archive":
        _store_local(bucket.record)


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
            continue
    return count


def find_record(
    key_ref: dict[str, Any], inline: Optional[dict[str, Any]]
) -> dict[str, Any]:
    bucket_id = str(key_ref.get("bucket_id"))
    if inline is not None:
        return inline
    path = _record_path(bucket_id)
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    raise NoRecipientKey(
        f"this computer has no key record for bucket {bucket_id}. Open it as the "
        "organization (its archive copy and key), or it was shredded."
    )


def _candidates() -> list[Unwrapper]:
    out: list[Unwrapper] = []
    device = device_keys(create=False)
    if device is not None:
        runtime, viewer = device
        out.append(runtime)
        if viewer is not runtime:
            out.append(viewer)
    code = os.environ.get(RECOVERY_CODE_ENV)
    if code:
        out.append(recovery_code_to_key(code))
    org_file = os.environ.get(ORG_KEY_FILE_ENV)
    if org_file:
        try:
            private = serialization.load_pem_private_key(
                Path(org_file).read_bytes(), password=None
            )
        except (OSError, ValueError) as exc:
            raise KeyHierarchyError(
                f"{ORG_KEY_FILE_ENV}: {org_file} can't be read"
            ) from exc
        out.append(SoftwareKey(private, ROLE_ORG, label="organization (file)"))
    return out


def open_bucket(record: dict[str, Any], *, prefer_runtime: bool = True) -> Bucket:
    """Unwrap a bucket's private half with the first key this computer holds."""
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
    for key in _candidates():
        try:
            fpr = key.fpr
        except KeyHierarchyError:
            continue
        stanza = stanzas.get(fpr)
        if stanza is None:
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
    raise NoRecipientKey(
        "no key on this computer opens this encrypted set: it is wrapped for "
        + ", ".join(f"{s.get('role')} {s.get('fpr')}" for s in stanzas.values())
        + ". Open it with its organization key, or set "
        f"{RECOVERY_CODE_ENV}. Nothing was read."
    )


def shred(record: dict[str, Any], policy: Optional[Policy] = None) -> dict[str, Any]:
    """Destroy every tracked copy of a bucket record: the local one
    (overwritten, then deleted) and the archive copy. Returns the shred
    record, which names the bucket and time and holds no key."""
    policy = policy or load_policy()
    bucket_id = str(record["bucket_id"])
    removed: list[str] = []
    path = _record_path(bucket_id)
    if path.is_file():
        size = path.stat().st_size
        with open(path, "r+b") as handle:
            handle.write(b"\0" * size)
            handle.flush()
            os.fsync(handle.fileno())
        path.unlink()
        removed.append("local")
    if policy.records == "archive" and policy.key_archive:
        try:
            target = _archive_dir(policy) / f"{bucket_id}.json"
            if target.is_file():
                target.unlink()
                removed.append("archive")
        except (OSError, KeyHierarchyError):
            pass
    return {
        "schema": SHRED_SCHEMA,
        "bucket_id": bucket_id,
        "shredded_at": time.time(),
        "copies_destroyed": removed,
        "records": record.get("records"),
    }


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
    "Policy",
    "Recipient",
    "SoftwareKey",
    "archive",
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

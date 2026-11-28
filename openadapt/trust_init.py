"""Customer-owned Ed25519 trust key initialization and storage.

Generates an Ed25519 keypair for signing local receipts and cohort manifests.
Private key material is stored in the OS keystore (macOS Keychain, Windows
DPAPI / Credential Manager, or Secret Service via ``keyring``) when available,
or in a ``0600`` file inside a ``0700`` user-level trust directory outside
mutable bundle directories.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

TRUST_METADATA_SCHEMA = "openadapt.trust-key/v1"
DEFAULT_TRUST_DIR = Path.home() / ".openadapt" / "trust"
KEYRING_SERVICE = "openadapt.trust.ed25519"
METADATA_FILENAME = "trust.json"
PUBLIC_KEY_FILENAME = "ed25519.pub.pem"
PRIVATE_KEY_FILENAME = "ed25519.key.pem"

_BUNDLE_MARKER_FILES = frozenset(
    {
        "bundle.json",
        "workflow.json",
        "workflow.yaml",
        "program.json",
        "compiled.json",
        "steps.json",
    }
)


class TrustInitError(ValueError):
    """Raised when trust key initialization or loading fails."""


class KeyStoreBackend(Protocol):
    """Minimal keystore interface for storing and retrieving private keys."""

    name: str

    def is_available(self) -> bool: ...

    def store(self, account: str, secret: str) -> None: ...

    def load(self, account: str) -> Optional[str]: ...


@dataclass(frozen=True)
class TrustKeyResult:
    """Metadata returned after initializing a customer-owned Ed25519 keypair."""

    key_id: str
    fingerprint_sha256: str
    algorithm: str
    storage_backend: str
    key_dir: str
    metadata_path: str
    public_key_path: str
    private_key_path: Optional[str]
    public_key_pem: str
    public_key_b64: str
    created_at: str
    owner: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": TRUST_METADATA_SCHEMA,
            "key_id": self.key_id,
            "fingerprint_sha256": self.fingerprint_sha256,
            "algorithm": self.algorithm,
            "storage_backend": self.storage_backend,
            "key_dir": self.key_dir,
            "metadata_path": self.metadata_path,
            "public_key_path": self.public_key_path,
            "private_key_path": self.private_key_path,
            "public_key_pem": self.public_key_pem,
            "public_key_b64": self.public_key_b64,
            "created_at": self.created_at,
            "owner": self.owner,
        }


class KeyringStore:
    """OS keystore adapter backed by the ``keyring`` package."""

    name = "keyring"

    def __init__(self, service: str = KEYRING_SERVICE) -> None:
        self._service = service

    def is_available(self) -> bool:
        try:
            import keyring
            from keyring.backends import fail

            backend = keyring.get_keyring()
            return not isinstance(backend, fail.Keyring)
        except (ImportError, OSError, RuntimeError, ValueError):
            return False

    def store(self, account: str, secret: str) -> None:
        import keyring

        keyring.set_password(self._service, account, secret)

    def load(self, account: str) -> Optional[str]:
        import keyring

        return keyring.get_password(self._service, account)


class MacOSKeychainStore:
    """macOS Keychain adapter using the system ``security`` CLI."""

    name = "macos-keychain"

    def __init__(
        self,
        service: str = KEYRING_SERVICE,
        runner: Optional[Callable[..., subprocess.CompletedProcess[str]]] = None,
    ) -> None:
        self._service = service
        self._runner = runner or subprocess.run

    def is_available(self) -> bool:
        if sys.platform != "darwin":
            return False
        try:
            proc = self._runner(
                ["security", "default-keychain"],
                capture_output=True,
                text=True,
                check=False,
            )
            return proc.returncode == 0
        except OSError:
            return False

    def store(self, account: str, secret: str) -> None:
        proc = self._runner(
            [
                "security",
                "add-generic-password",
                "-U",
                "-a",
                account,
                "-s",
                self._service,
                "-w",
                secret,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise TrustInitError(
                f"macOS Keychain store failed: {(proc.stderr or proc.stdout).strip()}"
            )

    def load(self, account: str) -> Optional[str]:
        proc = self._runner(
            [
                "security",
                "find-generic-password",
                "-a",
                account,
                "-s",
                self._service,
                "-w",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None


class WindowsDPAPIStore:
    """Windows DPAPI file-backed store using ``CryptProtectData``."""

    name = "windows-dpapi"

    def __init__(self, key_dir: Path) -> None:
        self._key_dir = key_dir

    def _blob_path(self, account: str) -> Path:
        safe = hashlib.sha256(account.encode("utf-8")).hexdigest()[:16]
        return self._key_dir / f"ed25519-{safe}.dpapi"

    def is_available(self) -> bool:
        if sys.platform != "win32":
            return False
        try:
            import ctypes

            return hasattr(ctypes, "windll") and hasattr(ctypes.windll, "crypt32")
        except (ImportError, OSError, AttributeError):
            return False

    def store(self, account: str, secret: str) -> None:
        if not self.is_available():
            raise TrustInitError("Windows DPAPI is not available on this platform.")
        import ctypes
        from ctypes import wintypes

        class _DataBlob(ctypes.Structure):
            _fields_ = [
                ("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char)),
            ]

        raw = secret.encode("utf-8")
        in_blob = _DataBlob(len(raw), ctypes.create_string_buffer(raw, len(raw)))
        out_blob = _DataBlob()
        ok = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            "openadapt-trust-ed25519",
            None,
            None,
            None,
            0,
            ctypes.byref(out_blob),
        )
        if not ok:
            raise TrustInitError("Windows DPAPI CryptProtectData failed.")
        try:
            protected = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(out_blob.pbData)
        self._blob_path(account).write_bytes(protected)

    def load(self, account: str) -> Optional[str]:
        path = self._blob_path(account)
        if not path.is_file() or not self.is_available():
            return None
        import ctypes
        from ctypes import wintypes

        class _DataBlob(ctypes.Structure):
            _fields_ = [
                ("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char)),
            ]

        protected = path.read_bytes()
        in_blob = _DataBlob(
            len(protected), ctypes.create_string_buffer(protected, len(protected))
        )
        out_blob = _DataBlob()
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(out_blob),
        )
        if not ok:
            return None
        try:
            plain = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(out_blob.pbData)
        return plain.decode("utf-8")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_inside_bundle_dir(path: Path) -> bool:
    """Return True if ``path`` sits inside a mutable workflow bundle directory."""
    resolved = path.expanduser().resolve(strict=False)
    for candidate in (resolved, *resolved.parents):
        if candidate.name.endswith(".bundle"):
            return True
        if candidate.is_dir():
            for marker in _BUNDLE_MARKER_FILES:
                if (candidate / marker).is_file():
                    return True
    return False


def _enforce_dir_mode(path: Path) -> None:
    if os.name == "posix":
        os.chmod(path, 0o700)


def _write_private_file(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    mode = 0o600
    fd = os.open(str(path), flags, mode)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    if os.name == "posix":
        os.chmod(path, 0o600)


def _select_os_keystore(
    key_dir: Path,
    keystore: Optional[KeyStoreBackend] = None,
) -> Optional[KeyStoreBackend]:
    if keystore is not None:
        return keystore if keystore.is_available() else None
    candidates: list[KeyStoreBackend] = [KeyringStore()]
    if sys.platform == "darwin":
        candidates.append(MacOSKeychainStore())
    elif sys.platform == "win32":
        candidates.append(WindowsDPAPIStore(key_dir))
    for backend in candidates:
        if backend.is_available():
            return backend
    return None


def compute_public_key_fingerprint(public_key: Ed25519PublicKey) -> tuple[str, str]:
    """Return ``(fingerprint_sha256, key_id)`` for an Ed25519 public key."""
    raw_pub = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    digest = hashlib.sha256(raw_pub).hexdigest()
    return digest, f"ed25519:{digest[:16]}"


def init_trust_key(
    *,
    key_dir: Optional[Path] = None,
    storage: str = "auto",
    force: bool = False,
    owner: str = "customer",
    keystore: Optional[KeyStoreBackend] = None,
) -> TrustKeyResult:
    """Initialize a customer-owned Ed25519 trust keypair.

    Parameters
    ----------
    key_dir:
        User-level directory for public metadata (and ``0600`` private key when
        file-backed storage is used). Defaults to ``~/.openadapt/trust``.
    storage:
        ``"auto"`` (OS keystore if available, else ``0600`` file), ``"keychain"``
        (require OS keystore), or ``"file"`` (store in ``0600`` file).
    force:
        Overwrite an existing key if True. Refuses to overwrite when False.
    owner:
        Label identifying the key owner (default ``"customer"``).
    keystore:
        Optional injectable keystore backend for testing.
    """
    if storage not in ("auto", "keychain", "file"):
        raise TrustInitError(
            f"Unsupported storage mode {storage!r}; expected 'auto', 'keychain', or 'file'."
        )

    target_dir = (key_dir or DEFAULT_TRUST_DIR).expanduser().resolve(strict=False)
    if is_inside_bundle_dir(target_dir):
        raise TrustInitError(
            f"Refusing to store trust keys inside mutable bundle directory: {target_dir}. "
            "Choose a user-level directory such as ~/.openadapt/trust."
        )

    metadata_path = target_dir / METADATA_FILENAME
    pub_path = target_dir / PUBLIC_KEY_FILENAME
    priv_path = target_dir / PRIVATE_KEY_FILENAME

    if not force and (metadata_path.exists() or priv_path.exists()):
        raise TrustInitError(
            f"Trust key already exists in {target_dir}. Pass --force to overwrite."
        )

    target_dir.mkdir(parents=True, exist_ok=True)
    _enforce_dir_mode(target_dir)

    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    priv_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    raw_pub = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    fingerprint, key_id = compute_public_key_fingerprint(public_key)
    pub_b64 = base64.b64encode(raw_pub).decode("ascii")

    selected_backend: Optional[KeyStoreBackend] = None
    if storage in ("auto", "keychain"):
        selected_backend = _select_os_keystore(target_dir, keystore=keystore)
        if storage == "keychain" and selected_backend is None:
            raise TrustInitError(
                "OS keystore (Keychain / DPAPI / keyring) is unavailable; "
                "use --storage file for 0600 file-backed storage."
            )

    stored_priv_path: Optional[str] = None
    storage_backend_name = "file"
    if selected_backend is not None:
        try:
            selected_backend.store(key_id, priv_pem.decode("utf-8"))
            storage_backend_name = selected_backend.name
            if priv_path.exists():
                priv_path.unlink()
        except Exception as exc:
            if storage == "keychain":
                raise TrustInitError(f"Failed to store key in OS keystore: {exc}") from exc
            selected_backend = None

    if selected_backend is None:
        _write_private_file(priv_path, priv_pem)
        stored_priv_path = str(priv_path)
        storage_backend_name = "file"

    pub_path.write_bytes(pub_pem)
    if os.name == "posix":
        os.chmod(pub_path, 0o644)

    created_at = _utc_now_iso()
    result = TrustKeyResult(
        key_id=key_id,
        fingerprint_sha256=fingerprint,
        algorithm="Ed25519",
        storage_backend=storage_backend_name,
        key_dir=str(target_dir),
        metadata_path=str(metadata_path),
        public_key_path=str(pub_path),
        private_key_path=stored_priv_path,
        public_key_pem=pub_pem.decode("utf-8"),
        public_key_b64=pub_b64,
        created_at=created_at,
        owner=owner,
    )
    metadata_path.write_text(
        json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if os.name == "posix":
        os.chmod(metadata_path, 0o644)

    return result


def load_trust_metadata(key_dir: Optional[Path] = None) -> dict[str, Any]:
    """Load ``trust.json`` metadata from ``key_dir``."""
    target_dir = (key_dir or DEFAULT_TRUST_DIR).expanduser().resolve(strict=False)
    metadata_path = target_dir / METADATA_FILENAME
    if not metadata_path.is_file():
        raise TrustInitError(
            f"No trust metadata found at {metadata_path}. Run `openadapt trust init` first."
        )
    try:
        doc = json.loads(metadata_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise TrustInitError(f"Malformed trust metadata at {metadata_path}: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("schema") != TRUST_METADATA_SCHEMA:
        raise TrustInitError(f"Unsupported trust metadata schema in {metadata_path}.")
    return doc


def load_public_key(key_dir: Optional[Path] = None) -> Ed25519PublicKey:
    """Load the initialized Ed25519 public key from ``key_dir``."""
    target_dir = (key_dir or DEFAULT_TRUST_DIR).expanduser().resolve(strict=False)
    pub_path = target_dir / PUBLIC_KEY_FILENAME
    if not pub_path.is_file():
        raise TrustInitError(
            f"No public key found at {pub_path}. Run `openadapt trust init` first."
        )
    loaded = serialization.load_pem_public_key(pub_path.read_bytes())
    if not isinstance(loaded, Ed25519PublicKey):
        raise TrustInitError(f"Key at {pub_path} is not an Ed25519 public key.")
    return loaded


def load_private_key(
    key_dir: Optional[Path] = None,
    *,
    keystore: Optional[KeyStoreBackend] = None,
) -> Ed25519PrivateKey:
    """Load the initialized Ed25519 private key from file or OS keystore."""
    target_dir = (key_dir or DEFAULT_TRUST_DIR).expanduser().resolve(strict=False)
    meta = load_trust_metadata(target_dir)
    backend_name = meta.get("storage_backend", "file")
    key_id = str(meta.get("key_id") or "")

    if backend_name == "file":
        priv_path = target_dir / PRIVATE_KEY_FILENAME
        if not priv_path.is_file():
            raise TrustInitError(f"Private key file missing at {priv_path}.")
        if os.name == "posix":
            mode = stat.S_IMODE(priv_path.stat().st_mode)
            if mode & 0o077:
                raise TrustInitError(
                    f"Private key {priv_path} has overly broad permissions ({oct(mode)}); expected 0600."
                )
        raw = priv_path.read_bytes()
    else:
        backend = _select_os_keystore(target_dir, keystore=keystore)
        if backend is None:
            raise TrustInitError(
                f"Trust key {key_id} is stored in {backend_name}, which is unavailable."
            )
        secret = backend.load(key_id)
        if not secret:
            raise TrustInitError(
                f"Private key {key_id} was not found in keystore {backend.name}."
            )
        raw = secret.encode("utf-8")

    loaded = serialization.load_pem_private_key(raw, password=None)
    if not isinstance(loaded, Ed25519PrivateKey):
        raise TrustInitError("Loaded key is not an Ed25519 private key.")
    return loaded

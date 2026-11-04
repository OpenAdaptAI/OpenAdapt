"""Encryption at rest for attended state.

The attended store (:mod:`openadapt_flow.runtime.durable.attended`) keeps the
pause capability, its history, the decision log (operator and free-text
message), program and reconciliation receipts and the program-transition
control frames in the run directory. With a key configured (the default: the
configured key, else this machine's local key), each file is written as
``<name>.enc``: the same AES-256-GCM container durable checkpoints use, with
associated data that names the format version and the file's run-relative
path, so a ciphertext copied over another path does not decrypt.

scrypt runs once per run, not once per file. The run's marker holds one random
salt; the key scrypt derives from the passphrase and that salt is kept in
memory, and HKDF gives one subkey for files and one for journal rows. Each
container carries the salt it was sealed under, so a reader needs only the
passphrase. Containers written before this (one scrypt salt each) still
decrypt.

Older runs wrote these files in plaintext. A keyed store still reads a
plaintext file when no ``.enc`` form exists, until the run's first encrypted
write. That write converts every plaintext attended file to its encrypted form
and then leaves a marker; from then on a plaintext file standing in for an
encrypted one is refused.

The external attended journal (:mod:`openadapt_flow.runtime.durable.authority`)
stores each decision-log snapshot with :func:`seal_journal_snapshot`. A sealed
run directory (a sealed bundle's run, or strict mode) encrypts these files
through its sealed-set writer instead, and never reaches this module's writer.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Callable, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from openadapt_flow import crypto as _crypto
from openadapt_flow import sealed_set as _sealed

#: Suffix of an encrypted attended file (the checkpoint store uses the same).
ENC_SUFFIX = ".enc"
#: Associated-data prefix for an attended file: format version, then the
#: run-relative path.
FILE_AAD_PREFIX = b"openadapt-flow/attended-state/v1\0"
#: Associated-data prefix for one external journal snapshot.
JOURNAL_AAD_PREFIX = b"openadapt-flow/attended-journal/v1\0"
#: Prefix of an encrypted ``snapshot_json`` value. A stored value without it
#: is a plaintext snapshot from an older run.
JOURNAL_MARKER = "openadapt-flow/attended-journal/v1:"
#: Present once a run directory's attended state is encrypted. It holds no
#: recorded data.
MARKER_FILENAME = ".attended_state.encrypted"
MARKER_PAYLOAD = b'{"schema":"openadapt.flow.attended-state-encryption/v1"}\n'
#: The marker schema that carries the run salt.
MARKER_SCHEMA_V2 = "openadapt.flow.attended-state-encryption/v2"
#: Associated-data prefixes and the stored-row prefix for containers sealed
#: under the run key. Older (per-container scrypt) forms keep the v1 values.
FILE_AAD_PREFIX_V2 = b"openadapt-flow/attended-state/v2\0"
JOURNAL_AAD_PREFIX_V2 = b"openadapt-flow/attended-journal/v2\0"
JOURNAL_MARKER_V2 = "openadapt-flow/attended-journal/v2:"
#: HKDF ``info`` for the two subkeys of a run key.
FILE_SUBKEY_INFO = b"attended-state/v1"
JOURNAL_SUBKEY_INFO = b"attended-journal/v1"

#: Plaintext files converted on a run's first encrypted write.
CONVERTED_FILES = (
    "attended_capability.json",
    "attended_capability_history.json",
    "attended_decisions.json",
)
CONVERTED_DIRS = (
    ".attended_program_receipts",
    ".attended_reconciliation_receipts",
    "private/program-transition-controls",
)

Writer = Callable[[Path, bytes], None]


class AttendedStateUnreadable(ValueError):
    """An attended file can't be authenticated, decrypted or trusted."""


# Older containers each need their own scrypt run, and the attended store
# reads the same file several times per action. Keep a few of their
# authenticated plaintexts, keyed by the exact container and associated data,
# and reuse one only for the same passphrase. Containers sealed under a run
# key never use this cache.
_CACHE: "OrderedDict[tuple[bytes, bytes], tuple[str, bytes]]" = OrderedDict()
_CACHE_LIMIT = 64
_CACHE_LOCK = threading.Lock()


# One scrypt per (passphrase, run salt) per process. Keyed by the run salt;
# an entry is reused only for the same passphrase (compared in constant time,
# as the container cache above does), so a different passphrase derives again.
_RUN_KEYS: "OrderedDict[bytes, tuple[str, tuple[AESGCM, AESGCM]]]" = OrderedDict()
_RUN_KEY_LIMIT = 32
_RUN_KEY_LOCK = threading.Lock()
# A journal salt for a run directory with no salted marker (a sealed run
# directory, or a run whose first journal row comes before any file write).
_JOURNAL_SALTS: dict[str, bytes] = {}


def _run_keys(passphrase: str, salt: bytes) -> tuple[AESGCM, AESGCM]:
    """The (file, journal) AEADs for one run salt, deriving them only once."""
    with _RUN_KEY_LOCK:
        cached = _RUN_KEYS.get(salt)
        if cached is not None:
            _RUN_KEYS.move_to_end(salt)
    if cached is not None and hmac.compare_digest(
        cached[0].encode("utf-8"), passphrase.encode("utf-8")
    ):
        return cached[1]
    master = _crypto.derive_data_key(passphrase, salt)
    pair = tuple(
        AESGCM(
            HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(
                master
            )
        )
        for info in (FILE_SUBKEY_INFO, JOURNAL_SUBKEY_INFO)
    )
    keys = (pair[0], pair[1])
    with _RUN_KEY_LOCK:
        _RUN_KEYS[salt] = (passphrase, keys)
        while len(_RUN_KEYS) > _RUN_KEY_LIMIT:
            _RUN_KEYS.popitem(last=False)
    return keys


def _seal_salted(aead: AESGCM, salt: bytes, plaintext: bytes, aad: bytes) -> bytes:
    nonce = secrets.token_bytes(_crypto.NONCE_LEN)
    return (
        _crypto.SALTED_FILE_MAGIC + salt + nonce + aead.encrypt(nonce, plaintext, aad)
    )


def _split_salted(container: bytes) -> tuple[bytes, bytes, bytes]:
    body = container[len(_crypto.SALTED_FILE_MAGIC) :]
    salt_len, nonce_len = _crypto.SALT_LEN, _crypto.NONCE_LEN
    salt, nonce = body[:salt_len], body[salt_len : salt_len + nonce_len]
    ciphertext = body[salt_len + nonce_len :]
    if len(salt) != salt_len or len(nonce) != nonce_len or not ciphertext:
        raise AttendedStateUnreadable("a truncated attended container")
    return salt, nonce, ciphertext


def _open_salted(container: bytes, key: str, aad: bytes, *, journal: bool) -> bytes:
    salt, nonce, ciphertext = _split_salted(container)
    aead = _run_keys(key, salt)[1 if journal else 0]
    try:
        return aead.decrypt(nonce, ciphertext, aad)
    except InvalidTag as exc:
        raise AttendedStateUnreadable(
            "attended state does not decrypt: wrong key, or the file was "
            "changed or moved from another path"
        ) from exc


def _read_marker(root: Path) -> tuple[bool, Optional[bytes]]:
    """(marker present, its run salt). An older marker has no salt."""
    marker = Path(root) / MARKER_FILENAME
    if marker.is_symlink():
        raise AttendedStateUnreadable("the attended-state marker is a symlink")
    if not marker.is_file():
        return False, None
    data = marker.read_bytes()
    if data == MARKER_PAYLOAD:
        return True, None
    try:
        obj = json.loads(data)
        if obj.get("schema") != MARKER_SCHEMA_V2:
            raise ValueError("unknown marker schema")
        salt = base64.b64decode(obj["salt"], validate=True)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise AttendedStateUnreadable("the attended-state marker is invalid") from exc
    if len(salt) != _crypto.SALT_LEN:
        raise AttendedStateUnreadable("the attended-state marker is invalid")
    return True, salt


def _marker_payload(salt: bytes) -> bytes:
    body = {"schema": MARKER_SCHEMA_V2, "salt": base64.b64encode(salt).decode()}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def enc_path(path: Path) -> Path:
    return path.with_name(path.name + ENC_SUFFIX)


def file_aad(root: Path, path: Path, prefix: bytes = FILE_AAD_PREFIX) -> bytes:
    """Associated data binding a file to its run-relative path."""
    try:
        rel = Path(os.path.abspath(path)).relative_to(os.path.abspath(root))
    except ValueError as exc:
        raise AttendedStateUnreadable(
            "an attended file lies outside its run directory"
        ) from exc
    return prefix + rel.as_posix().encode("utf-8")


def seal(plaintext: bytes, key: str, aad: bytes) -> bytes:
    return _crypto.encrypt_bytes(plaintext, key, aad=aad)


def _require_key(key: Optional[str]) -> str:
    if not key:
        raise AttendedStateUnreadable(
            "attended state is encrypted and no key is configured; set "
            "OPENADAPT_BUNDLE_KEY or use the key it was written with"
        )
    return key


def open_file(root: Path, path: Path, container: bytes, key: Optional[str]) -> bytes:
    """Plaintext of the encrypted form of ``path``, in either container
    format, or raise :class:`AttendedStateUnreadable`."""
    key = _require_key(key)
    if container.startswith(_crypto.SALTED_FILE_MAGIC):
        _refuse_other_run(root, container)
        return _open_salted(
            container, key, file_aad(root, path, FILE_AAD_PREFIX_V2), journal=False
        )
    return open_sealed(container, key, file_aad(root, path))


def _refuse_other_run(root: Path, container: bytes) -> None:
    """A salted file belongs to the run whose marker holds its salt. One
    copied in from another run under the same key and relative path would
    otherwise decrypt, so it is refused, as is a salted file whose run has no
    salted marker."""
    _present, run_salt = _read_marker(root)
    if run_salt is None or _split_salted(container)[0] != run_salt:
        raise AttendedStateUnreadable(
            "attended state was not sealed for this run: its salt does not "
            "match the run's marker"
        )


def open_sealed(container: bytes, key: Optional[str], aad: bytes) -> bytes:
    """Authenticate and decrypt one older (per-container scrypt) container,
    or raise :class:`AttendedStateUnreadable`."""
    key = _require_key(key)
    cache_key = (hashlib.sha256(container).digest(), aad)
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached is not None:
            _CACHE.move_to_end(cache_key)
    if cached is not None and hmac.compare_digest(
        cached[0].encode("utf-8"), key.encode("utf-8")
    ):
        return cached[1]
    try:
        plaintext = _crypto.decrypt_bytes(container, key, aad=aad)
    except _crypto.CryptoError as exc:
        raise AttendedStateUnreadable(
            "attended state does not decrypt: wrong key, or the file was "
            "changed or moved from another path"
        ) from exc
    with _CACHE_LOCK:
        _CACHE[cache_key] = (key, plaintext)
        while len(_CACHE) > _CACHE_LIMIT:
            _CACHE.popitem(last=False)
    return plaintext


def _refuse_symlink(path: Path) -> None:
    if path.is_symlink() or enc_path(path).is_symlink():
        raise AttendedStateUnreadable("an attended file must not be a symlink")


def exists(path: Path) -> bool:
    """Whether ``path`` exists in either form."""
    return enc_path(path).is_file() or path.is_file()


def read_bytes(root: Path, path: Path, key: Optional[str]) -> bytes:
    """Plaintext of ``path``: its ``.enc`` form decrypted, else an older
    plaintext file. Raises ``FileNotFoundError`` when neither exists."""
    _refuse_symlink(path)
    enc = enc_path(path)
    if enc.is_file():
        return open_file(root, path, enc.read_bytes(), key)
    if path.is_file():
        _refuse_plaintext_in_encrypted_run(root, path)
        return path.read_bytes()
    raise FileNotFoundError(str(path))


def _is_attended_file(root: Path, path: Path) -> bool:
    """Whether ``path`` is one of the files a run encrypts at rest."""
    try:
        rel = Path(path).relative_to(Path(root))
    except ValueError:
        return False
    parent = rel.parent.as_posix()
    if parent == ".":
        return rel.name in CONVERTED_FILES
    return parent in CONVERTED_DIRS


def _refuse_plaintext_in_encrypted_run(root: Path, path: Path) -> None:
    if not _is_attended_file(root, path):
        return
    if (Path(root) / MARKER_FILENAME).exists() or (
        Path(root) / MARKER_FILENAME
    ).is_symlink():
        raise AttendedStateUnreadable(
            "a plaintext attended file stands in for encrypted state"
        )


def digest_bytes(root: Path, path: Path, key: Optional[str]) -> Optional[bytes]:
    """Plaintext for a state digest, or None when the file is missing. The
    digest is the same before and after a file is encrypted. A sealed run
    directory's files are the sealed-set writer's, read as they are."""
    if _sealed.is_sealed_path(path):
        return path.read_bytes() if path.is_file() else None
    enc = enc_path(path)
    if enc.is_file():
        return open_file(root, path, enc.read_bytes(), key)
    if path.is_file():
        _refuse_plaintext_in_encrypted_run(root, path)
        return path.read_bytes()
    return None


def logical_names(directory: Path, suffix: str = ".json") -> set[str]:
    """Names of the files in ``directory`` ending in ``suffix``, in either
    form (``x.json`` and ``x.json.enc`` both give ``x.json``)."""
    if not directory.is_dir():
        return set()
    names: set[str] = set()
    for entry in directory.iterdir():
        name = entry.name
        if name.endswith(suffix + ENC_SUFFIX):
            names.add(name[: -len(ENC_SUFFIX)])
        elif name.endswith(suffix):
            names.add(name)
    return names


def _link_new(path: Path, data: bytes) -> None:
    """Create ``path`` with ``data`` unless it already exists."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            pass
    finally:
        tmp.unlink(missing_ok=True)


def _ensure_encrypted(root: Path, key: str) -> bytes:
    """Make the run's attended state encrypted and return its run salt.

    A run with no marker gets a new salt: every plaintext attended file of an
    older run is encrypted under it, then the marker is created holding it.
    A file whose ``.enc`` form already exists keeps that form: a concurrent
    writer may have just replaced it. An older marker without a salt is
    replaced by one with a salt; its files keep their older containers."""
    marker = root / MARKER_FILENAME
    present, salt = _read_marker(root)
    if present and salt is not None:
        return salt
    if present:
        new_salt = _crypto.SetKey.new_salt()
        tmp = marker.with_name(f".{marker.name}.{secrets.token_hex(8)}.tmp")
        _link_new(tmp, _marker_payload(new_salt))
        os.replace(tmp, marker)
        present, salt = _read_marker(root)
        if salt is None:
            raise AttendedStateUnreadable("the attended-state marker is invalid")
        return salt
    new_salt = _crypto.SetKey.new_salt()
    file_key = _run_keys(key, new_salt)[0]
    candidates = [root / name for name in CONVERTED_FILES]
    for dirname in CONVERTED_DIRS:
        directory = root / dirname
        if directory.is_dir() and not directory.is_symlink():
            candidates.extend(directory / name for name in logical_names(directory))
    converted: list[tuple[Path, Path, bytes, bytes]] = []
    for path in candidates:
        if path.is_symlink() or not path.is_file():
            continue
        enc = enc_path(path)
        if not enc.exists():
            plaintext = path.read_bytes()
            container = _seal_salted(
                file_key,
                new_salt,
                plaintext,
                file_aad(root, path, FILE_AAD_PREFIX_V2),
            )
            _link_new(enc, container)
            converted.append((path, enc, plaintext, container))
        path.unlink(missing_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    _link_new(marker, _marker_payload(new_salt))
    # A concurrent first write may have created the marker first; its salt
    # is the run's. Files this call sealed under its own salt are sealed
    # again under the run's, unless another writer has replaced them since.
    present, salt = _read_marker(root)
    if salt is None:
        raise AttendedStateUnreadable("the attended-state marker is invalid")
    if salt != new_salt:
        run_key = _run_keys(key, salt)[0]
        for path, enc, plaintext, container in converted:
            if enc.is_symlink() or not enc.is_file() or enc.read_bytes() != container:
                continue
            tmp = enc.with_name(f".{enc.name}.{secrets.token_hex(8)}.tmp")
            _link_new(
                tmp,
                _seal_salted(
                    run_key, salt, plaintext, file_aad(root, path, FILE_AAD_PREFIX_V2)
                ),
            )
            os.replace(tmp, enc)
    return salt


def write_bytes(
    root: Path, path: Path, data: bytes, key: Optional[str], writer: Writer
) -> None:
    """Write ``path`` encrypted under ``key`` (as ``.enc``), or in plaintext
    when no key resolves (the recorded opt-out). A run whose attended state is
    already encrypted stays encrypted: with no key, the write is refused.
    ``writer`` performs the atomic replace."""
    _refuse_symlink(path)
    root = Path(root)
    if key:
        salt = _ensure_encrypted(root, key)
        container = _seal_salted(
            _run_keys(key, salt)[0],
            salt,
            data,
            file_aad(root, path, FILE_AAD_PREFIX_V2),
        )
        writer(enc_path(path), container)
        path.unlink(missing_ok=True)
        return
    if (root / MARKER_FILENAME).exists() or (root / MARKER_FILENAME).is_symlink():
        raise AttendedStateUnreadable(
            "this run's attended state is encrypted and no key is configured; "
            "refusing to write it in plaintext (OPENADAPT_FLOW_PLAINTEXT_AT_REST "
            "does not downgrade an encrypted run)"
        )
    if enc_path(path).exists():
        raise AttendedStateUnreadable(
            "an encrypted attended file exists and no key is configured; "
            "refusing to replace it with plaintext"
        )
    writer(path, data)


def unlink(path: Path) -> None:
    path.unlink(missing_ok=True)
    enc_path(path).unlink(missing_ok=True)


# -- the external journal ----------------------------------------------------


def _journal_aad(
    path_key: str, sequence: int, prefix: bytes = JOURNAL_AAD_PREFIX
) -> bytes:
    return prefix + f"{path_key}\0{sequence}".encode("utf-8")


def _journal_salt(run_dir: Optional[Path], path_key: str) -> bytes:
    """The run salt from the run's marker when it has one, else one salt per
    run path for this process. Reading the marker never writes the run."""
    if run_dir is not None:
        _present, salt = _read_marker(Path(run_dir))
        if salt is not None:
            return salt
    with _RUN_KEY_LOCK:
        salt = _JOURNAL_SALTS.get(path_key)
        if salt is None:
            salt = _crypto.SetKey.new_salt()
            _JOURNAL_SALTS[path_key] = salt
        return salt


def seal_journal_snapshot(
    snapshot_json: str,
    key: Optional[str],
    *,
    path_key: str,
    sequence: int,
    run_dir: Optional[Path] = None,
) -> str:
    """The stored form of one snapshot: encrypted and marked when a key is
    set, else the plaintext (the recorded opt-out)."""
    if not key:
        return snapshot_json
    salt = _journal_salt(run_dir, path_key)
    container = _seal_salted(
        _run_keys(key, salt)[1],
        salt,
        snapshot_json.encode("utf-8"),
        _journal_aad(path_key, sequence, JOURNAL_AAD_PREFIX_V2),
    )
    return JOURNAL_MARKER_V2 + base64.b64encode(container).decode("ascii")


def is_sealed_journal_snapshot(stored: str) -> bool:
    """Whether a stored snapshot is encrypted (either format)."""
    return stored.startswith((JOURNAL_MARKER, JOURNAL_MARKER_V2))


def open_journal_snapshot(
    stored: str, key: Optional[str], *, path_key: str, sequence: int
) -> str:
    """The plaintext of one stored snapshot (older rows are plaintext)."""
    if stored.startswith(JOURNAL_MARKER_V2):
        try:
            container = base64.b64decode(
                stored[len(JOURNAL_MARKER_V2) :], validate=True
            )
        except ValueError as exc:
            raise AttendedStateUnreadable(
                "an attended journal row is malformed"
            ) from exc
        if not container.startswith(_crypto.SALTED_FILE_MAGIC):
            raise AttendedStateUnreadable("an attended journal row is malformed")
        return _open_salted(
            container,
            _require_key(key),
            _journal_aad(path_key, sequence, JOURNAL_AAD_PREFIX_V2),
            journal=True,
        ).decode("utf-8")
    if not stored.startswith(JOURNAL_MARKER):
        return stored
    container = stored[len(JOURNAL_MARKER) :].encode("utf-8")
    return open_sealed(container, key, _journal_aad(path_key, sequence)).decode("utf-8")

"""Encryption at rest for attended state.

The attended store (:mod:`openadapt_flow.runtime.durable.attended`) keeps the
pause capability, its history, the decision log (operator and free-text
message), program and reconciliation receipts and the program-transition
control frames in the run directory. With a key configured (the default: the
configured key, else this machine's local key), each file is written as
``<name>.enc``: the same AES-256-GCM container durable checkpoints use, with
associated data that names the format version and the file's run-relative
path, so a ciphertext copied over another path does not decrypt.

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

import hashlib
import hmac
import os
import secrets
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Callable, Optional

from openadapt_flow import crypto as _crypto

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


# Decrypting runs scrypt, and the attended store reads the same file several
# times per action. Keep a few authenticated plaintexts, keyed by the exact
# container and associated data, and reuse one only for the same passphrase.
_CACHE: "OrderedDict[tuple[bytes, bytes], tuple[str, bytes]]" = OrderedDict()
_CACHE_LIMIT = 64
_CACHE_LOCK = threading.Lock()


def enc_path(path: Path) -> Path:
    return path.with_name(path.name + ENC_SUFFIX)


def file_aad(root: Path, path: Path) -> bytes:
    """Associated data binding a file to its run-relative path."""
    try:
        rel = Path(os.path.abspath(path)).relative_to(os.path.abspath(root))
    except ValueError as exc:
        raise AttendedStateUnreadable(
            "an attended file lies outside its run directory"
        ) from exc
    return FILE_AAD_PREFIX + rel.as_posix().encode("utf-8")


def seal(plaintext: bytes, key: str, aad: bytes) -> bytes:
    return _crypto.encrypt_bytes(plaintext, key, aad=aad)


def open_sealed(container: bytes, key: Optional[str], aad: bytes) -> bytes:
    """Authenticate and decrypt one container, or raise
    :class:`AttendedStateUnreadable`."""
    if not key:
        raise AttendedStateUnreadable(
            "attended state is encrypted and no key is configured; set "
            "OPENADAPT_BUNDLE_KEY or use the key it was written with"
        )
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
        return open_sealed(enc.read_bytes(), key, file_aad(root, path))
    if path.is_file():
        if (Path(root) / MARKER_FILENAME).exists():
            raise AttendedStateUnreadable(
                "a plaintext attended file stands in for encrypted state"
            )
        return path.read_bytes()
    raise FileNotFoundError(str(path))


def digest_bytes(root: Path, path: Path, key: Optional[str]) -> Optional[bytes]:
    """Plaintext for a state digest, or None when the file is missing. The
    digest is the same before and after a file is encrypted."""
    enc = enc_path(path)
    if enc.is_file():
        return open_sealed(enc.read_bytes(), key, file_aad(root, path))
    if path.is_file():
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


def _convert_plaintext(root: Path, key: str) -> None:
    """Encrypt every plaintext attended file of an older run, then mark the
    run. A file whose ``.enc`` form already exists keeps that form: a
    concurrent writer may have just replaced it."""
    marker = root / MARKER_FILENAME
    if marker.exists():
        return
    candidates = [root / name for name in CONVERTED_FILES]
    for dirname in CONVERTED_DIRS:
        directory = root / dirname
        if directory.is_dir() and not directory.is_symlink():
            candidates.extend(directory / name for name in logical_names(directory))
    for path in candidates:
        if path.is_symlink() or not path.is_file():
            continue
        enc = enc_path(path)
        if not enc.exists():
            _link_new(enc, seal(path.read_bytes(), key, file_aad(root, path)))
        path.unlink(missing_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    _link_new(marker, MARKER_PAYLOAD)


def write_bytes(
    root: Path, path: Path, data: bytes, key: Optional[str], writer: Writer
) -> None:
    """Write ``path`` encrypted under ``key`` (as ``.enc``), or in plaintext
    when no key resolves (the recorded opt-out). ``writer`` performs the
    atomic replace."""
    _refuse_symlink(path)
    root = Path(root)
    if key:
        _convert_plaintext(root, key)
        writer(enc_path(path), seal(data, key, file_aad(root, path)))
        path.unlink(missing_ok=True)
        return
    writer(path, data)
    enc_path(path).unlink(missing_ok=True)
    (root / MARKER_FILENAME).unlink(missing_ok=True)


def unlink(path: Path) -> None:
    path.unlink(missing_ok=True)
    enc_path(path).unlink(missing_ok=True)


# -- the external journal ----------------------------------------------------


def _journal_aad(path_key: str, sequence: int) -> bytes:
    return JOURNAL_AAD_PREFIX + f"{path_key}\0{sequence}".encode("utf-8")


def seal_journal_snapshot(
    snapshot_json: str, key: Optional[str], *, path_key: str, sequence: int
) -> str:
    """The stored form of one snapshot: encrypted and marked when a key is
    set, else the plaintext (the recorded opt-out)."""
    if not key:
        return snapshot_json
    container = seal(
        snapshot_json.encode("utf-8"), key, _journal_aad(path_key, sequence)
    )
    return JOURNAL_MARKER + container.decode("utf-8")


def open_journal_snapshot(
    stored: str, key: Optional[str], *, path_key: str, sequence: int
) -> str:
    """The plaintext of one stored snapshot (older rows are plaintext)."""
    if not stored.startswith(JOURNAL_MARKER):
        return stored
    container = stored[len(JOURNAL_MARKER) :].encode("utf-8")
    return open_sealed(container, key, _journal_aad(path_key, sequence)).decode("utf-8")

"""Encryption at rest: the key, the strict mode, and the plaintext ledger.

Recordings, bundles, run directories (screenshots, reports, evidence) and
durable checkpoints can hold patient data. Flow encrypts them with
:mod:`openadapt_flow.sealed_set` and :mod:`openadapt_flow.crypto`, by default
and with no setup.

* The key is ``OPENADAPT_BUNDLE_KEY`` (or ``OPENADAPT_RECORDING_KEY``) when
  set. Otherwise it is a per-user key Flow generates on first use and keeps in
  the OS keychain (macOS Keychain, Windows Credential Manager, Linux Secret
  Service, through ``keyring``). A small marker,
  ``$OPENADAPT_HOME/keys/at-rest.key.store.json``, records which keychain
  entry holds it and a check value; it holds no key material. Where no
  keychain works (a headless Linux box without Secret Service, say), the key
  goes to ``$OPENADAPT_HOME/keys/at-rest.key`` with mode 0600 in a 0700
  directory, with a warning. ``OPENADAPT_FLOW_KEY_STORE`` picks the store
  (``auto``, the default; ``keychain``; ``file``). A key file made by an
  earlier version keeps being used, so nothing it encrypted is orphaned.
  ``OPENADAPT_FLOW_AT_REST_HOME`` moves the marker, the file and the ledger
  (default ``$OPENADAPT_HOME``, else ``~/.openadapt``). Back the key up: what
  it encrypts can't be read without it.
* Recordings, bundles, run directories and durable checkpoints are encrypted
  under that key by default (:data:`DEFAULT_SEALS_ARTIFACTS`).
* ``OPENADAPT_FLOW_AT_REST=strict``, the setting for healthcare and regulated
  deployments, also refuses the outputs that aren't encrypted yet
  (:func:`unsealed_write`).
* In the default mode, each output that still can't be encrypted goes through
  :func:`unsealed_write`: stderr names the path and a ledger line
  (``plaintext-at-rest.jsonl``) records it, and Desktop shows the ledger as a
  notice.
* ``OPENADAPT_ENCRYPT_AT_REST=0`` (or ``record --no-encrypt``, ``compile
  --no-encrypt``, ``run --no-encrypt``) turns encryption off, and
  ``OPENADAPT_FLOW_PLAINTEXT_AT_REST='<reason>'`` does the same with a reason.
  Each prints a one-line warning and adds a ledger line. Strict mode ignores
  both. Files encrypted earlier still read.

A keychain marker whose entry is gone, locked or different fails closed with
:class:`LocalKeyError`; Flow never mints a second key over it.

Stdlib only (``keyring`` is imported lazily), so :mod:`openadapt_flow.crypto`
can import it on every load.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Final, Optional

from openadapt_flow.private_file import open_binary

OPT_OUT_ENV: Final[str] = "OPENADAPT_FLOW_PLAINTEXT_AT_REST"
MODE_ENV: Final[str] = "OPENADAPT_FLOW_AT_REST"
#: ``0`` turns encryption at rest off (with a warning and a ledger line).
ENCRYPT_ENV: Final[str] = "OPENADAPT_ENCRYPT_AT_REST"
#: Where a new local key is kept: ``auto`` (the OS keychain, else a file),
#: ``keychain`` (the keychain or an error) or ``file``.
KEY_STORE_ENV: Final[str] = "OPENADAPT_FLOW_KEY_STORE"
LEDGER_NAME: Final[str] = "plaintext-at-rest.jsonl"
KEY_NAME: Final[str] = "at-rest.key"
MARKER_NAME: Final[str] = "device-key.store.json"
DEVICE_KEY_NAME: Final[str] = "device-software.key"
KEYCHAIN_SERVICE: Final[str] = "openadapt-flow-at-rest"
_MARKER_SCHEMA: Final[str] = "openadapt.flow.at-rest-key-store/v1"
_CHECK_LABEL: Final[bytes] = b"openadapt-flow/device-key-check/v1"
#: How long a reader waits for another process that is creating the key.
_CREATE_WAIT_S: Final[float] = 5.0
#: How long one keychain call may take before Flow gives up on it.
_KEYCHAIN_TIMEOUT_S: Final[float] = 30.0

_KEY_BYTES: Final[int] = 32
_lock = threading.Lock()
_warned: set[str] = set()


class LocalKeyError(RuntimeError):
    """The local at-rest key is unreadable, unsafe or malformed."""


class AtRestRefused(RuntimeError):
    """Strict mode refused a write that can't be sealed yet."""


def strict() -> bool:
    """Whether ``OPENADAPT_FLOW_AT_REST=strict``. Any other non-empty value
    raises, so a mistyped setting never runs in the weaker default mode."""
    value = (os.environ.get(MODE_ENV) or "").strip().lower()
    if value in ("", "default"):
        return False
    if value == "strict":
        return True
    raise ValueError(f"{MODE_ENV} takes 'strict' or 'default', not {value!r}")


_OFF: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})
_ON: Final[frozenset[str]] = frozenset({"", "1", "true", "yes", "on"})


def encrypt_setting() -> bool:
    """``OPENADAPT_ENCRYPT_AT_REST``: True (the default) unless set to ``0``,
    ``false``, ``no`` or ``off``. Any other value raises, so a mistyped
    setting never turns encryption off."""
    value = (os.environ.get(ENCRYPT_ENV) or "").strip().lower()
    if value in _ON:
        return True
    if value in _OFF:
        return False
    raise ValueError(f"{ENCRYPT_ENV} takes 1 or 0, not {value!r}")


def opt_out_for_this_process(source: str) -> None:
    """Turn encryption off for this process and its children (``--no-encrypt``).

    The one-line warning and the ledger line follow at the first write.
    """
    os.environ[ENCRYPT_ENV] = "0"
    if not (os.environ.get(OPT_OUT_ENV) or "").strip():
        os.environ[OPT_OUT_ENV] = source


def explicit_key() -> bool:
    """Whether the deployment configured a key (rather than the local one)."""
    return bool(
        os.environ.get("OPENADAPT_BUNDLE_KEY")
        or os.environ.get("OPENADAPT_RECORDING_KEY")
    )


def hold_back_in_strict(artifact: str) -> None:
    """Refuse, before any record changes, a library entry point whose output
    can't be encrypted yet, when strict mode is on."""
    if strict():
        raise AtRestRefused(
            f"Not starting: {artifact} is written in plaintext, and "
            f"{MODE_ENV}=strict holds it back. Nothing was executed."
        )


def unsealed_write(artifact: str, path: Path | str, *, refuse: bool = True) -> None:
    """Allow a plaintext write of an artifact Flow can't seal yet, or refuse it.

    Strict mode raises :class:`AtRestRefused`. Otherwise the write goes ahead:
    stderr names ``path`` and a ledger line records it, once per path in a
    process.

    ``refuse=False`` is for a report written after a run already changed a
    record: refusing then would lose the only account of what was written.
    It warns and records even in strict mode; the commands that reach these
    writers are refused before they run instead.
    """
    if refuse and strict():
        raise AtRestRefused(
            f"Not writing {artifact} at {path}: it can't be encrypted yet, and "
            f"{MODE_ENV}=strict refuses plaintext. Nothing was written."
        )
    try:
        _record(
            f"{artifact} at {path}",
            "can't be encrypted yet",
            path=str(path),
            warning=(
                f"WARNING: writing {artifact} in plaintext at {path}: it can't be "
                "encrypted yet and can hold patient data. Set "
                f"{MODE_ENV}=strict to refuse instead."
            ),
        )
    except AtRestRefused as exc:
        if refuse:
            raise
        # After records changed, the report is the only account of what was
        # written: keep it, and say loudly that the ledger missed it.
        print(
            f"ERROR: {artifact} at {path} is kept in plaintext so the account "
            f"of what changed isn't lost, but the plaintext ledger at "
            f"{ledger_path()} could not record it ({exc.__cause__ or exc}).",
            file=sys.stderr,
        )


HOME_ENV: Final[str] = "OPENADAPT_FLOW_AT_REST_HOME"


def _home() -> Path:
    # Its own override first, so a test suite or a service can keep the key
    # and the ledger apart from the rest of the engine's state.
    root = os.environ.get(HOME_ENV) or os.environ.get("OPENADAPT_HOME")
    return Path(root) if root else Path.home() / ".openadapt"


def local_key_path() -> Path:
    """Where the locally generated at-rest key lives."""
    return _home() / "keys" / KEY_NAME


def recent_plaintext_writes(limit: int = 20) -> list[dict]:
    """The last ``limit`` ledger lines, newest last (for the Desktop notice)."""
    try:
        lines = ledger_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines[-limit:]:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def ledger_path() -> Path:
    """The record of every process that wrote plaintext under the opt-out."""
    return _home() / LEDGER_NAME


def opt_out_reason() -> Optional[str]:
    """The reason given in ``OPENADAPT_FLOW_PLAINTEXT_AT_REST``, or None.

    None in strict mode, which ignores the opt-out.
    """
    reason = (os.environ.get(OPT_OUT_ENV) or "").strip()
    if not encrypt_setting():
        reason = reason or f"{ENCRYPT_ENV}=0"
    if strict() and reason:
        with _lock:
            first = "strict-ignores-opt-out" not in _warned
            _warned.add("strict-ignores-opt-out")
        if first:
            print(
                f"WARNING: {OPT_OUT_ENV} is ignored because {MODE_ENV}=strict.",
                file=sys.stderr,
            )
        return None
    return reason or None


def plaintext_allowed(purpose: str) -> bool:
    """Whether ``purpose`` may be written in plaintext, recording it if so.

    True only under the opt-out. The first time each purpose is used in a
    process, a warning goes to stderr and a line to :func:`ledger_path`.
    """
    reason = opt_out_reason()
    if reason is None:
        return False
    record_plaintext(purpose, reason)
    return True


def record_plaintext(purpose: str, reason: str) -> None:
    """Warn and record that ``purpose`` is written in plaintext for ``reason``.

    Used directly by an explicit per-command opt-out such as
    ``record --no-encrypt``.
    """
    _record(
        purpose,
        reason,
        warning=(
            f"WARNING: writing {purpose} in plaintext at rest ({reason!r}). "
            "It can hold patient data; encryption is the default."
        ),
    )


def _record(
    purpose: str, reason: str, *, warning: str, path: Optional[str] = None
) -> None:
    strict()  # an unknown mode stops here too, not only at the CLI
    with _lock:
        if purpose in _warned:
            return
        _warned.add(purpose)
    print(warning, file=sys.stderr)
    try:
        ledger = ledger_path()
        ledger.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "schema": "openadapt.flow.plaintext-at-rest/v1",
                "recorded_at": time.time(),
                "purpose": purpose,
                "reason": reason,
                "path": path,
                "pid": os.getpid(),
            },
            sort_keys=True,
        )
        fd = open_binary(ledger, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError as exc:
        # An unrecorded plaintext write is the silent case this ledger exists
        # to prevent, so the write stops here.
        with _lock:
            _warned.discard(purpose)
        raise AtRestRefused(
            f"Not writing {purpose}: the plaintext ledger at {ledger_path()} "
            f"can't be written ({exc}). Set {HOME_ENV} to a writable private "
            "directory. Nothing was written."
        ) from exc


def refusal(purpose: str, why: str) -> str:
    """The message for a write that would be plaintext and has no opt-out."""
    return (
        f"Not writing {purpose}: {why}, and Flow does not write patient data "
        f"in plaintext by default. Set {OPT_OUT_ENV}='<reason>' to allow "
        "plaintext for development or de-identified data (recorded)."
    )


def _check_mode(path: Path) -> None:
    if os.name != "posix":
        return
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise LocalKeyError(f"{path} is not a regular file. Nothing was read.")
    for item, label, fix in (
        (info, str(path), f"chmod 600 {path}"),
        (path.parent.stat(), str(path.parent), f"chmod 700 {path.parent}"),
    ):
        if item.st_uid != os.getuid():
            raise LocalKeyError(
                f"{label} belongs to another user. Nothing was read or written."
            )
        if item.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise LocalKeyError(
                f"{label} is open to group or others (mode "
                f"{oct(item.st_mode & 0o777)}); run {fix}. Nothing was read or "
                "written."
            )


def _read(path: Path) -> str:
    _check_mode(path)
    text = path.read_text(encoding="ascii").strip()
    try:
        raw = base64.urlsafe_b64decode(text.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise LocalKeyError(f"{path} does not hold a Flow at-rest key") from exc
    if len(raw) != _KEY_BYTES:
        raise LocalKeyError(f"{path} does not hold a Flow at-rest key")
    return text


# -- the local key -------------------------------------------------------------


class KeyStore:
    """Where a generated key can live outside the file system: the interface
    of the OS keychain store. Tests inject an in-memory one
    (:func:`use_key_store`) so they never touch the user's real keychain."""

    name = "keychain"

    def get(self, account: str) -> Optional[str]:  # pragma: no cover - interface
        raise NotImplementedError

    def set(self, account: str, value: str) -> None:  # pragma: no cover
        raise NotImplementedError


class KeychainStore(KeyStore):
    """The OS keychain through ``keyring``: macOS Keychain, Windows Credential
    Manager (DPAPI) and Linux Secret Service. Every call runs with a timeout,
    so a keychain that waits on a prompt can't hang a headless run."""

    def __init__(self, service: str = KEYCHAIN_SERVICE) -> None:
        self.service = service

    def _call(self, what: str, fn: str, *args: str) -> Optional[str]:
        try:
            import keyring
        except Exception as exc:  # noqa: BLE001 - not installed, or broken
            raise KeyStoreUnavailable(f"keyring is not importable ({exc})") from exc
        result: dict[str, object] = {}

        def work() -> None:
            try:
                result["value"] = getattr(keyring, fn)(self.service, *args)
            except BaseException as exc:  # noqa: BLE001 - reported below
                result["error"] = exc

        thread = threading.Thread(target=work, name="openadapt-keychain", daemon=True)
        thread.start()
        thread.join(_KEYCHAIN_TIMEOUT_S)
        if thread.is_alive():
            raise KeyStoreUnavailable(
                f"the OS keychain did not answer a {what} within "
                f"{_KEYCHAIN_TIMEOUT_S:.0f} s"
            )
        if "error" in result:
            exc = result["error"]
            raise KeyStoreUnavailable(
                f"the OS keychain refused a {what} ({type(exc).__name__})"
            ) from exc  # type: ignore[misc]
        value = result.get("value")
        return value if isinstance(value, str) else None

    def get(self, account: str) -> Optional[str]:
        return self._call("read", "get_password", account)

    def set(self, account: str, value: str) -> None:
        self._call("write", "set_password", account, value)


class KeyStoreUnavailable(RuntimeError):
    """The OS keychain can't be used: no backend, locked, denied or slow."""


_injected_store: Optional[KeyStore] = None


def use_key_store(store: Optional[KeyStore]) -> None:
    """Replace the OS keychain store (tests); ``None`` restores it."""
    global _injected_store
    _injected_store = store


def _keychain() -> KeyStore:
    return _injected_store if _injected_store is not None else KeychainStore()


def _store_choice() -> str:
    configured = os.environ.get(KEY_STORE_ENV)
    if (
        not configured
        and _injected_store is None
        and "PYTEST_CURRENT_TEST" in os.environ
    ):
        # A safety belt: a test that forgot to isolate the key store must
        # never write to, or raise a dialog from, the person's real keychain.
        return "file"
    value = (configured or "auto").strip().lower()
    if value not in ("auto", "keychain", "file"):
        raise ValueError(
            f"{KEY_STORE_ENV} takes 'auto', 'keychain' or 'file', not {value!r}"
        )
    return value


def marker_path() -> Path:
    """The non-secret record of which keychain entry holds the local key."""
    return _home() / "keys" / MARKER_NAME


def _account() -> str:
    # One entry per Flow home, so a service or a test suite with its own
    # OPENADAPT_FLOW_AT_REST_HOME never shares the person's key.
    return f"device-key:{_home().expanduser().resolve()}"


#: Prefix of a software device key: a P-256 private scalar, base64url.
SOFTWARE_KEY_PREFIX: Final[str] = "OADEV-P256-1:"


def _valid_secret(text: Optional[str]) -> bool:
    if not text or not text.startswith(SOFTWARE_KEY_PREFIX):
        return False
    try:
        raw = base64.urlsafe_b64decode(text[len(SOFTWARE_KEY_PREFIX) :].encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        return False
    return len(raw) == 32


def _check(text: str) -> str:
    import hashlib
    import hmac

    return hmac.new(text.encode("ascii"), _CHECK_LABEL, hashlib.sha256).hexdigest()


def _read_secret(path: Path) -> str:
    _check_mode(path)
    text = path.read_text(encoding="ascii").strip()
    if not _valid_secret(text):
        raise LocalKeyError(f"{path} does not hold a Flow device key")
    return text


def _read_marker(path: Path) -> Optional[dict[str, str]]:
    if not path.exists():
        return None
    _check_mode(path)
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LocalKeyError(
            f"{path} does not record where Flow's encryption key is. Nothing "
            "was read or written."
        ) from exc
    if (
        not isinstance(marker, dict)
        or marker.get("schema") != _MARKER_SCHEMA
        or marker.get("store") != "keychain"
        or not isinstance(marker.get("account"), str)
        or not isinstance(marker.get("service"), str)
        or not isinstance(marker.get("check"), str)
    ):
        raise LocalKeyError(
            f"{path} does not record where Flow's encryption key is. Nothing "
            "was read or written."
        )
    return marker


def _from_keychain(marker: dict[str, str], path: Path) -> Optional[str]:
    """The key the marker names, or None while its creator is still writing
    it. Raises when the keychain can't be read or holds another value."""
    store = _keychain()
    if isinstance(store, KeychainStore):
        store = KeychainStore(marker["service"])
    try:
        text = store.get(marker["account"])
    except KeyStoreUnavailable as exc:
        raise LocalKeyError(
            f"Flow's encryption key is in the OS keychain ({path} says so), but "
            f"{exc}. Unlock the keychain or allow access, or set "
            "OPENADAPT_BUNDLE_KEY. Nothing was read or written."
        ) from exc
    if text is None:
        return None
    if not _valid_secret(text) or _check(text) != marker["check"]:
        raise LocalKeyError(
            f"The OS keychain entry {marker['service']}/{marker['account']} "
            f"does not hold the key {path} names. Nothing was read or written; "
            "restore the entry from your backup, or set OPENADAPT_BUNDLE_KEY."
        )
    return text


def device_key_path() -> Path:
    """Where the device key lives when no OS keychain holds it."""
    return _home() / "keys" / DEVICE_KEY_NAME


def device_secret(*, create: bool) -> Optional[str]:
    """This computer's device identity (an age X25519 secret), from the OS
    keychain or the 0600 key file; created on first use when ``create``."""
    path = device_key_path()
    try:
        with _lock:
            found = _existing(path, wait=create)
            if found is not None or not create:
                return found
            return _create_device(path)
    except OSError as exc:
        raise LocalKeyError(
            f"Flow could not create or read its device key ({exc}). Set "
            f"OPENADAPT_BUNDLE_KEY, or {HOME_ENV} to a writable private "
            "directory. Nothing was written."
        ) from exc


def local_key() -> str:
    """This computer's local passphrase for the stores that take one (durable
    checkpoints, attended state, the version 1 sets an earlier version
    wrote), created on first use at ``keys/at-rest.key`` (0600)."""
    path = local_key_path()
    try:
        return _local_passphrase(path)
    except OSError as exc:
        raise LocalKeyError(
            f"Flow could not create or read its local encryption key at {path} "
            f"({exc}). Set OPENADAPT_BUNDLE_KEY, or {HOME_ENV} to a writable "
            "private directory. Nothing was written."
        ) from exc


def _local_passphrase(path: Path) -> str:
    with _lock:
        if path.exists():
            return _read(path)
        _private_dir(path.parent)
        text = base64.urlsafe_b64encode(secrets.token_bytes(_KEY_BYTES)).decode()
        if not _link_new(path, (text + "\n").encode("ascii")):
            return _read(path)
        print(
            "Created a local encryption key for recordings, bundles and runs "
            f"at {path}. Back it up: what it encrypts can't be read without it.",
            file=sys.stderr,
        )
        return text


def _existing(path: Path, *, wait: bool) -> Optional[str]:
    """The device secret already kept for this home: the key file, else the
    keychain entry the marker names. None when there is none yet."""
    deadline = time.monotonic() + (_CREATE_WAIT_S if wait else 0.0)
    marker_file = path.with_name(MARKER_NAME)
    while True:
        if path.exists():
            return _read_secret(path)
        marker = _read_marker(marker_file)
        if marker is None:
            return None
        text = _from_keychain(marker, marker_file)
        if text is not None:
            return text
        if time.monotonic() >= deadline:
            raise LocalKeyError(
                f"Flow's device key should be in the OS keychain ({marker_file} "
                f"says so), but the entry {marker['service']}/{marker['account']} "
                "is missing. Nothing was read or written; restore it from your "
                "backup, or set OPENADAPT_BUNDLE_KEY. Flow does not make a new "
                "key over it."
            )
        time.sleep(0.05)  # another process is between its marker and its write


def _private_dir(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        os.chmod(directory, 0o700)


def _link_new(path: Path, data: bytes) -> bool:
    """Create ``path`` holding ``data`` (mode 0600) only if it doesn't exist.

    A private temporary file is linked into place, which fails if another
    process got there first, so no reader sees a half-written file.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}")
    fd = open_binary(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.link(tmp, path)
    except FileExistsError:
        return False
    finally:
        tmp.unlink()
    return True


def _create_device(path: Path) -> str:
    from cryptography.hazmat.primitives.asymmetric import ec

    _private_dir(path.parent)
    scalar = ec.generate_private_key(ec.SECP256R1()).private_numbers().private_value
    text = SOFTWARE_KEY_PREFIX + base64.urlsafe_b64encode(
        scalar.to_bytes(32, "big")
    ).decode("ascii")
    choice = _store_choice()
    why_not = ""
    if choice != "file":
        stored, why_not = _create_in_keychain(path, text)
        if stored is not None:
            return stored
        if choice == "keychain":
            raise LocalKeyError(
                f"{KEY_STORE_ENV}=keychain, but {why_not}. Nothing was written."
            )
    if not _link_new(path, (text + "\n").encode("ascii")):
        return _read_secret(path)
    if choice == "auto":
        print(
            f"WARNING: no OS keychain is available ({why_not}), so Flow keeps "
            f"its device key in {path}, readable only by you. Back it up: what "
            "it encrypts can't be read without it.",
            file=sys.stderr,
        )
    else:
        print(
            "Created a local encryption key for recordings, bundles and runs "
            f"at {path}. Back it up: what it encrypts can't be read without it.",
            file=sys.stderr,
        )
    return text


def _create_in_keychain(path: Path, text: str) -> tuple[Optional[str], str]:
    """Store a new key in the keychain. Returns ``(key, "")`` on success, or
    ``(None, why)`` when the keychain can't hold it (nothing was claimed)."""
    marker_file = path.with_name(MARKER_NAME)
    store = _keychain()
    account = _account()
    service = store.service if isinstance(store, KeychainStore) else KEYCHAIN_SERVICE
    try:
        if store.get(account) is not None:
            # An entry without a marker: left by a crash between the two, or
            # by another tool. It may hold data's only key, so never replace it.
            raise LocalKeyError(
                f"The OS keychain already has an entry {service}/{account}, but "
                f"{marker_file} is missing. Nothing was written. Restore the "
                "marker from your backup, or remove the entry if nothing was "
                "encrypted under it."
            )
    except KeyStoreUnavailable as exc:
        return None, str(exc)
    marker = {
        "schema": _MARKER_SCHEMA,
        "store": "keychain",
        "service": service,
        "account": account,
        "check": _check(text),
        "created_at": time.time(),
    }
    body = (json.dumps(marker, indent=2, sort_keys=True) + "\n").encode("utf-8")
    # The marker is claimed first: a second process waits for this key
    # instead of writing its own over it.
    if not _link_new(marker_file, body):
        return _existing(path, wait=True), ""
    try:
        store.set(account, text)
        back = store.get(account)
    except KeyStoreUnavailable as exc:
        marker_file.unlink(missing_ok=True)
        return None, str(exc)
    if back != text:
        marker_file.unlink(missing_ok=True)
        return None, "the OS keychain did not keep the key"
    print(
        "Created a local encryption key for recordings, bundles and runs at "
        f"the OS keychain entry {service}. Back it up: what it encrypts can't "
        "be read without it.",
        file=sys.stderr,
    )
    return text, ""


def key_location() -> Optional[str]:
    """Where this home's key is kept, for messages: ``keychain``, a file path,
    or None when there is no key yet. Reads no key material."""
    for path in (local_key_path(), device_key_path()):
        if path.exists():
            return str(path)
    if device_key_path().with_name(MARKER_NAME).exists():
        return "keychain"
    return None


#: Whether the default mode seals new recordings, bundles and run
#: directories. Strict mode always does; ``OPENADAPT_ENCRYPT_AT_REST=0`` and
#: ``OPENADAPT_FLOW_PLAINTEXT_AT_REST`` turn it off, recorded.
DEFAULT_SEALS_ARTIFACTS: Final[bool] = True


def seal_new(artifact: str, path: Path | str) -> bool:
    """Whether a new bundle or recording at ``path`` is written sealed.

    True in strict mode, and by default. Otherwise (the opt-out, or a default
    mode that doesn't seal it) the plaintext write is named and recorded.
    """
    if strict():
        return True
    if opt_out_reason() is not None:
        plaintext_allowed(artifact)
        return False
    if DEFAULT_SEALS_ARTIFACTS:
        return True
    unsealed_write(artifact, path)
    return False


def seals_by_default() -> bool:
    """Whether new artifacts are sealed now, without recording anything:
    strict mode, or the default mode without the opt-out."""
    return strict() or (DEFAULT_SEALS_ARTIFACTS and opt_out_reason() is None)


def existing_key() -> Optional[str]:
    """The local key if it already exists, for read paths: creates nothing
    and records nothing. None when there is no local key yet."""
    path = local_key_path()
    try:
        if not path.exists():
            return None
        return _read(path)
    except OSError as exc:
        raise LocalKeyError(f"{path} can't be read ({exc}).") from exc


def default_key() -> Optional[str]:
    """The key to use when none is configured: the local key, or None under
    the opt-out (plaintext, the behaviour before encryption was the default).
    Strict mode ignores the opt-out.
    """
    reason = opt_out_reason()
    if reason is not None:
        record_plaintext("files Flow encrypts when a key is set", reason)
        return None
    return local_key()


def _reset_for_tests() -> None:
    with _lock:
        _warned.clear()

"""Encryption at rest: the key, the strict mode, and the plaintext ledger.

Recordings, bundles, run directories (screenshots, reports, evidence) and
durable checkpoints can hold patient data. Flow encrypts them with
:mod:`openadapt_flow.sealed_set` and :mod:`openadapt_flow.crypto`.

* The key is ``OPENADAPT_BUNDLE_KEY`` (or ``OPENADAPT_RECORDING_KEY``) when
  set. Otherwise it is a key Flow generates on this machine,
  ``$OPENADAPT_HOME/keys/at-rest.key`` (default ``~/.openadapt``;
  ``OPENADAPT_FLOW_AT_REST_HOME`` moves it), created on first use with mode
  0600 in a 0700 directory and refused if anyone but the owner can read it.
  Back it up: what it encrypts can't be read without it.
* Durable checkpoints are encrypted under that key by
  default.
* ``OPENADAPT_FLOW_AT_REST=strict``, the setting for healthcare and regulated
  deployments, also encrypts every recording, bundle and run directory, and
  refuses the outputs that aren't encrypted yet (:func:`unsealed_write`).
* In the default mode, each recording, bundle, run directory or other output
  written in plaintext goes through :func:`unsealed_write` or :func:`seal_new`:
  stderr names the path and a ledger line (``plaintext-at-rest.jsonl``)
  records it, and Desktop shows the ledger as a notice. Once every consumer
  reads encrypted artifacts (#1751), the default encrypts them too
  (:data:`DEFAULT_SEALS_ARTIFACTS`).
* ``OPENADAPT_FLOW_PLAINTEXT_AT_REST='<reason>'`` turns encryption off for
  development or de-identified data, with a warning and a ledger line. Strict
  mode ignores it.

Stdlib only, so :mod:`openadapt_flow.crypto` can import it on every load.
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

OPT_OUT_ENV: Final[str] = "OPENADAPT_FLOW_PLAINTEXT_AT_REST"
MODE_ENV: Final[str] = "OPENADAPT_FLOW_AT_REST"
LEDGER_NAME: Final[str] = "plaintext-at-rest.jsonl"
KEY_NAME: Final[str] = "at-rest.key"

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
        fd = os.open(ledger, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
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


def local_key() -> str:
    """Return this machine's at-rest key, creating it on first use."""
    path = local_key_path()
    try:
        return _local_key(path)
    except OSError as exc:
        raise LocalKeyError(
            f"Flow could not create or read its local encryption key at {path} "
            f"({exc}). Set OPENADAPT_BUNDLE_KEY, or {HOME_ENV} to a writable "
            "private directory. Nothing was written."
        ) from exc


def _local_key(path: Path) -> str:
    with _lock:
        if path.exists():
            return _read(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            os.chmod(path.parent, 0o700)
        text = base64.urlsafe_b64encode(secrets.token_bytes(_KEY_BYTES)).decode()
        # Write a private temporary file, then link it into place, which
        # fails if another process got there first: no reader ever sees a
        # half-written key, and no key is ever replaced.
        tmp = path.with_name(f".{KEY_NAME}.{os.getpid()}.{secrets.token_hex(4)}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, (text + "\n").encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(tmp, path)
        except FileExistsError:
            return _read(path)
        finally:
            tmp.unlink()
        print(
            f"Created a local encryption key for recordings, bundles and runs "
            f"at {path}. Back it up: what it encrypts can't be read without it.",
            file=sys.stderr,
        )
        return text


#: Whether the default mode seals new recordings and bundles. Strict mode
#: always does. It turns on once every consumer reads sealed artifacts
#: (#1751); until then the default names and records each plaintext write.
DEFAULT_SEALS_ARTIFACTS: Final[bool] = False


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

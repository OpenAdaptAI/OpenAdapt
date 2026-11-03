"""Encryption at rest by default: a local key, or a loud, recorded opt-out.

Recordings, bundles, run directories (screenshots, reports, evidence) and
durable checkpoints can hold patient data. Flow encrypts them whenever a key
is configured (:mod:`openadapt_flow.sealed_set`, :mod:`openadapt_flow.crypto`).
This module decides what happens when none is: Flow does not fall back to
plaintext.

* With ``OPENADAPT_BUNDLE_KEY`` (or ``OPENADAPT_RECORDING_KEY``) set, that key
  is used, as before.
* Otherwise Flow uses a key it generated on this machine,
  ``$OPENADAPT_HOME/keys/at-rest.key`` (default ``~/.openadapt``;
  ``OPENADAPT_FLOW_AT_REST_HOME`` overrides the directory). It is
  created on first use with mode 0600 in a 0700 directory, and refused if
  anyone but the owner can read it. Losing it loses every artifact sealed
  with it, so back it up like any other secret.
* ``OPENADAPT_FLOW_PLAINTEXT_AT_REST='<reason>'`` turns encryption off for
  development or for data that is already de-identified. Each process that
  writes plaintext prints a warning to stderr and appends one line to
  ``$OPENADAPT_HOME/plaintext-at-rest.jsonl`` with the reason.

Some artifacts can't be sealed yet: run directories (the offline Seal, undo
and other readers don't read sealed runs yet), approval and attended state,
desktop recordings, and the output of ``visualize``, ``explain``, ``teach``,
``emit-skill`` and ``push``. By default they are still written in plaintext,
and each write goes through :func:`unsealed_write`, which names the path on
stderr and adds a ledger line that Desktop shows as a notice.
``OPENADAPT_FLOW_AT_REST=strict``, the setting for healthcare and regulated
deployments, refuses them instead, seals every run directory, and ignores the
opt-out. ``docs/phi_at_rest.md`` lists them and links the tracking issue.

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
    if reason and strict():
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
        print(
            f"WARNING: could not record the plaintext opt-out: {exc}", file=sys.stderr
        )


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
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise LocalKeyError(
            f"{path} is readable by group or others (mode {oct(mode & 0o777)}); "
            f"run chmod 600 {path}. Nothing was read or written."
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


def default_key() -> Optional[str]:
    """The key to use when none is configured: the local key, or None under
    the opt-out (plaintext, the behaviour before encryption was the default).
    Strict mode ignores the opt-out.
    """
    if opt_out_reason() is not None:
        return None
    return local_key()


def _reset_for_tests() -> None:
    with _lock:
        _warned.clear()

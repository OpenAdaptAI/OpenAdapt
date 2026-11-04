"""Rollback state that outlives the process (docs/ROLLBACK.md, Durable runs).

A rollback compares the store with the read taken before the run. In one
process that read lives in memory. A durable run can pause and be resumed or
rolled back later, and a crash can stop a rollback half way, so the read must
outlive the process. Under ``<run-dir>/rollback/``:

- ``armed.json.enc``: the read from before the run, the MAC key its row
  digests use, the audit log as it was before the run, the run's parameters,
  and what binds them to this run: an arm id, the run id (once the run has
  one), the bundle version and the deployment's rollback settings.
- ``stop.json.enc``: the read the first rollback attempt took after the stop.
  A later attempt judges the run's changes by it, not by a store an earlier
  attempt may have partly undone.
- ``journal.json.enc``: which compensations an attempt started and finished,
  and whether a rollback already ended.

Every file is encrypted (AES-256-GCM, ``openadapt_flow.crypto``) with the
deployment's key and carries the arm id: a file from another run, or one
missing beside the others, is refused. Without a key nothing is written and a
durable run with rollback is refused before it starts.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from openadapt_flow.runtime.effects.db_snapshot import Snapshot

ROLLBACK_DIRNAME = "rollback"
ARMED_FILENAME = "armed.json.enc"
STOP_FILENAME = "stop.json.enc"
JOURNAL_FILENAME = "journal.json.enc"
LOCK_FILENAME = ".lock"
SCHEMA = "openadapt.rollback-armed/v2"
#: Domain separation for the sealed rollback files (``crypto.encrypt_bytes``).
ROLLBACK_AAD = b"openadapt-flow/rollback"


def _lock_exclusive(fd: int) -> None:
    """A non-blocking exclusive lock on ``fd``; OSError when it is held."""
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


class SealedStateError(RuntimeError):
    """The sealed rollback state is missing, unreadable or not this run's."""


class SealedRollback:
    """The sealed rollback state of one run directory."""

    def __init__(self, run_dir: Path | str, key: str) -> None:
        if not key:
            raise SealedStateError("sealed rollback state needs a key")
        self.run_dir = Path(run_dir)
        self.key = key
        self.dir = self.run_dir / ROLLBACK_DIRNAME

    # -- files ------------------------------------------------------------

    def _write(self, name: str, payload: bytes) -> None:
        from openadapt_flow.rollback import _private_dir

        _private_dir(self.dir)
        path = self.dir / name
        staging = path.with_name(f".{name}.{os.getpid()}")
        staging.unlink(missing_ok=True)
        fd = os.open(
            staging,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, path)
        directory = os.open(self.dir, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _seal(self, name: str, data: dict[str, Any]) -> None:
        from openadapt_flow import crypto

        plain = json.dumps(
            {"schema": SCHEMA, **data}, sort_keys=True, default=str
        ).encode("utf-8")
        self._write(name, crypto.encrypt_bytes(plain, self.key, aad=ROLLBACK_AAD))

    def _open(self, name: str) -> Optional[dict[str, Any]]:
        from openadapt_flow import crypto

        path = self.dir / name
        if path.is_symlink():
            raise SealedStateError(f"{path} is a symlink")
        if not path.is_file():
            return None
        try:
            plain = crypto.decrypt_bytes(path.read_bytes(), self.key, aad=ROLLBACK_AAD)
            data = json.loads(plain)
        except Exception as exc:  # noqa: BLE001 - a wrong key or a torn file
            raise SealedStateError(
                f"{name} couldn't be opened ({type(exc).__name__})"
            ) from exc
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            raise SealedStateError(f"{name} isn't sealed rollback state")
        return data

    def _arm_id(self) -> str:
        armed = self._open(ARMED_FILENAME)
        if armed is None or not armed.get("arm_id"):
            raise SealedStateError("the run's rollback isn't armed")
        return str(armed["arm_id"])

    def _own(self, name: str) -> Optional[dict[str, Any]]:
        """``name``, only when it belongs to this arming."""
        data = self._open(name)
        if data is not None and data.get("arm_id") != self._arm_id():
            raise SealedStateError(f"{name} belongs to another run")
        return data

    # -- the read from before the run ---------------------------------------

    def save_armed(
        self,
        *,
        before: Snapshot,
        reader_key: bytes,
        audit_before: Optional[list[dict[str, Any]]],
        params: dict[str, Any],
        settings: dict[str, Any],
        bundle_version: Optional[str] = None,
    ) -> None:
        """Arm afresh: whatever an earlier arming in this directory left goes."""
        arm_id = secrets.token_hex(16)
        for name in (STOP_FILENAME, JOURNAL_FILENAME):
            (self.dir / name).unlink(missing_ok=True)
        self._seal(
            ARMED_FILENAME,
            {
                "arm_id": arm_id,
                "run_id": None,
                "bundle_version": bundle_version,
                "settings": settings,
                "before": before.to_json(),
                "reader_key": base64.b64encode(reader_key).decode("ascii"),
                "audit_before": audit_before,
                "params": params,
            },
        )
        self._seal(JOURNAL_FILENAME, {"arm_id": arm_id, "steps": {}, "done": None})

    def bind_run(self, run_id: str) -> None:
        """Bind the armed state to the run id its durable manifest names."""
        armed = self._open(ARMED_FILENAME)
        if armed is None:
            raise SealedStateError("the run's rollback isn't armed")
        if armed.get("run_id") not in (None, run_id):
            raise SealedStateError("the armed state belongs to another run")
        armed["run_id"] = run_id
        armed.pop("schema", None)
        self._seal(ARMED_FILENAME, armed)

    def load_armed(self) -> Optional[dict[str, Any]]:
        """The armed state: ``before`` (a Snapshot), ``reader_key`` (bytes),
        ``audit_before``, ``params``, ``run_id``, ``bundle_version`` and
        ``settings``; None when the run wasn't armed or its rollback ended."""
        data = self._open(ARMED_FILENAME)
        if data is None:
            return None
        try:
            return {
                "before": Snapshot.from_json(data["before"]),
                "reader_key": base64.b64decode(data["reader_key"]),
                "audit_before": data.get("audit_before"),
                "params": dict(data.get("params") or {}),
                "run_id": data.get("run_id"),
                "bundle_version": data.get("bundle_version"),
                "settings": data["settings"],
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise SealedStateError("armed.json.enc is incomplete") from exc

    # -- the stop, as the first attempt read it ------------------------------

    def stop_read(self, snapshot: Snapshot) -> None:
        """Keep this arming's first read after the stop (never replaced)."""
        if self._own(STOP_FILENAME) is not None:
            return
        self._seal(
            STOP_FILENAME, {"arm_id": self._arm_id(), "after_stop": snapshot.to_json()}
        )

    def load_stop(self) -> Optional[Snapshot]:
        data = self._own(STOP_FILENAME)
        if data is None:
            return None
        try:
            return Snapshot.from_json(data["after_stop"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SealedStateError("stop.json.enc is incomplete") from exc

    # -- which compensations an attempt started ------------------------------

    def _journal(self) -> dict[str, Any]:
        data = self._own(JOURNAL_FILENAME)
        if data is None:
            # Written with the armed state: missing means removed.
            raise SealedStateError("journal.json.enc is missing")
        if not isinstance(data.get("steps"), dict):
            raise SealedStateError("journal.json.enc isn't a rollback journal")
        return data

    def step(self, step_id: str, state: str) -> None:
        """Record that a compensation for ``step_id`` ``started`` or was
        ``compensated``; written before the next action, so a crash after it
        leaves the journal saying so."""
        journal = self._journal()
        journal["steps"][step_id] = state
        journal.pop("schema", None)
        self._seal(JOURNAL_FILENAME, journal)

    def compensated_steps(self) -> frozenset[str]:
        """Steps whose compensation an earlier attempt finished: never run again."""
        steps = self._journal()["steps"]
        return frozenset(k for k, v in steps.items() if v == "compensated")

    def started_steps(self) -> frozenset[str]:
        """Steps whose compensation an earlier attempt started but didn't
        finish: done only when a fresh read shows the change gone."""
        steps = self._journal()["steps"]
        return frozenset(k for k, v in steps.items() if v != "compensated")

    def finished(self) -> Optional[str]:
        """The verdict of a rollback that already ended, if any."""
        path = self.dir / JOURNAL_FILENAME
        if not path.is_file():
            return None
        data = self._open(JOURNAL_FILENAME)
        return None if data is None else data.get("done")

    def mark_finished(self, status: str) -> None:
        """The rollback ended: no attempt runs again, and the reads go."""
        journal = self._journal()
        journal["done"] = status
        journal.pop("schema", None)
        self._seal(JOURNAL_FILENAME, journal)
        for name in (ARMED_FILENAME, STOP_FILENAME):
            (self.dir / name).unlink(missing_ok=True)

    def clear(self) -> None:
        """A run that finished needs none of it."""
        for name in (ARMED_FILENAME, STOP_FILENAME, JOURNAL_FILENAME):
            (self.dir / name).unlink(missing_ok=True)

    @contextmanager
    def lock(self) -> Iterator[None]:
        """One rollback of this run at a time; a second is refused."""
        from openadapt_flow.rollback import _private_dir

        _private_dir(self.dir)
        fd = os.open(self.dir / LOCK_FILENAME, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                _lock_exclusive(fd)
            except OSError as exc:
                raise SealedStateError(
                    "another rollback of this run is running"
                ) from exc
            yield
        finally:
            os.close(fd)


__all__ = ["SealedRollback", "SealedStateError"]

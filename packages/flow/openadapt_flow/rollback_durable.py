"""Rollback state that outlives the process (docs/ROLLBACK.md, Durable runs).

A rollback compares the store with the read taken before the run. In one
process that read lives in memory. A durable run can pause and be resumed or
rolled back later, and a crash can stop a rollback half way, so the read must
outlive the process:

- ``armed.json.enc``: the read from before the run, the MAC key its row
  digests use, the audit log as it was before the run, and the run's
  parameters. Written when the rollback is armed.
- ``stop.json.enc``: the read the first rollback attempt took after the stop.
  A later attempt judges the run's changes by it, not by a store an earlier
  attempt may have partly undone.
- ``journal.json``: which compensations an attempt started and finished (step
  ids only). A later attempt reads the store again before it treats any of
  them as done.

The two reads hold row digests and the values of changed rows, so they are
only ever written encrypted (AES-256-GCM, ``openadapt_flow.crypto``) with the
deployment's key; without a key nothing is written and a durable run with
rollback is refused before it starts.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, Optional

from openadapt_flow.runtime.effects.db_snapshot import Snapshot

ROLLBACK_DIRNAME = "rollback"
ARMED_FILENAME = "armed.json.enc"
STOP_FILENAME = "stop.json.enc"
JOURNAL_FILENAME = "journal.json"
SCHEMA = "openadapt.rollback-armed/v1"
#: Domain separation for the sealed rollback files (``crypto.encrypt_bytes``).
ROLLBACK_AAD = b"openadapt-flow/rollback"


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

    def _seal(self, name: str, data: dict[str, Any]) -> None:
        from openadapt_flow import crypto

        plain = json.dumps(data, sort_keys=True, default=str).encode("utf-8")
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

    # -- the read from before the run ---------------------------------------

    def save_armed(
        self,
        *,
        before: Snapshot,
        reader_key: bytes,
        audit_before: Optional[list[dict[str, Any]]],
        params: dict[str, Any],
    ) -> None:
        self._seal(
            ARMED_FILENAME,
            {
                "schema": SCHEMA,
                "before": before.to_json(),
                "reader_key": base64.b64encode(reader_key).decode("ascii"),
                "audit_before": audit_before,
                "params": params,
            },
        )

    def load_armed(self) -> Optional[dict[str, Any]]:
        """The armed state: ``before`` (a Snapshot), ``reader_key`` (bytes),
        ``audit_before`` and ``params``; None when the run wasn't armed."""
        data = self._open(ARMED_FILENAME)
        if data is None:
            return None
        try:
            return {
                "before": Snapshot.from_json(data["before"]),
                "reader_key": base64.b64decode(data["reader_key"]),
                "audit_before": data.get("audit_before"),
                "params": dict(data.get("params") or {}),
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise SealedStateError("armed.json.enc is incomplete") from exc

    # -- the stop, as the first attempt read it ------------------------------

    def stop_read(self, snapshot: Snapshot) -> None:
        """Keep the first attempt's read after the stop (never replaced)."""
        if (self.dir / STOP_FILENAME).exists():
            return
        self._seal(STOP_FILENAME, {"schema": SCHEMA, "after_stop": snapshot.to_json()})

    def load_stop(self) -> Optional[Snapshot]:
        data = self._open(STOP_FILENAME)
        if data is None:
            return None
        try:
            return Snapshot.from_json(data["after_stop"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SealedStateError("stop.json.enc is incomplete") from exc

    # -- which compensations an attempt started ------------------------------

    def _journal(self) -> dict[str, str]:
        path = self.dir / JOURNAL_FILENAME
        if path.is_symlink():
            raise SealedStateError(f"{path} is a symlink")
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise SealedStateError("journal.json couldn't be read") from exc
        steps = data.get("steps") if isinstance(data, dict) else None
        if not isinstance(steps, dict):
            raise SealedStateError("journal.json isn't a rollback journal")
        return {str(k): str(v) for k, v in steps.items()}

    def step(self, step_id: str, state: str) -> None:
        """Record that a compensation for ``step_id`` ``started`` or was
        ``compensated``; written before the next action, so a crash after it
        leaves the journal saying so."""
        steps = self._journal()
        steps[step_id] = state
        self._write(
            JOURNAL_FILENAME,
            json.dumps({"steps": steps}, sort_keys=True).encode("utf-8"),
        )

    def started_steps(self) -> frozenset[str]:
        """Every step an earlier attempt started compensating (finished or
        not): a later attempt reads the store before it skips or repeats it."""
        return frozenset(self._journal())

    def clear(self) -> None:
        """A run that finished needs none of it."""
        for name in (ARMED_FILENAME, STOP_FILENAME, JOURNAL_FILENAME):
            (self.dir / name).unlink(missing_ok=True)


__all__ = ["SealedRollback", "SealedStateError"]

"""Durable run records and the at-most-once gate behind ``request_id``.

Every ``run_workflow`` call leaves a record under
``<runs-dir>/openadapt-agent/``, refusals included, so ``get_run`` can always
answer. Records hold the PHI-safe contract result only. Inputs are never
stored; a keyed fingerprint of ``(workflow, inputs)`` is, so a reused
``request_id`` with different inputs is refused instead of silently replayed.

The at-most-once authority is openadapt-flow's ``IdempotencyLedger``: each
attempt reserves ``<request>:<attempt>`` before anything runs, and a lost race
reads back the winner instead of acting. A request may start a new attempt
only when its latest attempt proved nothing was written (see
``contract.RETRYABLE_REASONS``). If Flow's ledger can't be opened, a local
exclusive-create claim file gives the same guarantee for this runs directory.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import socket
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from openadapt_agent.contract import RETRYABLE_REASONS, ledger_outcome
from openadapt_agent.runner import is_safe_run_id, new_run_id

__all__ = ["Begin", "MAX_ATTEMPTS", "RunStore", "pid_alive", "read_record"]

_LOG = logging.getLogger(__name__)
_RECORD_SCHEMA = "openadapt-agent.run/v1"
_REQUEST_SCHEMA = "openadapt-agent.request/v1"
_LEDGER_NAMESPACE = "openadapt-agent/run-workflow/v1"
#: Attempts one request_id may make when earlier ones proved nothing was written.
MAX_ATTEMPTS = 5
#: How long a reservation may sit without its run record before it is treated
#: as left behind by a process that died between the two writes.
ORPHAN_GRACE_S = 120.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomic, owner-only write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


class _Duplicate(Exception):
    """The ledger key was already reserved by another attempt."""


class _ClaimFiles:
    """Exclusive-create claim files: the fallback at-most-once gate."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / (hashlib.sha256(key.encode("utf-8")).hexdigest() + ".claim")

    def reserve(self, key: str, *, run_id: str) -> None:
        try:
            fd = os.open(self._path(key), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise _Duplicate(key) from exc
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"run_id": run_id, "reserved_at": _now()}, handle)

    def lookup(self, key: str) -> Optional[dict[str, Any]]:
        return _read_json(self._path(key))

    def record_outcome(self, key: str, outcome: str, *, run_id: str) -> None:
        return None


class _FlowLedger:
    """openadapt-flow's IdempotencyLedger behind the same small interface."""

    def __init__(self, path: Path) -> None:
        from openadapt_flow.transaction import DuplicateActuation, IdempotencyLedger

        self._duplicate = DuplicateActuation
        self._ledger = IdempotencyLedger(path, namespace=_LEDGER_NAMESPACE)

    def reserve(self, key: str, *, run_id: str) -> None:
        try:
            self._ledger.reserve(key, run_id=run_id)
        except self._duplicate as exc:
            raise _Duplicate(key) from exc

    def lookup(self, key: str) -> Optional[dict[str, Any]]:
        return self._ledger.lookup(key)

    def record_outcome(self, key: str, outcome: str, *, run_id: str) -> None:
        self._ledger.record_outcome(key, outcome, run_id=run_id)


@dataclass
class Begin:
    """What ``RunStore.begin`` decided for one request."""

    kind: str  # "new" | "replay" | "conflict" | "retry_limit"
    run_id: str
    record: Optional[dict[str, Any]] = None
    first_run_id: Optional[str] = None


class RunStore:
    """PHI-safe run records plus the request index and idempotency ledger."""

    def __init__(self, runs_dir: Path | str, *, max_attempts: int = MAX_ATTEMPTS) -> None:
        # Canonical path: Flow's ledger refuses symlinked components, and two
        # servers must agree on one ledger for the same directory.
        self.root = Path(runs_dir).expanduser().resolve() / "openadapt-agent"
        self.runs = self.root / "runs"
        self.requests = self.root / "requests"
        for directory in (self.root, self.runs, self.requests):
            directory.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass
        self.max_attempts = max_attempts
        self._lock = threading.RLock()
        self._key = self._fingerprint_key()
        try:
            self._ledger: Any = _FlowLedger(self.root / "ledger.sqlite")
            self.ledger_kind = "openadapt-flow"
        except Exception:  # missing symbol, old Flow, unsupported filesystem
            _LOG.warning("openadapt-flow idempotency ledger unavailable; using claim files")
            self._ledger = _ClaimFiles(self.root / "claims")
            self.ledger_kind = "claim-files"

    # -- records -----------------------------------------------------------

    def _fingerprint_key(self) -> bytes:
        path = self.root / "fingerprint.key"
        existing = path.read_bytes() if path.is_file() and not path.is_symlink() else b""
        if len(existing) >= 32:
            return existing
        key = secrets.token_bytes(32)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return path.read_bytes()
        with os.fdopen(fd, "wb") as handle:
            handle.write(key)
        return key

    def fingerprint(self, workflow: str, inputs: Any) -> str:
        material = json.dumps({"workflow": workflow, "inputs": inputs}, sort_keys=True, default=str)
        return hmac.new(self._key, material.encode("utf-8"), hashlib.sha256).hexdigest()

    def _request_hash(self, request_id: str) -> str:
        return hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:40]

    def read(self, run_id: str) -> Optional[dict[str, Any]]:
        if not is_safe_run_id(run_id):
            return None
        record = _read_json(self.runs / f"{run_id}.json")
        if record is None or record.get("schema") != _RECORD_SCHEMA:
            return None
        return record

    def write(self, record: dict[str, Any]) -> None:
        run_id = record["run_id"]
        if not is_safe_run_id(run_id):
            raise ValueError("unsafe run id")
        _write_json(self.runs / f"{run_id}.json", record)

    def new_record(
        self,
        *,
        request_id: Optional[str],
        workflow: Optional[str],
        mode: str,
        run_id: Optional[str] = None,
    ) -> dict[str, Any]:
        return {
            "schema": _RECORD_SCHEMA,
            "run_id": run_id or new_run_id(),
            "request_id": request_id,
            "workflow": workflow,
            "mode": mode,
            "state": "running",
            "started_at": _now(),
            "pid": os.getpid(),
            "host": socket.gethostname(),
        }

    def _orphan(
        self, run_id: str, request_id: str, workflow: str, mode: str
    ) -> dict[str, Any]:
        """A running record with no owner process: get_run marks it interrupted."""
        record = self.new_record(
            request_id=request_id, workflow=workflow, mode=mode, run_id=run_id
        )
        record["pid"] = None
        self.write(record)
        return record

    def save_refusal(self, record: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        """Persist a result that never reserved the request (bad input, conflict)."""
        record = dict(record, state="finished", finished_at=_now(), result=result)
        self.write(record)
        return record

    def finish(
        self,
        record: dict[str, Any],
        result: dict[str, Any],
        *,
        transaction_outcome: Optional[str] = None,
    ) -> dict[str, Any]:
        """Persist a terminal result and record it in the ledger."""
        record = dict(record, state="finished", finished_at=_now(), result=result)
        self.write(record)
        key = record.get("ledger_key")
        outcome = ledger_outcome(str(result.get("reason")), transaction_outcome)
        if key and outcome:
            try:
                self._ledger.record_outcome(key, outcome, run_id=record["run_id"])
            except Exception:
                _LOG.warning("could not record a ledger outcome for %s", record["run_id"])
        return record

    def update_result(self, record: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        """Replace a finished result after a person resolved its review."""
        record = dict(record, result=result, updated_at=_now())
        self.write(record)
        return record

    # -- requests ----------------------------------------------------------

    def begin(
        self, *, request_id: str, workflow: str, inputs: Any, mode: str
    ) -> Begin:
        """Decide whether this request starts a new attempt or replays one."""
        fingerprint = self.fingerprint(workflow, inputs)
        request_hash = self._request_hash(request_id)
        entry_path = self.requests / f"{request_hash}.json"
        with self._lock:
            entry = _read_json(entry_path) or {
                "schema": _REQUEST_SCHEMA,
                "fingerprint": fingerprint,
                "workflow": workflow,
                "attempts": [],
            }
            attempts = [run for run in entry.get("attempts", []) if isinstance(run, str)]
            if attempts and entry.get("fingerprint") != fingerprint:
                return Begin("conflict", run_id=new_run_id(), first_run_id=attempts[0])
            if attempts:
                latest = self.read(attempts[-1])
                if latest is None:
                    # The entry is written only after the record, so a missing
                    # or unreadable record was lost. Leave an orphan that
                    # get_run reports as interrupted, never as running forever.
                    latest = self._orphan(attempts[-1], request_id, workflow, mode)
                reason = ((latest or {}).get("result") or {}).get("reason")
                retryable = (
                    latest is not None
                    and latest.get("state") == "finished"
                    and reason in RETRYABLE_REASONS
                )
                if not retryable:
                    return Begin("replay", run_id=attempts[-1], record=latest)
                if len(attempts) >= self.max_attempts:
                    return Begin("retry_limit", run_id=new_run_id(), first_run_id=attempts[0])
            attempt = len(attempts) + 1
            key = f"{request_hash}:{attempt}"
            record = self.new_record(request_id=request_id, workflow=workflow, mode=mode)
            record["attempt"] = attempt
            record["ledger_key"] = key
            record["fingerprint"] = fingerprint
            try:
                self._ledger.reserve(key, run_id=record["run_id"])
            except _Duplicate:
                # Another process won this attempt. Read its result instead.
                claim = self._ledger.lookup(key) or {}
                owner = claim.get("run_id")
                if not isinstance(owner, str) or not is_safe_run_id(owner):
                    raise
                record = self.read(owner)
                if record is not None and record.get("fingerprint") not in (None, fingerprint):
                    # Another process took this request_id for different inputs.
                    return Begin("conflict", run_id=new_run_id(), first_run_id=owner)
                if record is None and _older_than(claim.get("reserved_at"), ORPHAN_GRACE_S):
                    # Reserved, then the process died before writing the record.
                    record = self._orphan(owner, request_id, workflow, mode)
                return Begin("replay", run_id=owner, record=record)
            self.write(record)
            entry["attempts"] = [*attempts, record["run_id"]]
            entry["fingerprint"] = fingerprint
            _write_json(entry_path, entry)
            return Begin("new", run_id=record["run_id"], record=record)


def _older_than(timestamp: Any, seconds: float) -> bool:
    """Whether an ISO timestamp is more than ``seconds`` old. Unknown is old."""
    if not isinstance(timestamp, str):
        return True
    try:
        then = datetime.fromisoformat(timestamp)
    except ValueError:
        return True
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() > seconds


def read_record(runs_dir: Path | str, run_id: str) -> Optional[dict[str, Any]]:
    """Read one run record without creating anything on disk."""
    if not is_safe_run_id(run_id):
        return None
    record = _read_json(Path(runs_dir) / "openadapt-agent" / "runs" / f"{run_id}.json")
    if record is None or record.get("schema") != _RECORD_SCHEMA:
        return None
    return record


def pid_alive(pid: Any) -> bool:
    """Whether a process with this id still exists on this computer."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        # os.kill would terminate the process on Windows. Ask instead, and
        # treat any doubt as "gone": a run then reads as interrupted, which
        # asks a person to check the record and never invites a retry.
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            kernel32.CloseHandle(handle)
            return bool(ok) and code.value == 259
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True

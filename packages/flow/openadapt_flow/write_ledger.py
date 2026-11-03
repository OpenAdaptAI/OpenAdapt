"""The uncertain-write ledger: no write is sent twice while its outcome is unknown.

:func:`openadapt_flow.verifier.begin_write` consults it before every write
and :func:`~openadapt_flow.verifier.verify_write` records each outcome, so a
restart, a retry or a second worker that shares the ledger can't resend a
write that may already have landed. See ``docs/VERIFIER_API.md``.

Each write identity (a key, see :meth:`Ledger.key_for`) has an append-only
log of events:

``begun``
    An attempt: its baseline was read and the write may now be sent.
``resolved``
    The attempt's outcome (``CONFIRMED``, ``REFUTED`` or ``UNCERTAIN``),
    from a read-back. ``delivered`` says the caller received it.
``delivered``
    A stored outcome was handed to a later caller instead of a resend.
``released``
    A person checked the record and released a held attempt.

Nothing is rewritten or deleted. Events hold closed fields only: no record
values, and keys are HMACs under a secret kept with the ledger.

A lease (:meth:`LedgerBackend.lease`) gives one caller a key from
``begin_write`` until ``verify_write`` returns. :class:`FileLedger` leases
are ``flock`` locks, which the operating system drops when the process dies:
a free lease on an open attempt means its owner died between the send and
the read-back.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence

__all__ = [
    "Attempt",
    "FileLedger",
    "Lease",
    "Ledger",
    "LedgerBackend",
    "LedgerError",
    "MemoryLedger",
    "default_ledger",
]

#: ``OPENADAPT_LEDGER_DIR`` overrides it.
DEFAULT_DIR = "~/.openadapt/ledger"
_KEY = 64  # hex chars of an HMAC-SHA256


class LedgerError(RuntimeError):
    """The ledger can't be read or written. Nothing may be sent."""


class Lease(Protocol):
    def release(self) -> None: ...


class LedgerBackend(Protocol):
    """Where the ledger lives. Implement it for a shared store (a database)
    when workers run on several hosts; :class:`FileLedger` is one machine."""

    def secret(self) -> bytes:
        """The HMAC key for write identities, the same for every worker."""

    def events(self, key: str) -> list[dict[str, Any]]:
        """Every event appended for ``key``, oldest first."""

    def append(self, key: str, event: Mapping[str, Any]) -> None:
        """Append one event durably before returning."""

    def lease(self, key: str) -> Optional[Lease]:
        """An exclusive lease on ``key`` without waiting, or None when
        another live caller holds it. It must end when its holder dies."""

    def keys(self) -> list[str]:
        """Every key with at least one event."""

    def attempt_dir(self, attempt_id: str) -> Path:
        """A new private folder for one attempt's baseline and values."""


def _private_dir(path: Path) -> Path:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
    except OSError as exc:
        raise LedgerError(f"can't keep the write ledger: {exc}") from exc
    info = os.lstat(path)
    if (
        not os.path.isdir(path)
        or os.path.islink(path)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        # Another user could remove entries (a resend) or plant them.
        raise LedgerError(f"{path} must be a private directory owned by this user")
    return path


def _check_key(key: str) -> str:
    if len(key) != _KEY or any(c not in "0123456789abcdef" for c in key):
        raise LedgerError("a ledger key is 64 lowercase hex characters")
    return key


class _FileLease:
    def __init__(self, handle: Any) -> None:
        self._handle = handle

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


class FileLedger:
    """The ledger on this machine's local disk (the default).

    ``root``/``entries/<key>.jsonl`` holds each key's events, one JSON object
    per line, appended and fsynced. ``locks/<key>.lock`` carries the lease,
    ``attempts/<id>/`` each attempt's baseline and values, and
    ``ledger.key`` the HMAC key (all private to this user). ``flock`` locks
    don't hold across hosts or on network file systems.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = _private_dir(Path(root).expanduser())
        self._entries = _private_dir(self.root / "entries")
        self._locks = _private_dir(self.root / "locks")
        self._attempts = _private_dir(self.root / "attempts")

    def secret(self) -> bytes:
        path = self.root / "ledger.key"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w") as fh:
                fh.write(secrets.token_hex(32))
                fh.flush()
                os.fsync(fh.fileno())
        # Another worker may be writing it: wait for the whole key.
        for _ in range(50):
            try:
                text = path.read_text().strip()
            except OSError as exc:
                raise LedgerError(f"can't read the ledger key: {exc}") from exc
            if len(text) == 64:
                try:
                    return bytes.fromhex(text)
                except ValueError:
                    break
            time.sleep(0.01)
        raise LedgerError(
            f"{path} is not a ledger key (a crash while it was written?). "
            "A person checks that no write is open, then removes it"
        )

    def events(self, key: str) -> list[dict[str, Any]]:
        path = self._entries / f"{_check_key(key)}.jsonl"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise LedgerError(f"can't read the write ledger: {exc}") from exc
        out: list[dict[str, Any]] = []
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                # A line torn by a crash mid-append: its call never returned,
                # so nothing it would have allowed happened.
                continue
            if isinstance(event, dict):
                out.append(event)
        return out

    def append(self, key: str, event: Mapping[str, Any]) -> None:
        path = self._entries / f"{_check_key(key)}.jsonl"
        line = json.dumps(dict(event), sort_keys=True, separators=(",", ":"))
        try:
            fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                size = os.fstat(fd).st_size
                torn = size > 0 and os.pread(fd, 1, size - 1) != b"\n"
                os.write(fd, (("\n" if torn else "") + line + "\n").encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            raise LedgerError(f"can't write the write ledger: {exc}") from exc

    def lease(self, key: str) -> Optional[Lease]:
        path = self._locks / f"{_check_key(key)}.lock"
        try:
            handle = open(path, "a")  # noqa: SIM115 - the lease owns it
        except OSError as exc:
            raise LedgerError(f"can't lease a write ledger key: {exc}") from exc
        try:
            os.chmod(path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return None
        except OSError as exc:
            handle.close()
            raise LedgerError(f"can't lease a write ledger key: {exc}") from exc
        return _FileLease(handle)

    def keys(self) -> list[str]:
        return sorted(p.stem for p in self._entries.glob("*.jsonl"))

    def attempt_dir(self, attempt_id: str) -> Path:
        path = self._attempts / attempt_id
        try:
            path.mkdir(mode=0o700)
        except OSError as exc:
            raise LedgerError(f"can't keep the attempt: {exc}") from exc
        return path


class _MemoryLease:
    def __init__(self, lock: threading.Lock) -> None:
        self._lock: Optional[threading.Lock] = lock

    def release(self) -> None:
        lock, self._lock = self._lock, None
        if lock is not None:
            lock.release()


class MemoryLedger:
    """The ledger in this process only: for tests. It survives no restart."""

    def __init__(self, root: Optional[str | Path] = None) -> None:
        import tempfile

        self._root = Path(root or tempfile.mkdtemp(prefix="openadapt-ledger-"))
        self._secret = secrets.token_bytes(32)
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def secret(self) -> bytes:
        return self._secret

    def events(self, key: str) -> list[dict[str, Any]]:
        with self._guard:
            return [dict(e) for e in self._events.get(key, [])]

    def append(self, key: str, event: Mapping[str, Any]) -> None:
        with self._guard:
            self._events.setdefault(_check_key(key), []).append(dict(event))

    def lease(self, key: str) -> Optional[Lease]:
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        return _MemoryLease(lock) if lock.acquire(blocking=False) else None

    def keys(self) -> list[str]:
        with self._guard:
            return sorted(self._events)

    def attempt_dir(self, attempt_id: str) -> Path:
        path = self._root / attempt_id
        path.mkdir(mode=0o700, parents=True)
        return path


@dataclass
class Attempt:
    """The latest attempt of a key, folded from its events."""

    attempt_id: str
    begun: dict[str, Any]
    resolved: Optional[dict[str, Any]] = None
    delivered: bool = False
    released: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def state(self) -> str:
        """The state with no repeat window (see :meth:`state_with`)."""

        return self.state_with(0.0)

    def state_with(self, repeat_s: float) -> str:
        """``open`` (no outcome yet), ``free`` (a new attempt may start),
        ``undelivered`` (an outcome to hand back instead of a resend),
        ``uncertain`` (reconcile first) or ``held`` (a person checks).

        A write that landed (``CONFIRMED``, or found in the store on a
        reconcile) is handed back instead of sent again while its reply may
        not have reached the caller, and for ``repeat_s`` after it resolved:
        a retry inside that window gets the result. A caller-given write
        key names one write, made once: it is handed back for good.
        """

        if self.released:
            return "free"  # a person checked the record
        r = self.resolved
        if r is None:
            return "open"
        if r.get("retry_safe"):
            return "free"
        if r.get("outcome") == "CONFIRMED" or r.get("landed"):
            if r.get("write_key_bound") or r.get("review") or not self.delivered:
                # Under review: handed back until a person releases it, so
                # no later attempt hides it from the list.
                return "undelivered"
            return "free" if _age(r.get("at")) >= repeat_s else "undelivered"
        if r.get("outcome") == "REFUTED":
            return "held"
        return "uncertain"

    @property
    def review(self) -> bool:
        """Landed, but a person should look at the store; until released."""

        return bool(self.resolved and self.resolved.get("review")) and not self.released

    @property
    def folder(self) -> Path:
        return Path(str(self.begun.get("folder", "")))


def _age(at: Any) -> float:
    import calendar

    try:
        then = calendar.timegm(time.strptime(str(at), "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return 0.0
    return time.time() - then


def prune(folder: Path) -> None:
    """Remove an attempt's raw values and baseline once nothing needs them
    (it resolved for good). Receipts stay; they hold no record values."""

    if not str(folder) or not folder.is_absolute():
        return  # an attempt with no folder of its own
    for name in ("attempt.json", "baseline.json"):
        (folder / name).unlink(missing_ok=True)
    for copy in folder.glob("reconcile-*/baseline.json"):
        copy.unlink(missing_ok=True)


def fold(events: Sequence[Mapping[str, Any]]) -> Optional[Attempt]:
    """The latest attempt in ``events``, or None when there is none."""

    latest: Optional[Attempt] = None
    for event in events:
        kind = event.get("event")
        if kind == "begun":
            latest = Attempt(str(event.get("attempt")), dict(event))
            continue
        if latest is None or event.get("attempt") != latest.attempt_id:
            continue
        if kind == "resolved":
            latest.resolved = dict(event)
            latest.delivered = bool(event.get("delivered"))
        elif kind == "delivered":
            latest.delivered = True
        elif kind == "released":
            latest.released = True
    if latest is not None:
        latest.events = [
            dict(e) for e in events if e.get("attempt") == latest.attempt_id
        ]
    return latest


#: A null's form in a write's identity. Every other leaf becomes text, so no
#: value can take this form: ``None`` and ``""`` are different writes, as the
#: record check reads them.
_NULL = {"__null__": True}


def _scalar(value: Any, *, legacy_null: bool = False) -> Any:
    """One form per stored value: ``7`` and ``"7"`` are the same write.

    ``legacy_null`` gives the form ledgers wrote before nulls were told
    apart (``None`` as ``""``), so an attempt recorded then is still found.
    """

    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "" if legacy_null else dict(_NULL)
    if isinstance(value, (int, float, str)):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _scalar(v, legacy_null=legacy_null) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scalar(v, legacy_null=legacy_null) for v in value]
    return str(value)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )


class Ledger:
    """The ledger's logic over a :class:`LedgerBackend`.

    ``settle_s``: how long after an attempt began a read-back that shows
    nothing proves the write never landed. Default None: such an attempt
    stays held until a person releases it, because a queued write can land
    late.

    ``repeat_s``: how long a landed write without a write key is handed
    back to an identical call instead of being sent again (an agent's retry
    after a lost reply). Default 10 minutes. Two intended writes with the
    same values inside it need distinct write keys.
    """

    def __init__(
        self,
        backend: LedgerBackend,
        *,
        settle_s: Optional[float] = None,
        repeat_s: float = 600.0,
    ):
        self.backend = backend
        self.settle_s = settle_s
        self.repeat_s = repeat_s

    def key_for(
        self,
        contract_digest: str,
        values: Mapping[str, Any],
        write_key: Optional[str] = None,
        *,
        legacy_null: bool = False,
    ) -> str:
        """A write's identity: its contract, its bound values, and the
        caller's ``write_key`` (a task id) when given. ``legacy_null`` gives
        the key a ledger wrote before ``None`` and ``""`` were told apart."""

        body = _canonical(
            {
                "contract": contract_digest,
                "values": _scalar(dict(values), legacy_null=legacy_null),
                "key": write_key,
            }
        )
        return hmac.new(self.backend.secret(), body, hashlib.sha256).hexdigest()

    def latest(self, key: str) -> Optional[Attempt]:
        return fold(self.backend.events(key))

    def state(self, attempt: Optional[Attempt]) -> str:
        return "free" if attempt is None else attempt.state_with(self.repeat_s)

    def attempts(self) -> list[tuple[str, dict[str, Any], Optional[dict[str, Any]]]]:
        """Every attempt in the ledger: (key, begun event, resolved event).

        A released attempt counts as resolved when a person released it.
        """

        out = []
        for key in self.backend.keys():
            begun: dict[str, dict[str, Any]] = {}
            resolved: dict[str, dict[str, Any]] = {}
            for event in self.backend.events(key):
                attempt = str(event.get("attempt"))
                if event.get("event") == "begun":
                    begun[attempt] = dict(event)
                elif event.get("event") in ("resolved", "released"):
                    resolved.setdefault(attempt, dict(event))
            out += [(key, b, resolved.get(a)) for a, b in begun.items()]
        return out

    def begun(self, key: str, attempt_id: str, **fields: Any) -> None:
        self.backend.append(
            key,
            {
                "event": "begun",
                "attempt": attempt_id,
                "at": _now(),
                "t": time.time(),
                **fields,
            },
        )

    def resolved(self, key: str, attempt_id: str, **fields: Any) -> None:
        self.backend.append(
            key,
            {
                "event": "resolved",
                "attempt": attempt_id,
                "at": _now(),
                "t": time.time(),
                **fields,
            },
        )

    def delivered(self, key: str, attempt_id: str) -> None:
        self.backend.append(
            key, {"event": "delivered", "attempt": attempt_id, "at": _now()}
        )

    def release(self, key: str, *, by: str, reason: str, found: str) -> Attempt:
        """A person checked the record: release the key's held attempt.

        ``found`` is what they saw in the store. ``absent``: the write isn't
        there, so the key is free and it may be sent again. ``landed``: it
        is there, so the attempt resolves as landed and an identical call is
        handed back, never resent.

        Raises :class:`LedgerError` when another caller holds the key, or
        its latest attempt isn't held, uncertain, open or under review.
        """

        if not by.strip() or not reason.strip():
            raise LedgerError("a release names who checked the record, and why")
        if found not in ("absent", "landed"):
            raise LedgerError("found is absent or landed")
        lease = self.backend.lease(_check_key(key))
        if lease is None:
            raise LedgerError("the write is in flight in another worker")
        try:
            attempt = self.latest(key)
            if attempt is None or (
                attempt.state not in ("held", "uncertain", "open")
                and not attempt.review
            ):
                raise LedgerError("nothing to release for that key")
            if found == "landed":
                self.backend.append(
                    key,
                    {
                        "event": "resolved",
                        "attempt": attempt.attempt_id,
                        "at": _now(),
                        "t": time.time(),
                        "outcome": "UNCERTAIN",
                        "landed": True,
                        "delivered": False,
                        "via": "person",
                        "by": by.strip()[:128],
                        "reason": reason.strip()[:500],
                        "write_key_bound": bool(attempt.begun.get("write_key_bound")),
                        "result": {
                            "verified": False,
                            "outcome": "UNCERTAIN",
                            "landed": True,
                            "reason": "a person found the write in the store",
                        },
                    },
                )
                prune(attempt.folder)
                return attempt
            self.backend.append(
                key,
                {
                    "event": "released",
                    "attempt": attempt.attempt_id,
                    "at": _now(),
                    "t": time.time(),
                    "by": by.strip()[:128],
                    "reason": reason.strip()[:500],
                },
            )
            attempt.released = True
            prune(attempt.folder)
            return attempt
        finally:
            lease.release()

    def unresolved(self) -> list[tuple[str, Attempt]]:
        """Every key whose latest attempt is open, uncertain or held, or
        landed with something a person should look at (``review``)."""

        out = []
        for key in self.backend.keys():
            attempt = self.latest(key)
            if attempt is None:
                continue
            if attempt.state in ("open", "uncertain", "held") or attempt.review:
                out.append((key, attempt))
        return out


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def default_ledger() -> Ledger:
    """The :class:`FileLedger` in ``OPENADAPT_LEDGER_DIR`` or
    ``~/.openadapt/ledger``."""

    return Ledger(FileLedger(os.environ.get("OPENADAPT_LEDGER_DIR") or DEFAULT_DIR))

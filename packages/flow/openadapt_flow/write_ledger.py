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
import stat
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Callable,
    ContextManager,
    Iterator,
    Literal,
    Mapping,
    Optional,
    Protocol,
    Sequence,
)

from openadapt_flow.write_journal_authority import (
    JournalCommit,
    JournalHead,
    LedgerError,
    LedgerIntegrityHeld,
    SQLiteJournalAuthority,
)

__all__ = [
    "Attempt",
    "FileLedger",
    "Lease",
    "Ledger",
    "LedgerBackend",
    "LedgerError",
    "LedgerIntegrityHeld",
    "Journal",
    "MemoryLedger",
    "SQLiteJournalAuthority",
    "default_ledger",
]

#: ``OPENADAPT_LEDGER_DIR`` overrides it.
DEFAULT_DIR = "~/.openadapt/ledger"
_KEY = 64  # hex chars of an HMAC-SHA256


class Lease(Protocol):
    def release(self) -> None: ...


class Journal(Protocol):
    """A key's journal while its short event lock is held."""

    def events(self) -> list[dict[str, Any]]: ...

    def append(self, event: Mapping[str, Any]) -> None: ...


@dataclass
class _Journal:
    read: Callable[[], list[dict[str, Any]]]
    write: Callable[[Mapping[str, Any]], None]

    def events(self) -> list[dict[str, Any]]:
        return self.read()

    def append(self, event: Mapping[str, Any]) -> None:
        self.write(event)


class LedgerBackend(Protocol):
    """Where the ledger lives. Implement it for a shared store (a database)
    when workers run on several hosts; :class:`FileLedger` is one machine.

    Mutations require transaction(). A backend without this primitive fails
    closed; an events()/append() pair is not an atomic substitute. Low-level
    append observations carry their observed ``based_on`` journal revision.
    Without one, a weak observation is retained but cannot grant permission.
    """

    def secret(self) -> bytes:
        """The HMAC key for write identities, the same for every worker."""

    def events(self, key: str) -> list[dict[str, Any]]:
        """Every event appended for ``key``, oldest first."""

    def append(self, key: str, event: Mapping[str, Any]) -> None:
        """Append one event durably before returning."""

    def transaction(self, key: str) -> ContextManager[Journal]:
        """Serialize a read, decision and durable append for one key.

        This short lock must be independent of the lifecycle lease. Every
        append, including low-level append(), must participate in it.
        """

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
    def __init__(self, handle: Any, gate: Any = None) -> None:
        self._handle = handle
        self._gate = gate

    def release(self) -> None:
        handle, self._handle = self._handle, None
        gate, self._gate = self._gate, None
        try:
            if handle is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                finally:
                    handle.close()
        finally:
            if gate is not None:
                gate.close()


@dataclass(frozen=True)
class LegacyJournalInspection:
    """Historical observations only; they cannot authorize a write."""

    events: tuple[dict[str, Any], ...]
    authenticated: bool = field(default=False, init=False)


class FileLedger:
    """The ledger on this machine's local disk (the default).

    ``root``/``entries/<key>.jsonl`` holds each key's events, one JSON object
    per line, appended and fsynced. ``locks/<key>.lock`` carries the lease,
    and ``locks/<key>.events.lock`` serializes short journal decisions,
    ``attempts/<id>/`` each attempt's baseline and values, and
    ``ledger.key`` the HMAC key (all private to this user). ``flock`` locks
    don't hold across hosts or on network file systems.
    """

    def __init__(
        self, root: str | Path, *, authority: SQLiteJournalAuthority | None = None
    ) -> None:
        self.root = _private_dir(Path(root).expanduser())
        if authority is not None and self.root.resolve() != authority.root:
            raise LedgerIntegrityHeld("ledger_authority_wrong_scope")
        self._authority = authority
        self._entries = _private_dir(self.root / "entries")
        self._locks = _private_dir(self.root / "locks")
        self._attempts = _private_dir(self.root / "attempts")

    @property
    def integrity_protected(self) -> bool:
        return self._authority is not None

    def secret(self) -> bytes:
        if self._authority is not None:
            return self._authority.lookup_key()
        return self._legacy_secret()

    def _legacy_secret(self) -> bytes:
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
                text = path.read_text(encoding="utf-8").strip()
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
        with self.transaction(key) as journal:
            return journal.events()

    def _events(self, key: str) -> list[dict[str, Any]]:
        path = self._entries / f"{_check_key(key)}.jsonl"
        try:
            data = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise LedgerError(f"can't read the write ledger: {exc}") from exc
        lines = data.splitlines()
        if data and not data.endswith("\n"):
            # A complete JSON object without its terminating newline may be
            # a short write whose append never completed.
            lines = lines[:-1]
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
        basis = event.get("based_on", None if event.get("event") == "begun" else -1)
        Ledger(self)._update(key, event, expected_revision=basis)

    @contextmanager
    def transaction(self, key: str) -> Iterator[Journal]:
        if self._authority is not None:
            with self._protected_transaction(_check_key(key)) as journal:
                yield journal
            return
        path = self._locks / f"{_check_key(key)}.events.lock"
        try:
            with self._legacy_gate(), open(path, "a", encoding="utf-8") as handle:
                os.chmod(path, 0o600)
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield _Journal(
                        lambda: self._events(key), lambda e: self._append(key, e)
                    )
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise LedgerError(f"can't update the write ledger: {exc}") from exc

    @staticmethod
    def _event_wire(event: Mapping[str, Any]) -> bytes:
        line = json.dumps(dict(event), sort_keys=True, separators=(",", ":"))
        return (line + "\n").encode("utf-8")

    def _append(self, key: str, event: Mapping[str, Any]) -> None:
        path = self._entries / f"{_check_key(key)}.jsonl"
        try:
            flags = os.O_RDWR | os.O_APPEND | os.O_CREAT
            if self._authority is not None:
                flags |= os.O_NOFOLLOW | os.O_NONBLOCK
            fd = os.open(path, flags, 0o600)
            try:
                info = os.fstat(fd)
                if self._authority is not None and not stat.S_ISREG(info.st_mode):
                    raise LedgerIntegrityHeld("ledger_journal_mismatch")
                size = info.st_size
                torn = size > 0 and os.pread(fd, 1, size - 1) != b"\n"
                # Preserve a torn tail without making a previously complete
                # but unterminated JSON object effective on the next append.
                data = (b" [incomplete]\n" if torn else b"") + self._event_wire(event)
                while data:
                    written = os.write(fd, data)
                    if written <= 0:
                        raise OSError("the journal append made no progress")
                    data = data[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            raise LedgerError(f"can't write the write ledger: {exc}") from exc

    def _legacy_gate(self, *, exclusive: bool = False) -> Any:
        fd = os.open(
            self._locks / ".enrollment.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
        handle = os.fdopen(fd, "rb+")
        try:
            mode = fcntl.LOCK_EX | fcntl.LOCK_NB if exclusive else fcntl.LOCK_SH
            fcntl.flock(handle.fileno(), mode)
            return handle
        except BaseException:
            handle.close()
            raise

    def lease(self, key: str) -> Optional[Lease]:
        key = _check_key(key)
        authority = self._authority
        if authority is None:
            path = self._locks / f"{key}.lock"
        else:
            current = authority.snapshot()
            if key in current.legacy_keys:
                raise LedgerIntegrityHeld("ledger_legacy_unauthenticated")
            path = authority.path.with_name(f"{authority.path.name}.{key}.lease")
        gate = None
        handle = None
        try:
            if authority is None:
                gate = self._legacy_gate()
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            handle = os.fdopen(fd, "rb+")
            os.chmod(path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            lease = _FileLease(handle, gate)
            handle = gate = None
            return lease
        except BlockingIOError:
            return None
        except OSError as exc:
            if authority is not None:
                raise LedgerIntegrityHeld("ledger_authority_unavailable") from exc
            raise LedgerError(f"can't lease a write ledger key: {exc}") from exc
        finally:
            if handle is not None:
                handle.close()
            if gate is not None:
                gate.close()

    def keys(self) -> list[str]:
        authority = self._authority
        if authority is not None:
            with authority.serialized():
                current = authority.snapshot()
                self._verify_inventory(current)
                return sorted(current.heads)
        return sorted(p.stem for p in self._entries.glob("*.jsonl"))

    def inspect_legacy(self, key: str) -> LegacyJournalInspection:
        """Read the original file format without making a trust claim."""
        return LegacyJournalInspection(tuple(self._events(key)))

    def enroll(self, authority: SQLiteJournalAuthority, *, authorized: bool) -> None:
        """Anchor an offline journal; existing histories remain inspection only.

        The trusted operator must stop old processes that do not honor the
        enrollment gate. The gate refuses enrollment while a current worker
        has a lease or an open journal transaction. This does not clear an
        earlier unresolved attempt or attest to any old observation.
        """
        if authorized is not True or self._authority is not None:
            raise LedgerIntegrityHeld("ledger_legacy_unauthenticated")
        if authority.root != self.root.resolve():
            raise LedgerIntegrityHeld("ledger_authority_wrong_scope")
        try:
            with self._legacy_gate(exclusive=True):
                heads, _, _ = self._file_inventory()
                if heads and not (self.root / "ledger.key").is_file():
                    raise LedgerIntegrityHeld("ledger_legacy_unauthenticated")
                try:
                    lookup_key = (
                        self._legacy_secret() if heads else secrets.token_bytes(32)
                    )
                except LedgerError as exc:
                    raise LedgerIntegrityHeld("ledger_legacy_unauthenticated") from exc
                authority.initialize(heads, lookup_key)
        except BlockingIOError as exc:
            raise LedgerIntegrityHeld("ledger_legacy_unauthenticated") from exc
        except OSError as exc:
            raise LedgerIntegrityHeld("ledger_authority_unavailable") from exc

    def _file_inventory(
        self, selected: str | None = None
    ) -> tuple[dict[str, JournalHead], list[dict[str, Any]], bytes]:
        heads: dict[str, JournalHead] = {}
        selected_events: list[dict[str, Any]] = []
        selected_wire = b""
        try:
            for path in self._entries.iterdir():
                if path.suffix != ".jsonl":
                    continue
                key = _check_key(path.stem)
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as source:
                    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                        raise ValueError
                    wire = source.read()
                if not wire or not wire.endswith(b"\n"):
                    raise ValueError
                events = [
                    json.loads(line.decode("utf-8")) for line in wire[:-1].split(b"\n")
                ]
                if not all(isinstance(event, dict) for event in events):
                    raise ValueError
                heads[key] = JournalHead(len(events), hashlib.sha256(wire).hexdigest())
                if selected == key:
                    selected_events, selected_wire = events, wire
        except (LedgerError, OSError, ValueError) as exc:
            raise LedgerIntegrityHeld("ledger_journal_mismatch") from exc
        return heads, selected_events, selected_wire

    def _verify_inventory(
        self, current: JournalCommit, key: str | None = None
    ) -> tuple[list[dict[str, Any]], bytes]:
        heads, events, wire = self._file_inventory(key)
        if heads != current.heads:
            raise LedgerIntegrityHeld("ledger_journal_mismatch")
        return events, wire

    @contextmanager
    def _protected_transaction(self, key: str) -> Iterator[Journal]:
        authority = self._authority
        assert authority is not None
        with authority.serialized():
            current = authority.snapshot()
            events, wire = self._verify_inventory(current, key)
            if key in current.legacy_keys:
                raise LedgerIntegrityHeld("ledger_legacy_unauthenticated")

            def append(event: Mapping[str, Any]) -> None:
                nonlocal current, events, wire
                next_wire = wire + self._event_wire(event)
                head = JournalHead(
                    len(events) + 1, hashlib.sha256(next_wire).hexdigest()
                )
                next_commit = JournalCommit(
                    current.revision + 1,
                    {**current.heads, key: head},
                    current.legacy_keys,
                )
                token = authority.prepare(current)
                try:
                    self._append(key, event)
                    fd = os.open(self._entries, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                    next_events, _ = self._verify_inventory(next_commit, key)
                except (LedgerError, OSError) as exc:
                    # Preserve the pending marker after an indeterminate append.
                    raise LedgerIntegrityHeld("ledger_authority_interrupted") from exc
                authority.commit(current, token, key, head)
                current, events, wire = next_commit, next_events, next_wire

            yield _Journal(lambda: deepcopy(events), append)

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
            return deepcopy(self._events.get(key, []))

    def append(self, key: str, event: Mapping[str, Any]) -> None:
        basis = event.get("based_on", None if event.get("event") == "begun" else -1)
        Ledger(self)._update(key, event, expected_revision=basis)

    @contextmanager
    def transaction(self, key: str) -> Iterator[Journal]:
        with self._guard:
            events = self._events.setdefault(_check_key(key), [])
            yield _Journal(
                lambda: deepcopy(events), lambda e: events.append(deepcopy(dict(e)))
            )

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
    #: Effective settlement. Rejected observations remain in events.
    resolved: Optional[dict[str, Any]] = None
    delivered: bool = False
    released: bool = False
    #: The ``rechecked`` event: a recheck confirmed a provisional result.
    rechecked: Optional[dict[str, Any]] = None
    events: list[dict[str, Any]] = field(default_factory=list)
    #: Journal length at this observation, not a native-write proof.
    revision: int = 0

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
        if r.get("outcome") == "REFUTED":
            return "held"
        # Legacy retry_safe flags are observations, not a complete
        # dispatch/channel proof. They cannot release an unresolved attempt.
        if r.get("outcome") == "CONFIRMED" or r.get("landed"):
            if (
                r.get("write_key_bound")
                or r.get("review")
                or not self.delivered
                or self.recheck_owed
            ):
                # Under review, or a recheck still owed: handed back until a
                # person releases it (or the recheck settles it), so no later
                # attempt hides it from the list.
                return "undelivered"
            return "free" if _age(r.get("at")) >= repeat_s else "undelivered"
        return "uncertain"

    def recheck_hold(
        self,
    ) -> Optional[Literal["prior_refutation", "person_decision", "released_attempt"]]:
        """The terminal history that a native reread cannot supersede."""

        for event in reversed(self.events):
            if event.get("applied") is False:
                continue
            if event.get("event") == "released":
                return "released_attempt"
            if event.get("event") == "resolved" and event.get("via") == "person":
                return "person_decision"
            if event.get("event") == "resolved" and event.get("outcome") == "REFUTED":
                return "prior_refutation"
        return None

    @property
    def review(self) -> bool:
        """Landed, but a person should look at the store; until released."""

        return bool(self.resolved and self.resolved.get("review")) and not self.released

    @property
    def recheck_owed(self) -> Optional[str]:
        """The ``recheck_due_at`` of a provisional ``CONFIRMED`` that no
        recheck has settled yet, else None.

        A recheck that confirms the write records ``rechecked``; one that
        doesn't resolves the attempt again (no longer provisional). A
        person's release settles it too.
        """

        if self.released or self.rechecked is not None or self.resolved is None:
            return None
        result = self.resolved.get("result")
        if not isinstance(result, Mapping) or not result.get("provisional"):
            return None
        return str(result.get("recheck_due_at") or "") or None

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

    attempts = _fold_attempts(events)
    return attempts[-1] if attempts else None


def _fold_attempts(events: Sequence[Mapping[str, Any]]) -> list[Attempt]:
    """One projection for latest, history, and recovery.

    A recorded refutation is a workflow hold, not proof of native absence.
    A weaker observation cannot clear that hold. Only an explicit person
    decision can do so. Rejected concurrent observations remain in events.
    """

    attempts: dict[str, Attempt] = {}
    for event in events:
        kind = event.get("event")
        identity = str(event.get("attempt"))
        if kind == "begun":
            if event.get("applied") is not False and identity not in attempts:
                attempts[identity] = Attempt(identity, dict(event))
            continue
        attempt = attempts.get(identity)
        if attempt is None:
            continue
        if event.get("applied") is False:
            continue
        prior = attempt.resolved or {}
        if kind == "resolved":
            if (
                prior.get("outcome") == "REFUTED"
                and event.get("outcome") != "REFUTED"
                and not (event.get("via") == "person" and event.get("applied") is True)
                and not attempt.released
            ):
                continue
            attempt.resolved = dict(event)
            attempt.delivered = bool(event.get("delivered"))
            attempt.rechecked = None
            attempt.released = False
        elif kind == "delivered":
            attempt.delivered = True
        elif kind == "released":
            attempt.released = True
        elif kind == "rechecked" and prior.get("outcome") == "CONFIRMED":
            attempt.rechecked = dict(event)
    for attempt in attempts.values():
        attempt.revision = len(events)
        attempt.events = [
            dict(e) for e in events if e.get("attempt") == attempt.attempt_id
        ]
    return list(attempts.values())


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

    ``settle_s`` is retained for constructor compatibility but grants no
    retry permission. Elapsed time and an absent read cannot exclude a
    queued write or another delivery channel. Unresolved attempts stay held.

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

    def _transaction(self, key: str) -> ContextManager[Journal]:
        transaction: Optional[Callable[[str], ContextManager[Journal]]] = getattr(
            self.backend, "transaction", None
        )
        if not callable(transaction):
            raise LedgerError(
                "the ledger backend does not support atomic journal updates"
            )
        return transaction(_check_key(key))

    def _update(
        self,
        key: str,
        event: Mapping[str, Any],
        *,
        expected_revision: Optional[int] = None,
        cleanup: Optional[Path] = None,
        require_applied: bool = False,
        person_decision: bool = False,
        _after_commit: Optional[Callable[[Attempt], None]] = None,
    ) -> Attempt:
        """Append an observation and project it under one journal lock.

        Revision rejection never discards the observation, and never grants
        retry, delivery, or cleanup permission. A same-attempt refutation
        may tighten a concurrent hold; it cannot affect a newer attempt.
        """

        with self._transaction(key) as journal:
            events = journal.events()
            prior = fold(events)
            identity = str(event.get("attempt"))
            kind = event.get("event")
            current = prior is not None and prior.attempt_id == identity
            fresh = expected_revision is None or (
                type(expected_revision) is int and expected_revision == len(events)
            )
            refuted = (
                current
                and prior is not None
                and (prior.resolved or {}).get("outcome") == "REFUTED"
            )
            tightening = kind == "resolved" and event.get("outcome") == "REFUTED"
            person = kind == "released" or event.get("via") == "person"
            # A current positive recheck is an observation, not a release.
            # The fold leaves earlier non-confirmed settlements unchanged.
            positive_recheck = (
                kind == "rechecked" and event.get("outcome") == "CONFIRMED"
            )
            if kind == "begun":
                applied = fresh and (prior is None or self.state(prior) == "free")
                applied = applied and not any(
                    e.get("event") == "begun" and str(e.get("attempt")) == identity
                    for e in events
                )
            else:
                applied = bool(current and (fresh or (tightening and not person)))
                if person and not person_decision:
                    applied = False
                if refuted and not tightening and not person and not positive_recheck:
                    applied = False
                # Protect the settlement and any projected recheck. A fresh
                # positive audit row on a non-confirmed settlement does neither.
                if (
                    current
                    and prior is not None
                    and (
                        (kind == "resolved" and event.get("via") == "recheck")
                        or (
                            kind == "rechecked"
                            and (prior.resolved or {}).get("outcome") == "CONFIRMED"
                        )
                    )
                    and prior.recheck_hold() is not None
                ):
                    applied = False
                if kind == "rechecked" and (
                    prior is None
                    or (
                        (prior.resolved or {}).get("outcome") != "CONFIRMED"
                        and not positive_recheck
                    )
                ):
                    applied = False
            stored = {
                **event,
                "revision": len(events) + 1,
                "based_on": len(events)
                if expected_revision is None
                else expected_revision,
                "applied": applied,
            }
            journal.append(stored)
            if require_applied and not applied:
                raise LedgerError("the attempt changed before the journal update")
            after = fold([*events, stored])
            if after is None:
                raise LedgerError("the journal has no current attempt")
            if _after_commit is not None and applied and current:
                # The durable append is accepted. Publish any required proof
                # before deleting its inputs, while the revision is still fenced.
                _after_commit(after)
            if cleanup is not None and applied and current and fresh:
                # Keep the short lock through cleanup: a newer hold cannot
                # interleave after permission was checked but before unlink.
                prune(cleanup)
            return after

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

        A release counts as settlement only when there was no read outcome.
        A workflow release must not erase an existing observation from the
        overlap check.
        """

        out = []
        for key in self.backend.keys():
            for attempt in _fold_attempts(self.backend.events(key)):
                resolved = attempt.resolved
                if attempt.released and resolved is None:
                    resolved = next(
                        e
                        for e in reversed(attempt.events)
                        if e.get("event") == "released"
                        and e.get("applied") is not False
                    )
                out.append((key, attempt.begun, resolved))
        return out

    def begun(self, key: str, attempt_id: str, **fields: Any) -> None:
        self._update(
            key,
            {
                "event": "begun",
                "attempt": attempt_id,
                "at": _now(),
                "t": time.time(),
                **fields,
            },
            require_applied=True,
        )

    def resolved(
        self,
        key: str,
        attempt_id: str,
        *,
        expected_revision: Optional[int] = None,
        cleanup: Optional[Path] = None,
        _after_commit: Optional[Callable[[Attempt], None]] = None,
        **fields: Any,
    ) -> Attempt:
        return self._update(
            key,
            {
                "event": "resolved",
                "attempt": attempt_id,
                "at": _now(),
                "t": time.time(),
                **fields,
            },
            expected_revision=expected_revision,
            cleanup=cleanup,
            _after_commit=_after_commit,
        )

    def rechecked(
        self,
        key: str,
        attempt_id: str,
        *,
        expected_revision: Optional[int] = None,
        **fields: Any,
    ) -> Attempt:
        """A recheck confirmed the attempt's provisional result: no
        recheck is owed any more (:attr:`Attempt.recheck_owed`)."""

        return self._update(
            key,
            {
                "event": "rechecked",
                "attempt": attempt_id,
                "at": _now(),
                "t": time.time(),
                **fields,
            },
            expected_revision=expected_revision,
        )

    def delivered(
        self,
        key: str,
        attempt_id: str,
        *,
        expected_revision: Optional[int] = None,
    ) -> Attempt:
        return self._update(
            key,
            {"event": "delivered", "attempt": attempt_id, "at": _now()},
            expected_revision=expected_revision,
            require_applied=True,
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
                return self._update(
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
                    expected_revision=attempt.revision,
                    cleanup=attempt.folder,
                    require_applied=True,
                    person_decision=True,
                )
            return self._update(
                key,
                {
                    "event": "released",
                    "attempt": attempt.attempt_id,
                    "at": _now(),
                    "t": time.time(),
                    "by": by.strip()[:128],
                    "reason": reason.strip()[:500],
                },
                expected_revision=attempt.revision,
                cleanup=attempt.folder,
                require_applied=True,
                person_decision=True,
            )
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

    def rechecks_owed(self) -> list[tuple[str, Attempt]]:
        """Every key whose latest attempt is a provisional ``CONFIRMED``
        still owed its recheck. Kept here, not in a server's memory, so a
        restart doesn't lose it."""

        out = []
        for key in self.backend.keys():
            attempt = self.latest(key)
            if attempt is not None and attempt.recheck_owed:
                out.append((key, attempt))
        return out

    def find_attempt(self, attempt_id: str) -> Optional[tuple[str, Attempt]]:
        """The key whose latest attempt has this id (a write id), or None
        when none does or more than one key claims it."""

        found = [
            key
            for key, begun, _ in self.attempts()
            if str(begun.get("attempt")) == attempt_id
        ]
        if len(found) != 1:
            return None
        attempt = self.latest(found[0])
        if attempt is None or attempt.attempt_id != attempt_id:
            return None
        return found[0], attempt


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def default_ledger() -> Ledger:
    """The :class:`FileLedger` in ``OPENADAPT_LEDGER_DIR`` or
    ``~/.openadapt/ledger``."""

    return Ledger(FileLedger(os.environ.get("OPENADAPT_LEDGER_DIR") or DEFAULT_DIR))

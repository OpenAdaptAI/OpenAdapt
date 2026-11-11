"""Single-host authority for the committed state of a file write journal.

The authority database and its backups must be protected independently of
the journal directory. The expected root, identity and generation come from
trusted configuration. This module does not provision that trust boundary.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import threading
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


class LedgerError(RuntimeError):
    """The ledger can't be read or written. Nothing may be sent."""


HoldCode = Literal[
    "ledger_authority_absent",
    "ledger_authority_unavailable",
    "ledger_authority_wrong_scope",
    "ledger_authority_stale",
    "ledger_authority_interrupted",
    "ledger_journal_mismatch",
    "ledger_legacy_unauthenticated",
]


class LedgerIntegrityHeld(LedgerError):
    """The current journal cannot authorize an action."""

    def __init__(self, code: HoldCode) -> None:
        self.code = code
        super().__init__(code)


_HEX = re.compile(r"[0-9a-f]{64}")


def _unique_heads(pairs: list[tuple[str, object]]) -> dict[str, object]:
    heads = dict(pairs)
    if len(heads) != len(pairs):
        raise ValueError
    return heads


@dataclass(frozen=True)
class JournalHead:
    revision: int
    digest: str


@dataclass(frozen=True)
class JournalCommit:
    revision: int
    heads: Mapping[str, JournalHead]
    legacy_keys: frozenset[str]


class _ReentrantFileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._thread_lock = threading.RLock()
        self._depth = 0
        self._handle: Any = None

    def acquire(self) -> None:
        self._thread_lock.acquire()
        if self._depth == 0:
            try:
                fd = os.open(
                    self.path,
                    os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                    0o600,
                )
                handle = os.fdopen(fd, "rb+")
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                except BaseException:
                    handle.close()
                    raise
                self._handle = handle
            except BaseException:
                self._thread_lock.release()
                raise
        self._depth += 1

    def release(self) -> None:
        self._depth -= 1
        if self._depth == 0:
            handle = self._handle
            self._handle = None
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        self._thread_lock.release()


_LOCKS: dict[Path, _ReentrantFileLock] = {}
_LOCKS_MUTEX = threading.Lock()


def _get_reentrant_file_lock(lock_path: Path) -> _ReentrantFileLock:
    resolved = lock_path.resolve()
    with _LOCKS_MUTEX:
        lock = _LOCKS.get(resolved)
        if lock is None:
            lock = _ReentrantFileLock(resolved)
            _LOCKS[resolved] = lock
        return lock


class SQLiteJournalAuthority:
    """Keep one committed inventory and at most one indeterminate append."""

    def __init__(
        self,
        path: str | Path,
        *,
        root: str | Path,
        ledger_id: str,
        generation: str,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.path = Path(path).expanduser().resolve()
        if (
            not ledger_id
            or not generation
            or self.path == self.root
            or self.root in self.path.parents
        ):
            raise LedgerIntegrityHeld("ledger_authority_wrong_scope")
        self._scope = (
            ledger_id,
            generation,
            hashlib.sha256(os.fsencode(self.root)).hexdigest(),
        )

    def _require_file(self) -> None:
        try:
            info = self.path.stat()
        except FileNotFoundError as exc:
            raise LedgerIntegrityHeld("ledger_authority_absent") from exc
        except OSError as exc:
            raise LedgerIntegrityHeld("ledger_authority_unavailable") from exc
        if not stat.S_ISREG(info.st_mode):
            raise LedgerIntegrityHeld("ledger_authority_unavailable")

    def _connect(self) -> sqlite3.Connection:
        self._require_file()
        db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=1)
        try:
            db.execute("PRAGMA synchronous=FULL")
        except BaseException:
            db.close()
            raise
        return db

    @contextmanager
    def serialized(self) -> Iterator[None]:
        """Serialize protected file reads, append phases and applied cleanup."""
        self._require_file()
        lock = _get_reentrant_file_lock(self.path.with_name(self.path.name + ".lock"))
        try:
            lock.acquire()
            try:
                yield
            finally:
                lock.release()
        except OSError as exc:
            raise LedgerIntegrityHeld("ledger_authority_unavailable") from exc

    @staticmethod
    def _encode(heads: Mapping[str, JournalHead]) -> str:
        return json.dumps(
            {key: [head.revision, head.digest] for key, head in heads.items()},
            sort_keys=True,
            separators=(",", ":"),
        )

    def initialize(self, heads: Mapping[str, JournalHead], lookup_key: bytes) -> None:
        """Explicitly create a trust boundary; imported keys remain historical."""
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(fd)
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    """CREATE TABLE journal_authority (
                       id INTEGER PRIMARY KEY CHECK (id = 1),
                       ledger_id TEXT NOT NULL, generation TEXT NOT NULL,
                       root_digest TEXT NOT NULL, revision INTEGER NOT NULL,
                       heads TEXT NOT NULL, legacy_keys TEXT NOT NULL,
                       lookup_key BLOB NOT NULL,
                       pending TEXT)"""
                )
                db.execute(
                    "INSERT INTO journal_authority "
                    "VALUES (1, ?, ?, ?, 0, ?, ?, ?, NULL)",
                    (
                        *self._scope,
                        self._encode(heads),
                        json.dumps(sorted(heads)),
                        lookup_key,
                    ),
                )
            fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except FileExistsError as exc:
            raise LedgerIntegrityHeld("ledger_authority_stale") from exc
        except (OSError, sqlite3.Error) as exc:
            raise LedgerIntegrityHeld("ledger_authority_interrupted") from exc

    def _read(self, db: sqlite3.Connection) -> tuple[JournalCommit, str | None]:
        row = db.execute(
            "SELECT ledger_id, generation, root_digest, revision, heads, "
            "legacy_keys, pending FROM journal_authority WHERE id = 1"
        ).fetchone()
        if row is None:
            raise LedgerIntegrityHeld("ledger_authority_absent")
        if row[:3] != self._scope:
            raise LedgerIntegrityHeld("ledger_authority_wrong_scope")
        revision, wire_heads, wire_legacy, pending = row[3:]
        try:
            raw_heads = json.loads(wire_heads, object_pairs_hook=_unique_heads)
            raw_legacy = json.loads(wire_legacy)
            if (
                type(revision) is not int
                or revision < 0
                or not isinstance(raw_heads, dict)
                or not isinstance(raw_legacy, list)
                or (pending is not None and not isinstance(pending, str))
            ):
                raise ValueError
            heads = {}
            for key, value in raw_heads.items():
                if (
                    not _HEX.fullmatch(key)
                    or not isinstance(value, list)
                    or len(value) != 2
                    or type(value[0]) is not int
                    or value[0] < 1
                    or not isinstance(value[1], str)
                    or not _HEX.fullmatch(value[1])
                ):
                    raise ValueError
                heads[key] = JournalHead(value[0], value[1])
            if not all(isinstance(key, str) and key in heads for key in raw_legacy):
                raise ValueError
            if len(set(raw_legacy)) != len(raw_legacy):
                raise ValueError
            return JournalCommit(revision, heads, frozenset(raw_legacy)), pending
        except (TypeError, ValueError) as exc:
            raise LedgerIntegrityHeld("ledger_authority_stale") from exc

    def snapshot(self) -> JournalCommit:
        try:
            with self.serialized(), closing(self._connect()) as db:
                current, pending = self._read(db)
                if pending is not None:
                    raise LedgerIntegrityHeld("ledger_authority_interrupted")
                return current
        except sqlite3.Error as exc:
            raise LedgerIntegrityHeld("ledger_authority_unavailable") from exc

    def lookup_key(self) -> bytes:
        try:
            with self.serialized(), closing(self._connect()) as db, db:
                db.execute("BEGIN")
                _, pending = self._read(db)
                if pending is not None:
                    raise LedgerIntegrityHeld("ledger_authority_interrupted")
                row = db.execute(
                    "SELECT lookup_key FROM journal_authority WHERE id = 1"
                ).fetchone()
                if row is None or not isinstance(row[0], bytes) or len(row[0]) != 32:
                    raise LedgerIntegrityHeld("ledger_authority_stale")
                return row[0]
        except sqlite3.Error as exc:
            raise LedgerIntegrityHeld("ledger_authority_unavailable") from exc

    def prepare(self, expected: JournalCommit) -> str:
        """Durably mark a single append pending before any journal bytes change."""
        token = secrets.token_hex(32)
        try:
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                current, pending = self._read(db)
                if pending is not None:
                    raise LedgerIntegrityHeld("ledger_authority_interrupted")
                if current != expected:
                    raise LedgerIntegrityHeld("ledger_authority_stale")
                db.execute(
                    "UPDATE journal_authority SET pending = ? WHERE id = 1", (token,)
                )
            return token
        except sqlite3.Error as exc:
            raise LedgerIntegrityHeld("ledger_authority_interrupted") from exc

    def commit(
        self, expected: JournalCommit, token: str, key: str, head: JournalHead
    ) -> None:
        """Commit the next head and inventory together, clearing the marker."""
        try:
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                current, pending = self._read(db)
                if pending != token:
                    raise LedgerIntegrityHeld("ledger_authority_interrupted")
                prior = current.heads.get(key)
                if current != expected or head.revision != (
                    prior.revision + 1 if prior else 1
                ):
                    raise LedgerIntegrityHeld("ledger_authority_stale")
                db.execute(
                    "UPDATE journal_authority SET heads = ?, revision = ?, "
                    "pending = NULL WHERE id = 1",
                    (self._encode({**current.heads, key: head}), current.revision + 1),
                )
        except sqlite3.Error as exc:
            raise LedgerIntegrityHeld("ledger_authority_interrupted") from exc

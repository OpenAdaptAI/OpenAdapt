"""Defensive storage checks on disposable, synthetic ledger roots."""

from __future__ import annotations

import fcntl
import hashlib
import multiprocessing
import os
import signal
import sqlite3
from multiprocessing.connection import Connection
from pathlib import Path
from unittest.mock import patch

import pytest

from openadapt_flow.write_ledger import (
    FileLedger,
    Ledger,
    LedgerIntegrityHeld,
    SQLiteJournalAuthority,
)

ATTEMPT = "synthetic-attempt"
RECEIPT = "a" * 64
DUE = "2030-01-01T00:00:00Z"
PERSON = "synthetic-reviewer"
REASON = "checked the synthetic record"
INTERRUPTED = "ledger_authority_interrupted"
UNAUTHENTICATED = "ledger_legacy_unauthenticated"


def _authority(
    path: Path, root: Path, *, ledger_id: str = "synthetic", generation: str = "1"
) -> SQLiteJournalAuthority:
    return SQLiteJournalAuthority(
        path, root=root, ledger_id=ledger_id, generation=generation
    )


def _key(ledger: Ledger, task: str = "synthetic-task") -> str:
    return ledger.key_for("synthetic-contract", {"value": "test-only"}, task)


@pytest.fixture
def protected(tmp_path: Path) -> tuple[FileLedger, Ledger, SQLiteJournalAuthority]:
    root = tmp_path / "journal"
    directory = tmp_path / "authority"
    directory.mkdir()
    authority = _authority(directory / "ledger.sqlite", root)
    FileLedger(root).enroll(authority, authorized=True)
    backend = FileLedger(root, authority=authority)
    return backend, Ledger(backend), authority


def _reopen(
    backend: FileLedger, authority: SQLiteJournalAuthority
) -> tuple[FileLedger, Ledger]:
    handle = _authority(authority.path, backend.root)
    restarted = FileLedger(backend.root, authority=handle)
    return restarted, Ledger(restarted)


@pytest.mark.parametrize("found", ["absent", "landed"])
def test_protected_append_restart_and_person_settlement(protected, found: str) -> None:
    backend, ledger, authority = protected
    key = _key(ledger)
    folder = backend.attempt_dir(ATTEMPT)
    retained = folder / "baseline.json"
    retained.write_text("synthetic baseline")
    ledger.begun(key, ATTEMPT, folder=str(folder), write_key_bound=True)
    resolved = ledger.resolved(
        key,
        ATTEMPT,
        outcome="CONFIRMED",
        delivered=True,
        write_key_bound=True,
        receipt_sha256=RECEIPT,
        result={"provisional": True, "recheck_due_at": DUE},
    )
    anchored = authority.snapshot()
    assert anchored.revision == 2 and anchored.heads[key].revision == 2
    assert not anchored.legacy_keys

    restarted, later = _reopen(backend, authority)
    assert restarted.integrity_protected
    assert _key(later) == key and restarted.keys() == [key]
    assert later.find_attempt(ATTEMPT) == (key, resolved)
    assert later.rechecks_owed() == [(key, resolved)]
    assert resolved.recheck_owed == DUE
    assert [event["revision"] for event in resolved.events] == [1, 2]
    assert retained.is_file()

    held = later.resolved(
        key,
        ATTEMPT,
        outcome="REFUTED",
        via="recheck",
        receipt_sha256=RECEIPT,
        expected_revision=resolved.revision,
    )
    assert held.state == "held" and held.resolved["outcome"] == "REFUTED"
    decision = later.release(key, by=PERSON, reason=REASON, found=found)
    assert decision.events[-1]["applied"] is True
    assert decision.events[-1]["by"] == PERSON
    assert decision.events[-1]["reason"] == REASON
    assert decision.released is (found == "absent")
    assert decision.state == ("free" if found == "absent" else "undelivered")
    assert decision.resolved is not None
    if found == "absent":
        assert decision.resolved == held.resolved
    else:
        assert decision.resolved["via"] == "person"
        assert decision.resolved["landed"] is True
    assert not retained.exists()
    _, reopened = _reopen(backend, authority)
    assert reopened.find_attempt(ATTEMPT) == (key, decision)
    assert reopened.rechecks_owed() == []


@pytest.mark.parametrize(
    ("fault", "code"),
    [
        ("absent", "ledger_authority_absent"),
        ("unavailable", "ledger_authority_unavailable"),
        ("other-ledger", "ledger_authority_wrong_scope"),
        ("other-generation", "ledger_authority_wrong_scope"),
    ],
)
def test_unbound_authority_cannot_supply_empty_or_dispatch(
    protected, fault: str, code: str
) -> None:
    backend, ledger, authority = protected
    key = _key(ledger)
    ledger.begun(key, ATTEMPT)
    expected = authority.snapshot()
    path = authority.path
    if fault == "absent":
        path = path.with_name("absent.sqlite")
    if fault == "unavailable":
        path = path.with_name("not-a-database")
        path.mkdir()
    invalid = _authority(
        path,
        backend.root,
        ledger_id="other" if fault == "other-ledger" else "synthetic",
        generation="2" if fault == "other-generation" else "1",
    )
    reopened = FileLedger(backend.root, authority=invalid)
    with pytest.raises(LedgerIntegrityHeld) as inventory:
        reopened.keys()
    assert inventory.value.code == code
    with pytest.raises(LedgerIntegrityHeld) as read:
        Ledger(reopened).latest(key)
    assert read.value.code == code
    with pytest.raises(LedgerIntegrityHeld) as lease:
        reopened.lease(key)
    assert lease.value.code == code
    with pytest.raises(LedgerIntegrityHeld) as mutation:
        Ledger(reopened).begun(key, "second-synthetic-attempt")
    assert mutation.value.code == code
    assert path.exists() is (fault != "absent")
    assert authority.snapshot() == expected


def test_exact_recheck_survives_restart_and_keeps_rejected_observation(
    protected,
) -> None:
    backend, ledger, authority = protected
    key = _key(ledger)
    ledger.begun(key, ATTEMPT, write_key_bound=True)
    initial = ledger.resolved(
        key,
        ATTEMPT,
        outcome="CONFIRMED",
        receipt_sha256=RECEIPT,
        result={"provisional": True, "recheck_due_at": DUE},
    )
    _, restarted = _reopen(backend, authority)
    rejected = restarted.resolved(
        key,
        ATTEMPT,
        outcome="CONFIRMED",
        expected_revision=initial.revision - 1,
    )
    assert rejected.events[-1]["applied"] is False
    assert rejected.recheck_owed == DUE
    checked = restarted.rechecked(
        key,
        ATTEMPT,
        outcome="CONFIRMED",
        receipt_sha256=RECEIPT,
        expected_revision=rejected.revision,
    )
    assert checked.rechecked is not None
    assert checked.rechecked["receipt_sha256"] == RECEIPT
    assert checked.recheck_owed is None
    assert checked.events[-2]["applied"] is False
    _, later = _reopen(backend, authority)
    assert later.find_attempt(ATTEMPT) == (key, checked)
    assert later.rechecks_owed() == []


def test_stale_authority_compare_does_not_leave_pending(protected) -> None:
    _, ledger, authority = protected
    key = _key(ledger)
    previous = authority.snapshot()
    ledger.begun(key, ATTEMPT)
    current = authority.snapshot()
    assert current.revision == previous.revision + 1
    with pytest.raises(LedgerIntegrityHeld) as failure:
        authority.prepare(previous)
    assert failure.value.code == "ledger_authority_stale"
    assert authority.snapshot() == current
    updated = ledger.resolved(key, ATTEMPT, outcome="REFUTED")
    assert updated.state == "held"


@pytest.mark.parametrize("gap", ["inventory", "tail"])
def test_incomplete_file_observation_holds_without_mutation(
    protected, monkeypatch: pytest.MonkeyPatch, gap: str
) -> None:
    backend, ledger, authority = protected
    key = _key(ledger)
    folder = backend.attempt_dir(ATTEMPT)
    retained = folder / "baseline.json"
    retained.write_text("synthetic baseline")
    before_key = authority.snapshot()
    ledger.begun(key, ATTEMPT, folder=str(folder))
    before_tail = authority.snapshot()
    held = ledger.resolved(key, ATTEMPT, outcome="REFUTED")
    committed = authority.snapshot()
    observed = before_key if gap == "inventory" else before_tail

    def incomplete_inventory(selected=None):
        return dict(observed.heads), [], b""

    # Return a prior genuine metadata view through the synthetic read seam.
    # The underlying synthetic journal is never modified by the fault.
    with monkeypatch.context() as scoped:
        scoped.setattr(backend, "_file_inventory", incomplete_inventory)
        for operation in (
            backend.keys,
            lambda: ledger.latest(key),
            lambda: ledger.resolved(key, ATTEMPT, outcome="CONFIRMED", cleanup=folder),
            lambda: ledger.release(key, by=PERSON, reason=REASON, found="absent"),
        ):
            with pytest.raises(LedgerIntegrityHeld) as failure:
                operation()
            assert failure.value.code == "ledger_journal_mismatch"
    assert authority.snapshot() == committed
    assert ledger.latest(key) == held
    assert retained.read_text() == "synthetic baseline"


@pytest.mark.parametrize("stage", ["prepared", "file-append", "commit"])
def test_interrupted_protected_append_stays_pending_and_preserves_cleanup(
    protected, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    backend, ledger, authority = protected
    key = _key(ledger)
    folder = backend.attempt_dir(ATTEMPT)
    retained = folder / "baseline.json"
    retained.write_text("synthetic baseline")
    ledger.begun(key, ATTEMPT, folder=str(folder))
    assert ledger.latest(key).state == "open"
    trace = []
    native_write_calls = 0
    prepare, append, commit = authority.prepare, backend._append, authority.commit

    def observe_prepare(expected):
        token = prepare(expected)
        trace.append("prepared")
        if stage == "prepared":
            raise LedgerIntegrityHeld(INTERRUPTED)
        return token

    write = os.write

    def short_write_then_interrupt(fd, data):
        nonlocal native_write_calls
        native_write_calls += 1
        if native_write_calls == 1:
            return write(fd, data[:1])
        raise OSError("synthetic interrupted append")

    def observe_append(key, event):
        trace.append("file-append")
        if stage == "file-append":
            with monkeypatch.context() as scoped:
                scoped.setattr(os, "write", short_write_then_interrupt)
                append(key, event)
        else:
            append(key, event)

    def unavailable_connection():
        raise sqlite3.OperationalError("synthetic commit interruption")

    def observe_commit(*args):
        trace.append("commit")
        with monkeypatch.context() as scoped:
            scoped.setattr(authority, "_connect", unavailable_connection)
            return commit(*args)

    with monkeypatch.context() as scoped:
        scoped.setattr(authority, "prepare", observe_prepare)
        scoped.setattr(backend, "_append", observe_append)
        scoped.setattr(authority, "commit", observe_commit)
        with pytest.raises(LedgerIntegrityHeld) as failure:
            ledger.resolved(key, ATTEMPT, outcome="CONFIRMED", cleanup=folder)
        assert failure.value.code == INTERRUPTED

    order = ["prepared", "file-append", "commit"]
    assert trace == order[: order.index(stage) + 1]
    assert native_write_calls == (2 if stage == "file-append" else 0)
    reopened, restarted = _reopen(backend, authority)
    for operation in (
        authority.snapshot,
        reopened.keys,
        lambda: restarted.latest(key),
        lambda: reopened.lease(key),
        lambda: restarted.release(key, by=PERSON, reason=REASON, found="absent"),
        lambda: restarted.resolved(key, ATTEMPT, outcome="CONFIRMED", cleanup=folder),
    ):
        with pytest.raises(LedgerIntegrityHeld) as held:
            operation()
        assert held.value.code == INTERRUPTED
    assert retained.read_text() == "synthetic baseline"


_PROCESS_TIMEOUT = 20


@pytest.fixture
def protected_workers():
    context = multiprocessing.get_context("spawn")
    workers = []

    def start(target, *args):
        receive, send = context.Pipe(duplex=False)
        worker = context.Process(target=target, args=(*args, send))
        try:
            worker.start()
        except BaseException:
            receive.close()
            worker.close()
            raise
        finally:
            send.close()
        workers.append((worker, receive))
        return worker, receive

    try:
        yield context.Event(), start
    finally:
        for worker, _ in workers:
            if worker.is_alive():
                worker.kill()
        for worker, receive in workers:
            receive.close()
            worker.join(timeout=_PROCESS_TIMEOUT)
        survivors = [worker for worker, _ in workers if worker.is_alive()]
        for worker, _ in workers:
            if worker not in survivors:
                worker.close()
        assert not survivors


def _message(receive: Connection):
    assert receive.poll(_PROCESS_TIMEOUT), "ledger worker did not reach its boundary"
    return receive.recv()


def _join_worker(worker, expected=0):
    worker.join(timeout=_PROCESS_TIMEOUT)
    assert worker.exitcode == expected


def _pause_after_prepare(
    root: Path,
    path: Path,
    key: str,
    cleanup: Path | None,
    proceed,
    output: Connection,
) -> None:
    authority = _authority(path, root)
    ledger = Ledger(FileLedger(root, authority=authority))
    prepare = authority.prepare

    def prepared(expected):
        token = prepare(expected)
        output.send("prepared")
        assert proceed.wait(timeout=2 * _PROCESS_TIMEOUT)
        return token

    with patch.object(authority, "prepare", prepared):
        result = ledger.resolved(key, ATTEMPT, outcome="CONFIRMED", cleanup=cleanup)
    output.send(("resolved", result.state))


def _contend_for_authority(
    root: Path, path: Path, operation: str, key: str, task: str, output: Connection
) -> None:
    authority = _authority(path, root)
    backend = FileLedger(root, authority=authority)
    ledger = Ledger(backend)
    lock = path.with_name(path.name + ".lock").stat()
    flock = fcntl.flock
    signaled = False

    def observe_lock(fd, mode):
        nonlocal signaled
        if not signaled and mode == fcntl.LOCK_EX:
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) == (lock.st_dev, lock.st_ino):
                # The real nonblocking probe must find the owner's flock held.
                with pytest.raises(BlockingIOError):
                    flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                signaled = True
                output.send("contended")
        return flock(fd, mode)

    with patch.object(fcntl, "flock", observe_lock):
        try:
            if operation == "lookup":
                result = _key(ledger, task)
            elif operation == "lease":
                lease = backend.lease(key)
                assert lease is not None
                lease.release()
                result = key
            else:
                assert operation == "append"
                ledger.begun(key, task)
                result = key
        except LedgerIntegrityHeld as held:
            output.send(("held", held.code))
        else:
            output.send((operation, result))


def _read_interrupted_authority(
    root: Path, path: Path, key: str, cleanup: Path, output: Connection
) -> None:
    authority = _authority(path, root)
    backend = FileLedger(root, authority=authority)
    ledger = Ledger(backend)
    refusals = []
    for operation in (
        authority.snapshot,
        lambda: _key(ledger),
        backend.keys,
        lambda: ledger.latest(key),
        lambda: backend.lease(key),
        lambda: ledger.release(key, by=PERSON, reason=REASON, found="absent"),
        lambda: ledger.resolved(key, ATTEMPT, outcome="CONFIRMED", cleanup=cleanup),
    ):
        with pytest.raises(LedgerIntegrityHeld) as held:
            operation()
        refusals.append(held.value.code)
    output.send(tuple(refusals))


def test_live_prepared_appender_serializes_protected_readers_and_writers(
    protected, protected_workers
) -> None:
    backend, ledger, authority = protected
    proceed, start = protected_workers
    key = _key(ledger)
    tasks = [f"synthetic-task-{i}" for i in range(3)]
    append_keys = {_key(ledger, task): task for task in tasks}
    assert len({key, *append_keys}) == 4
    ledger.begun(key, ATTEMPT)
    initial = authority.snapshot()
    prefix = backend.events(key)

    owner, prepared = start(
        _pause_after_prepare, backend.root, authority.path, key, None, proceed
    )
    assert _message(prepared) == "prepared"
    operations = [
        ("lookup", key, "synthetic-task"),
        ("lease", key, "synthetic-task"),
        *(("append", child_key, task) for child_key, task in append_keys.items()),
    ]
    contenders = []
    for operation, child_key, task in operations:
        worker, receive = start(
            _contend_for_authority,
            backend.root,
            authority.path,
            operation,
            child_key,
            task,
        )
        contenders.append((operation, child_key, worker, receive))
    try:
        for _, _, _, receive in contenders:
            assert _message(receive) == "contended"
    finally:
        proceed.set()

    assert _message(prepared) == ("resolved", "undelivered")
    _join_worker(owner)
    for operation, child_key, worker, receive in contenders:
        assert _message(receive) == (operation, child_key)
        _join_worker(worker)

    restarted, reopened = _reopen(backend, authority)
    expected_attempts = {key: ATTEMPT, **append_keys}
    current = authority.snapshot()
    assert current.revision == initial.revision + 4 and not current.legacy_keys
    assert set(restarted.keys()) == set(current.heads) == set(expected_attempts)
    for child_key, attempt_id in expected_attempts.items():
        attempt = reopened.latest(child_key)
        revisions = [1, 2] if child_key == key else [1]
        assert attempt is not None and attempt.attempt_id == attempt_id
        assert [event["revision"] for event in attempt.events] == revisions
        assert all(event["applied"] is True for event in attempt.events)
        assert current.heads[child_key].revision == attempt.revision == revisions[-1]
        wire = (backend.root / "entries" / f"{child_key}.jsonl").read_bytes()
        assert current.heads[child_key].digest == hashlib.sha256(wire).hexdigest()
        if child_key == key:
            assert attempt.events[:1] == prefix
            assert attempt.resolved is not None
            assert attempt.resolved["outcome"] == "CONFIRMED"
        else:
            assert attempt.state == "open"


def test_killed_prepared_appender_holds_protected_journal_without_cleanup(
    protected, protected_workers
) -> None:
    backend, ledger, authority = protected
    proceed, start = protected_workers
    key = _key(ledger)
    folder = backend.attempt_dir(ATTEMPT)
    retained = folder / "baseline.json"
    retained.write_text("synthetic baseline")
    retained_input = folder / "attempt.json"
    retained_input.write_text("synthetic input")
    ledger.begun(key, ATTEMPT, folder=str(folder))
    entry = backend.root / "entries" / f"{key}.jsonl"
    prefix = entry.read_bytes()

    owner, prepared = start(
        _pause_after_prepare, backend.root, authority.path, key, folder, proceed
    )
    assert _message(prepared) == "prepared"
    assert owner.is_alive()
    owner.kill()
    _join_worker(owner, -signal.SIGKILL)

    reader, result = start(
        _read_interrupted_authority, backend.root, authority.path, key, folder
    )
    assert _message(result) == (INTERRUPTED,) * 7
    _join_worker(reader)
    assert entry.read_bytes() == prefix
    assert retained.read_text() == "synthetic baseline"
    assert retained_input.read_text() == "synthetic input"


def test_legacy_enrollment_needs_quiescence_and_keeps_history_inspection_only(
    tmp_path: Path,
) -> None:
    root = tmp_path / "legacy"
    directory = tmp_path / "authority"
    directory.mkdir()
    authority = _authority(directory / "ledger.sqlite", root)
    legacy = FileLedger(root)
    ledger = Ledger(legacy)
    key = _key(ledger)
    ledger.begun(key, ATTEMPT)
    held = ledger.resolved(key, ATTEMPT, outcome="REFUTED")
    inspection = legacy.inspect_legacy(key)
    assert inspection.authenticated is False
    assert inspection.events == tuple(held.events)

    with pytest.raises(LedgerIntegrityHeld) as unauthorized:
        legacy.enroll(authority, authorized=False)
    assert unauthorized.value.code == UNAUTHENTICATED
    lease = legacy.lease(key)
    assert lease is not None
    try:
        with pytest.raises(LedgerIntegrityHeld) as busy:
            legacy.enroll(authority, authorized=True)
        assert busy.value.code == UNAUTHENTICATED
        assert not authority.path.exists()
    finally:
        lease.release()

    legacy.enroll(authority, authorized=True)
    protected_backend = FileLedger(root, authority=authority)
    protected_ledger = Ledger(protected_backend)
    assert authority.snapshot().legacy_keys == frozenset({key})
    assert protected_backend.keys() == [key]
    assert _key(protected_ledger) == key
    assert protected_backend.inspect_legacy(key) == inspection
    for operation in (
        lambda: protected_ledger.latest(key),
        lambda: protected_backend.lease(key),
        lambda: protected_ledger.begun(key, "second-synthetic-attempt"),
    ):
        with pytest.raises(LedgerIntegrityHeld) as refusal:
            operation()
        assert refusal.value.code == UNAUTHENTICATED
    assert protected_backend.inspect_legacy(key) == inspection

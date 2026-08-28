"""Bounded SQLite writer-lock recovery contracts."""

from __future__ import annotations

import multiprocessing
import os
import signal
import sqlite3
import threading
from functools import partial
from pathlib import Path
from time import monotonic, sleep
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from openadapt_capture import db, recorder, video
from openadapt_capture.db import crud
from openadapt_capture.db.models import MemoryStat, Recording
from openadapt_capture.extensions import synchronized_queue as sq
from openadapt_capture.recorder import (
    memory_writer,
    performance_stats_writer,
    video_post_callback,
    video_pre_callback,
    write_events,
    write_video_event,
)


def _operational_error(message):
    return sa.exc.OperationalError(
        "INSERT INTO memory_stat (...) VALUES (...)",
        {},
        sqlite3.OperationalError(message),
    )


def _locked_memory_stat_database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "SQLITE_BUSY_TIMEOUT_SECONDS", 0.01)
    # Keep the budget and the attempt ceiling proportional to that timeout so a
    # test of a lock that never clears finishes in a fraction of a second. The
    # production values are measured by their own tests below.
    monkeypatch.setattr(crud, "SQLITE_WRITE_LOCK_BUDGET_SECONDS", 1.0)
    monkeypatch.setattr(crud, "SQLITE_LOCK_ATTEMPT_CEILING_SECONDS", 0.05)
    db_path = tmp_path / "recording.db"
    engine, Session = db.create_db(str(db_path))
    setup_session = Session()
    recording = crud.insert_recording(
        setup_session,
        {
            "timestamp": 1.0,
            "monitor_width": 100,
            "monitor_height": 100,
            "platform": "test",
            "task_description": "SQLite lock retry",
        },
    )
    recording_id = recording.id
    setup_session.close()

    writer_session = Session()
    writer_session.execute(sa.text("SELECT 1"))
    locking_connection = sqlite3.connect(db_path)
    locking_connection.execute("BEGIN EXCLUSIVE")
    locking_connection.execute(
        "UPDATE recording SET task_description = task_description WHERE id = ?",
        (recording_id,),
    )
    event_data = {
        "recording_timestamp": 1.0,
        "recording_id": recording_id,
        "memory_usage_bytes": 1,
        "timestamp": 1,
    }
    return engine, writer_session, locking_connection, event_data


@pytest.mark.parametrize(
    "message",
    (
        "database is locked",
        "database table is locked: memory_stat",
        "database schema is locked: main",
    ),
)
def test_python_310_sqlite_lock_messages_are_retryable(message):
    assert crud._is_sqlite_lock_error(_operational_error(message))


def test_transient_sqlite_writer_lock_recovers(tmp_path, monkeypatch):
    engine, session, locking_connection, event_data = _locked_memory_stat_database(
        tmp_path, monkeypatch
    )
    retry_delays = []

    def release_lock(delay):
        retry_delays.append(delay)
        locking_connection.commit()

    monkeypatch.setattr(crud, "sleep", release_lock)
    try:
        crud._insert(session, event_data, MemoryStat)
        assert retry_delays, "the insert did not retry the held lock"
        assert session.query(MemoryStat).count() == 1
    finally:
        locking_connection.close()
        session.close()
        engine.dispose()


def test_persistent_sqlite_writer_lock_still_fails(tmp_path, monkeypatch):
    engine, session, locking_connection, event_data = _locked_memory_stat_database(
        tmp_path, monkeypatch
    )
    retry_delays = []
    monkeypatch.setattr(crud, "sleep", retry_delays.append)
    try:
        with pytest.raises(sa.exc.OperationalError, match="database is locked"):
            crud._insert(session, event_data, MemoryStat)

        assert retry_delays, "the insert did not retry the held lock"
        locking_connection.rollback()
        assert session.query(MemoryStat).count() == 0
    finally:
        locking_connection.close()
        session.close()
        engine.dispose()


def _recording_under_a_competing_writer(tmp_path, monkeypatch):
    """Create a capture database whose one write lock a second writer holds."""
    monkeypatch.setattr(db, "SQLITE_BUSY_TIMEOUT_SECONDS", 0.01)
    # Keep the budget and the attempt ceiling proportional to that timeout so a
    # test of a lock that never clears finishes in a fraction of a second. The
    # production values are measured by their own tests below.
    monkeypatch.setattr(crud, "SQLITE_WRITE_LOCK_BUDGET_SECONDS", 1.0)
    monkeypatch.setattr(crud, "SQLITE_LOCK_ATTEMPT_CEILING_SECONDS", 0.05)
    db_path = tmp_path / "recording.db"
    engine, Session = db.create_db(str(db_path))
    setup_session = Session()
    recording = crud.insert_recording(
        setup_session,
        {
            "timestamp": 2.0,
            "monitor_width": 100,
            "monitor_height": 100,
            "platform": "test",
            "task_description": "Video start time under contention",
        },
    )
    setup_session.close()

    video_writer_session = Session()
    # check_same_thread: one test releases this lock from the writer thread.
    competitor = sqlite3.connect(db_path, timeout=0.01, check_same_thread=False)
    competitor.execute("BEGIN IMMEDIATE")
    competitor.execute(
        "UPDATE recording SET task_description = task_description WHERE id = ?",
        (recording.id,),
    )
    return engine, video_writer_session, competitor, recording


def test_video_start_time_recovers_from_a_transient_writer_lock(tmp_path, monkeypatch):
    """The video writer must survive another writer holding the write lock.

    Without this recovery the recorder's ``video_writer`` process died of an
    unhandled OperationalError inside its startup callback, before it could
    announce readiness, and the whole recording failed.
    """
    engine, session, competitor, recording = _recording_under_a_competing_writer(
        tmp_path, monkeypatch
    )
    retry_delays = []

    def release_lock(delay):
        retry_delays.append(delay)
        competitor.commit()

    monkeypatch.setattr(crud, "sleep", release_lock)
    try:
        crud.update_video_start_time(session, recording, 1234.5)
        assert retry_delays, "the update did not retry the held lock"
        stored = session.execute(
            sa.select(Recording.video_start_time).where(Recording.id == recording.id)
        ).scalar_one()
        assert stored == pytest.approx(1234.5)
    finally:
        competitor.close()
        session.close()
        engine.dispose()


def test_video_start_time_fails_loud_under_a_held_lock(tmp_path, monkeypatch):
    """A lock that never clears must surface, never be silently skipped."""
    engine, session, competitor, recording = _recording_under_a_competing_writer(
        tmp_path, monkeypatch
    )
    retry_delays = []
    monkeypatch.setattr(crud, "sleep", retry_delays.append)
    try:
        with pytest.raises(sa.exc.OperationalError, match="database is locked"):
            crud.update_video_start_time(session, recording, 1234.5)

        assert retry_delays, "the update did not retry the held lock"
        competitor.rollback()
        stored = session.execute(
            sa.select(Recording.video_start_time).where(Recording.id == recording.id)
        ).scalar_one()
        assert stored is None
    finally:
        competitor.close()
        session.close()
        engine.dispose()


def test_video_start_time_reports_a_missing_recording(tmp_path, monkeypatch):
    """A recording row that is not there must be reported, not written blind."""
    monkeypatch.setattr(db, "SQLITE_BUSY_TIMEOUT_SECONDS", 0.01)
    db_path = tmp_path / "recording.db"
    engine, Session = db.create_db(str(db_path))
    session = Session()
    absent = Recording(id=404, timestamp=3.0)
    try:
        crud.update_video_start_time(session, absent, 1234.5)
        assert session.execute(sa.select(sa.func.count(Recording.id))).scalar_one() == 0
    finally:
        session.close()
        engine.dispose()


def test_the_real_video_writer_starts_through_a_writer_lock(tmp_path, monkeypatch):
    """The recorder's own video writer body must survive contention at startup.

    This drives the production ``write_events`` target with the production
    ``video_pre_callback``, against a database whose write lock another
    connection holds. It isolates the startup callback: encoding a frame would
    need a real FFmpeg process, and the callback is where the writer died.

    Run it in a thread rather than a process so the short busy timeout and the
    lock release apply. The process boundary itself is covered in
    tests/test_child_process_failure_reporting.py.
    """
    monkeypatch.setattr(signal, "signal", lambda *_args, **_kwargs: None)
    engine, session, competitor, recording = _recording_under_a_competing_writer(
        tmp_path, monkeypatch
    )
    session.close()

    released = []

    def release_lock(delay):
        released.append(delay)
        competitor.commit()

    monkeypatch.setattr(crud, "sleep", release_lock)

    terminate = multiprocessing.Event()
    started = multiprocessing.Event()
    perf_queue = sq.SynchronizedQueue()
    writer = threading.Thread(
        target=write_events,
        args=(
            "screen/video",
            write_video_event,
            sq.SynchronizedQueue(),
            multiprocessing.Value("i", 0),
            perf_queue,
            SimpleNamespace(id=recording.id, timestamp=recording.timestamp),
            str(tmp_path / "recording.db"),
            terminate,
            started,
            partial(
                video_pre_callback,
                video_dir=str(tmp_path),
                frame_size=(64, 48),
                provision=video.FFmpegProvision(
                    executable="ffmpeg-is-never-run-by-this-test",
                    codec="mpeg4",
                    pixel_format="yuv420p",
                    muxer="mp4",
                    source="test",
                ),
                timeout_seconds=5.0,
            ),
            video_post_callback,
        ),
    )
    writer.start()
    try:
        assert started.wait(timeout=30.0), "the video writer never announced readiness"
    finally:
        terminate.set()
        writer.join(timeout=30.0)
    try:
        assert not writer.is_alive(), "the video writer hung"
        assert released, "the video writer did not retry the held lock"
        stored = db.get_session_for_path(str(tmp_path / "recording.db")).execute(
            sa.select(Recording.video_start_time).where(Recording.id == recording.id)
        ).scalar_one()
        assert stored is not None
    finally:
        while not perf_queue.empty():
            perf_queue.get()
        competitor.close()
        engine.dispose()


def _capture_database_with_a_recording(tmp_path, task_description):
    """Create a per-capture database holding one recording row."""
    db_path = tmp_path / "recording.db"
    engine, Session = db.create_db(
        str(db_path), journal_mode=db.SQLITE_CAPTURE_JOURNAL_MODE
    )
    setup_session = Session()
    recording = crud.insert_recording(
        setup_session,
        {
            "timestamp": 4.0,
            "monitor_width": 100,
            "monitor_height": 100,
            "platform": "test",
            "task_description": task_description,
        },
    )
    detached = SimpleNamespace(id=recording.id, timestamp=recording.timestamp)
    setup_session.close()
    return engine, db_path, detached


def test_a_live_capture_database_keeps_a_write_log(tmp_path):
    """The contention this module bounds is first of all reduced.

    Under the default rollback journal every commit creates, syncs and deletes
    a journal file beside the capture. On Windows that churn costs about half a
    second per screenshot row, and a writer draining a backlog then holds the
    single write lock at nearly full duty cycle while the other writers starve.
    """
    engine, db_path, _ = _capture_database_with_a_recording(tmp_path, "journal mode")
    try:
        with engine.connect() as connection:
            mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar()
            sync = connection.exec_driver_sql("PRAGMA synchronous").scalar()
        assert str(mode).lower() == "wal"
        # 1 is NORMAL. A write log makes it safe against a process crash.
        assert sync == 1
    finally:
        engine.dispose()


def test_finalizing_a_capture_removes_the_write_log(tmp_path):
    """A sealed capture carries no sidecar files.

    ``terminal.build_artifact_manifest`` inventories every regular file under
    the capture directory, and the shared-memory file is created and removed by
    whoever opens the database next. A sealed capture that listed one would
    fail its own validation later.
    """
    engine, db_path, recording = _capture_database_with_a_recording(tmp_path, "seal")
    session = db.get_session_for_path(str(db_path))
    crud.insert_memory_stat(session, recording, 1, 1)
    assert Path(f"{db_path}-wal").exists(), "the live capture kept no write log"
    db.close_capture_session(session)
    engine.dispose()

    db.finalize_capture_database(str(db_path))

    assert not Path(f"{db_path}-wal").exists()
    assert not Path(f"{db_path}-shm").exists()
    # The finalized file must still read back through the read-only path the
    # recorder verifies and seals it with.
    read_only = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        assert read_only.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        assert read_only.execute("SELECT COUNT(*) FROM memory_stat").fetchone() == (1,)
    finally:
        read_only.close()


def _hold_the_write_lock(db_path, recording_id, release_from_another_thread=False):
    """Take the one write lock and hold it."""
    competitor = sqlite3.connect(
        db_path, timeout=0.01, check_same_thread=not release_from_another_thread
    )
    competitor.execute("BEGIN IMMEDIATE")
    competitor.execute(
        "UPDATE recording SET task_description = task_description WHERE id = ?",
        (recording_id,),
    )
    return competitor


def _memory_stat_row(recording):
    return {
        "recording_id": recording.id,
        "recording_timestamp": recording.timestamp,
        "memory_usage_bytes": 1,
        "timestamp": 1,
    }


def test_one_locked_attempt_costs_less_than_the_declared_ceiling(tmp_path):
    """Measure the number the budget's upper bound rests on.

    ``_write_with_lock_retry`` refuses to begin an attempt unless
    ``SQLITE_LOCK_ATTEMPT_CEILING_SECONDS`` of budget remains. That is what
    makes the budget an upper bound on the total wait rather than an estimate
    of it, and it holds only while one real attempt against a real held lock
    costs less than the ceiling. Measure it with the production busy timeout
    instead of asserting it: this is the value that used to be inherited, and
    a hosted Windows runner measured one attempt at about seven seconds.
    """
    engine, db_path, recording = _capture_database_with_a_recording(tmp_path, "ceiling")
    session = db.get_session_for_path(str(db_path))
    competitor = _hold_the_write_lock(db_path, recording.id)
    try:
        started = monotonic()
        with pytest.raises(sa.exc.OperationalError):
            session.execute(sa.insert(MemoryStat), [_memory_stat_row(recording)])
            session.commit()
        attempt_cost = monotonic() - started
    finally:
        session.rollback()
        competitor.rollback()
        competitor.close()
        session.close()
        engine.dispose()

    assert attempt_cost <= crud.SQLITE_LOCK_ATTEMPT_CEILING_SECONDS, (
        f"one attempt against a held lock cost {attempt_cost:.2f}s, over the "
        f"declared {crud.SQLITE_LOCK_ATTEMPT_CEILING_SECONDS:.2f}s ceiling"
    )


def test_the_total_wait_never_runs_past_the_declared_budget(tmp_path, monkeypatch):
    """A lock that never clears must cost the budget and not a second more.

    The defect this replaces expressed the wait as a count of attempts, so the
    total was whatever three attempts happened to cost: about twenty-two
    seconds on a hosted Windows runner, against a thirty-second readiness
    deadline. Measure the total against the declared budget instead.
    """
    # Scale the busy timeout, the attempt ceiling and the budget together, so
    # the loop behaves exactly as it does in production and the test still
    # finishes in about two seconds. The production attempt cost is measured by
    # test_one_locked_attempt_costs_less_than_the_declared_ceiling.
    monkeypatch.setattr(db, "SQLITE_BUSY_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(crud, "SQLITE_LOCK_ATTEMPT_CEILING_SECONDS", 0.2)
    monkeypatch.setattr(crud, "SQLITE_WRITE_LOCK_BUDGET_SECONDS", 2.0)
    engine, db_path, recording = _capture_database_with_a_recording(tmp_path, "budget")
    session = db.get_session_for_path(str(db_path))
    competitor = _hold_the_write_lock(db_path, recording.id)
    try:
        started = monotonic()
        with pytest.raises(sa.exc.OperationalError, match="database is locked"):
            crud._insert(session, _memory_stat_row(recording), MemoryStat)
        elapsed = monotonic() - started
    finally:
        competitor.rollback()
        competitor.close()
        session.close()
        engine.dispose()

    assert elapsed <= crud.SQLITE_WRITE_LOCK_BUDGET_SECONDS, (
        f"a permanently held lock cost {elapsed:.2f}s, over the declared "
        f"{crud.SQLITE_WRITE_LOCK_BUDGET_SECONDS:.1f}s budget"
    )
    # ... and it is spent, not abandoned. The replaced policy stopped after
    # three attempts, which against this lock is under a tenth of the budget.
    # That is the half of the contract a count of attempts cannot express.
    assert elapsed >= (
        crud.SQLITE_WRITE_LOCK_BUDGET_SECONDS
        - 2 * crud.SQLITE_LOCK_ATTEMPT_CEILING_SECONDS
    ), f"the helper gave up after {elapsed:.2f}s, long before its budget"


def test_the_write_lock_budget_fits_the_readiness_deadline():
    """The bound is only worth having if a writer can still announce readiness.

    ``recorder`` refuses to import when this does not hold, so this test states
    the same contract where a reader of the database code can see it.
    """
    assert (
        crud.SQLITE_WRITE_LOCK_BUDGET_SECONDS < recorder.STARTUP_READY_TIMEOUT_SECONDS
    )


def test_concurrent_writers_all_survive_a_busy_write_lock(tmp_path, monkeypatch):
    """Start the real writers together against one already-busy database.

    This is the shape the recorder actually fails in, and the shape the earlier
    regression test missed: production drives several writer bodies at the one
    per-capture database at the same time, and the lock they compete for is
    already held by whichever of them got there first. A test that drives a
    single writer against a synthetic lock cannot see a writer starve.

    Two hosted Windows runs of 0d14af17 recorded what starving looks like. In
    the qualification lane ``video_writer`` spent 22.8s on three attempts at
    its start-time update, never announced readiness, and failed the 30s
    deadline. In the tests lane ``mem_writer`` spent 21.9s on three attempts
    while the screen writer drained its backlog, exhausted them and exited 1.

    The busy timeout, the attempt ceiling and the budget are scaled together
    here so the whole test runs in a couple of seconds. What is NOT scaled is
    the ratio that decides the outcome: the lock stays held for many multiples
    of what one attempt costs, so a writer survives only by re-entering the
    race, not by waiting longer in any single attempt. Against the replaced
    policy every writer here dies, because three attempts is three chances.
    """
    monkeypatch.setattr(signal, "signal", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(db, "SQLITE_BUSY_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(crud, "SQLITE_LOCK_ATTEMPT_CEILING_SECONDS", 0.2)
    monkeypatch.setattr(crud, "SQLITE_WRITE_LOCK_BUDGET_SECONDS", 6.0)
    hold_seconds = 1.5

    engine, db_path, recording = _capture_database_with_a_recording(
        tmp_path, "concurrent writers"
    )
    competitor = _hold_the_write_lock(
        db_path, recording.id, release_from_another_thread=True
    )

    terminate = multiprocessing.Event()
    perf_queue = sq.SynchronizedQueue()
    for index in range(4):
        perf_queue.put(("screen", float(index), float(index) + 1.0))

    started_events = {
        name: multiprocessing.Event()
        for name in ("mem_writer", "perf_stats_writer", "video_writer")
    }
    failures: dict[str, BaseException] = {}

    def _guard(name, target):
        def _run():
            try:
                target()
            except BaseException as error:  # noqa: BLE001 - reported below
                failures[name] = error

        return _run

    writers = [
        threading.Thread(
            target=_guard(
                "mem_writer",
                partial(
                    memory_writer,
                    recording,
                    str(db_path),
                    terminate,
                    os.getpid(),
                    started_events["mem_writer"],
                ),
            )
        ),
        threading.Thread(
            target=_guard(
                "perf_stats_writer",
                partial(
                    performance_stats_writer,
                    perf_queue,
                    recording,
                    str(db_path),
                    terminate,
                    started_events["perf_stats_writer"],
                ),
            )
        ),
        threading.Thread(
            target=_guard(
                "video_writer",
                partial(
                    write_events,
                    "screen/video",
                    write_video_event,
                    sq.SynchronizedQueue(),
                    multiprocessing.Value("i", 0),
                    sq.SynchronizedQueue(),
                    recording,
                    str(db_path),
                    terminate,
                    started_events["video_writer"],
                    partial(
                        video_pre_callback,
                        video_dir=str(tmp_path),
                        frame_size=(64, 48),
                        provision=video.FFmpegProvision(
                            executable="ffmpeg-is-never-run-by-this-test",
                            codec="mpeg4",
                            pixel_format="yuv420p",
                            muxer="mp4",
                            source="test",
                        ),
                        timeout_seconds=5.0,
                    ),
                    video_post_callback,
                ),
            )
        ),
    ]

    releaser = threading.Timer(hold_seconds, competitor.commit)
    releaser.start()
    for writer in writers:
        writer.start()
    try:
        # The video writer announces readiness from its startup callback, which
        # is the write that starved on the qualification runner.
        assert started_events["video_writer"].wait(timeout=20.0), (
            "the video writer never announced readiness through a busy lock"
        )
        # The stats writers announce before their first write, so prove they
        # survived it instead: this is exactly how mem_writer died.
        reader = db.get_session_for_path(str(db_path))
        try:
            deadline = monotonic() + 20.0
            while monotonic() < deadline:
                reader.rollback()  # start a fresh read of what is committed
                committed = reader.execute(
                    sa.select(sa.func.count(MemoryStat.id))
                ).scalar_one()
                if committed:
                    break
                sleep(0.05)
            else:  # pragma: no cover - only reached on a regression
                pytest.fail("the memory writer committed nothing through a busy lock")
        finally:
            db.close_capture_session(reader)
    finally:
        releaser.cancel()
        terminate.set()
        for writer in writers:
            writer.join(timeout=30.0)
        competitor.close()
        engine.dispose()
        while not perf_queue.empty():
            perf_queue.get()

    assert not failures, f"a writer died under contention: {failures}"
    for writer in writers:
        assert not writer.is_alive(), "a writer hung under contention"
    for name, event in started_events.items():
        assert event.is_set(), f"{name} never announced readiness"

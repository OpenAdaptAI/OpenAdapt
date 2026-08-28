"""Bounded SQLite writer-lock recovery contracts."""

from __future__ import annotations

import multiprocessing
import signal
import sqlite3
import threading
from functools import partial
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from openadapt_capture import db, video
from openadapt_capture.db import crud
from openadapt_capture.db.models import MemoryStat, Recording
from openadapt_capture.extensions import synchronized_queue as sq
from openadapt_capture.recorder import (
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
        assert retry_delays == [crud.SQLITE_LOCK_RETRY_DELAYS_SECONDS[0]]
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

        assert retry_delays == list(crud.SQLITE_LOCK_RETRY_DELAYS_SECONDS)
        locking_connection.rollback()
        assert session.query(MemoryStat).count() == 0
    finally:
        locking_connection.close()
        session.close()
        engine.dispose()


def _recording_under_a_competing_writer(tmp_path, monkeypatch):
    """Create a capture database whose one write lock a second writer holds."""
    monkeypatch.setattr(db, "SQLITE_BUSY_TIMEOUT_SECONDS", 0.01)
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
        assert retry_delays == [crud.SQLITE_LOCK_RETRY_DELAYS_SECONDS[0]]
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

        assert retry_delays == list(crud.SQLITE_LOCK_RETRY_DELAYS_SECONDS)
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
        assert released == [crud.SQLITE_LOCK_RETRY_DELAYS_SECONDS[0]]
        stored = db.get_session_for_path(str(tmp_path / "recording.db")).execute(
            sa.select(Recording.video_start_time).where(Recording.id == recording.id)
        ).scalar_one()
        assert stored is not None
    finally:
        while not perf_queue.empty():
            perf_queue.get()
        competitor.close()
        engine.dispose()

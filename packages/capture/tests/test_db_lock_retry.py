"""Bounded SQLite writer-lock recovery contracts."""

from __future__ import annotations

import sqlite3

import pytest
import sqlalchemy as sa

from openadapt_capture import db
from openadapt_capture.db import crud
from openadapt_capture.db.models import MemoryStat


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

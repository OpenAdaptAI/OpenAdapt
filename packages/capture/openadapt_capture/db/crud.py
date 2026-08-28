"""CRUD operations for openadapt-capture database.

Copied from legacy OpenAdapt db/crud.py, adapted for per-capture databases.
Only import paths are changed; function signatures and logic are identical.
"""

import json
import sqlite3
from time import monotonic, sleep
from typing import Any, Callable, TypeVar

import sqlalchemy as sa
from loguru import logger
from sqlalchemy.orm import Session as SaSession

from openadapt_capture.db.models import (
    ActionEvent,
    AudioInfo,
    BrowserEvent,
    MemoryStat,
    PerformanceStat,
    Recording,
    Screenshot,
    WindowEvent,
)

# Type variable for generic model queries
BaseModelType = TypeVar("BaseModelType")
T = TypeVar("T")

BATCH_SIZE = 1

# The whole share of a startup-readiness deadline that ONE write transaction
# may spend waiting for the single SQLite write lock.
#
# Express the wait as time, never as a number of attempts. An attempt count
# gives no bound at all, because the cost of an attempt is whatever SQLite's
# busy handler decides: measured on a hosted Windows runner, one attempt with
# a five-second busy timeout took about seven seconds, so three attempts spent
# about twenty-two seconds and then gave up. Twenty-two seconds is both too
# long to fit a thirty-second readiness deadline and, at three samples of a
# busy lock, far too few chances to win the race.
#
# The same budget spent on short attempts samples the lock some tens of times
# instead of three. recorder.py checks this budget against its own readiness
# deadline when it is imported.
SQLITE_WRITE_LOCK_BUDGET_SECONDS = 20.0

# The most one attempt is allowed to cost.
#
# Every statement of the transaction may wait the connection's busy timeout,
# and SQLite's busy handler overshoots that timeout under contention. The
# helper below refuses to BEGIN an attempt unless this much of the budget
# remains, which is what makes the budget an upper bound on the total wait
# rather than an estimate of it.
#
# tests/test_db_lock_retry.py measures a real attempt against a real held lock
# and fails if it costs more than this.
SQLITE_LOCK_ATTEMPT_CEILING_SECONDS = 2.0

# Back off between attempts so the writers do not resample the lock in step,
# and so a writer that has just lost gives the winner room to commit. The delay
# grows to a cap; the budget, not the delay, decides when to stop.
SQLITE_LOCK_FIRST_RETRY_SECONDS = 0.05
SQLITE_LOCK_MAX_RETRY_SECONDS = 0.5
_SQLITE_LOCK_PRIMARY_CODES = {
    getattr(sqlite3, "SQLITE_BUSY", 5),
    getattr(sqlite3, "SQLITE_LOCKED", 6),
}

action_events = []
screenshots = []
window_events = []
browser_events = []
performance_stats = []
memory_stats = []


def _is_sqlite_lock_error(error: sa.exc.OperationalError) -> bool:
    """Return whether an OperationalError is SQLite lock contention."""
    original = error.orig
    if not isinstance(original, sqlite3.OperationalError):
        return False

    error_code = getattr(original, "sqlite_errorcode", None)
    if isinstance(error_code, int):
        primary_code = error_code & 0xFF
        return primary_code in _SQLITE_LOCK_PRIMARY_CODES

    message = str(original).lower()
    return any(
        lock_message in message
        for lock_message in (
            "database is locked",
            "database table is locked",
            "database schema is locked",
        )
    )


def _write_with_lock_retry(
    session: SaSession,
    write: Callable[[], T],
    statement_label: str,
) -> T:
    """Run one write transaction, recovering from bounded SQLite contention.

    ``write`` must perform every statement of the transaction and must be safe
    to run again from the start: a rollback discards its partial work before
    each retry.

    Every recorder writer process writes the one per-capture database file, so
    each of them competes for the single SQLite write lock. A connection whose
    bounded wait expires reports "database is locked", which is contention, not
    corruption.

    The retry runs against a clock, not a counter. It re-enters the race for as
    long as ``SQLITE_WRITE_LOCK_BUDGET_SECONDS`` allows, and it stops as soon
    as too little of that budget remains to finish another attempt. The total
    wait is therefore never more than the budget, whatever one attempt costs on
    the machine underneath.
    """
    deadline = monotonic() + SQLITE_WRITE_LOCK_BUDGET_SECONDS
    backoff = SQLITE_LOCK_FIRST_RETRY_SECONDS
    attempt = 0
    while True:
        attempt += 1
        started_at = monotonic()
        try:
            result = write()
            session.commit()
            return result
        except sa.exc.OperationalError as exc:
            if not _is_sqlite_lock_error(exc):
                raise
            waited = monotonic() - started_at

            # A failed execute or commit can leave the Session transaction
            # unusable. Roll it back before either retrying or failing loud.
            session.rollback()

            # Begin another attempt only if the budget can pay for one. This
            # test, not the count of attempts, is what bounds the total wait.
            remaining = deadline - monotonic()
            if remaining <= SQLITE_LOCK_ATTEMPT_CEILING_SECONDS:
                logger.error(
                    f"SQLite writer lock during {statement_label} did not clear "
                    f"within {SQLITE_WRITE_LOCK_BUDGET_SECONDS:.1f}s: attempt "
                    f"{attempt} waited {waited:.2f}s and the budget is spent"
                )
                raise

            # Never sleep away the room the next attempt needs.
            delay = min(backoff, remaining - SQLITE_LOCK_ATTEMPT_CEILING_SECONDS)
            logger.warning(
                f"SQLite writer lock during {statement_label} after waiting "
                f"{waited:.2f}s; retrying in {delay:.2f}s "
                f"(attempt {attempt}, {remaining:.1f}s of budget left)"
            )
            sleep(delay)
            backoff = min(backoff * 2, SQLITE_LOCK_MAX_RETRY_SECONDS)


def _execute_insert_with_lock_retry(
    session: SaSession,
    table: sa.Table,
    to_insert: list[dict[str, Any]],
) -> sa.engine.Result:
    """Commit one insert, with bounded recovery from SQLite writer contention."""
    return _write_with_lock_retry(
        session,
        lambda: session.execute(sa.insert(table), to_insert),
        "insert",
    )


def _insert(
    session: SaSession,
    event_data: dict[str, Any],
    table: sa.Table,
    buffer: list[dict[str, Any]] | None = None,
) -> sa.engine.Result | None:
    """Insert using Core API for improved performance (no rows are returned).

    Args:
        session (sa.orm.Session): The database session.
        event_data (dict): The event data to be inserted.
        table (sa.Table): The SQLAlchemy table to insert the data into.
        buffer (list, optional): A buffer list to store the inserted objects
            before committing. Defaults to None.

    Returns:
        sa.engine.Result | None: The SQLAlchemy Result object if a buffer is
          not provided. None if a buffer is provided.
    """
    db_obj = {column.name: None for column in table.__table__.columns}
    for key in db_obj:
        if key in event_data:
            val = event_data[key]
            db_obj[key] = val
            del event_data[key]

    # make sure all event data was saved
    assert not event_data, event_data

    if buffer is not None:
        buffer.append(db_obj)

    if buffer is None or len(buffer) >= BATCH_SIZE:
        to_insert = buffer or [db_obj]
        result = _execute_insert_with_lock_retry(session, table, to_insert)
        if buffer:
            buffer.clear()
        # Note: this does not contain the inserted row(s)
        return result


def insert_action_event(
    session: SaSession,
    recording: Recording,
    event_timestamp: int,
    event_data: dict[str, Any],
) -> None:
    """Insert an action event into the database.

    Args:
        session (sa.orm.Session): The database session.
        recording (Recording): The recording object.
        event_timestamp (int): The timestamp of the event.
        event_data (dict): The data of the event.
    """
    event_data = {
        **event_data,
        "timestamp": event_timestamp,
        "recording_id": recording.id,
        "recording_timestamp": recording.timestamp,
    }
    _insert(session, event_data, ActionEvent, action_events)


def insert_screenshot(
    session: SaSession,
    recording: Recording,
    event_timestamp: int,
    event_data: dict[str, Any],
) -> None:
    """Insert a screenshot into the database.

    Args:
        session (sa.orm.Session): The database session.
        recording (Recording): The recording object.
        event_timestamp (int): The timestamp of the event.
        event_data (dict): The data of the event.
    """
    event_data = {
        **event_data,
        "timestamp": event_timestamp,
        "recording_id": recording.id,
        "recording_timestamp": recording.timestamp,
    }
    _insert(session, event_data, Screenshot, screenshots)


def insert_window_event(
    session: SaSession,
    recording: Recording,
    event_timestamp: int,
    event_data: dict[str, Any],
) -> None:
    """Insert a window event into the database.

    Args:
        session (sa.orm.Session): The database session.
        recording (Recording): The recording object.
        event_timestamp (int): The timestamp of the event.
        event_data (dict): The data of the event.
    """
    event_data = {
        **event_data,
        "timestamp": event_timestamp,
        "recording_id": recording.id,
        "recording_timestamp": recording.timestamp,
    }
    _insert(session, event_data, WindowEvent, window_events)


def insert_browser_event(
    session: SaSession,
    recording: Recording,
    event_timestamp: int,
    event_data: dict[str, Any],
) -> None:
    """Insert a browser event into the database.

    Args:
        session (sa.orm.Session): The database session.
        recording (Recording): The recording object.
        event_timestamp (int): The timestamp of the event.
        event_data (dict): The data of the event.
    """
    event_data = {
        **event_data,
        "timestamp": event_timestamp,
        "recording_id": recording.id,
        "recording_timestamp": recording.timestamp,
    }
    _insert(session, event_data, BrowserEvent, browser_events)


def insert_perf_stat(
    session: SaSession,
    recording: Recording,
    event_type: str,
    start_time: float,
    end_time: float,
) -> None:
    """Insert an event performance stat into the database.

    Args:
        session (sa.orm.Session): The database session.
        recording (Recording): The recording object.
        event_type (str): The type of the event.
        start_time (float): The start time of the event.
        end_time (float): The end time of the event.
    """
    event_perf_stat = {
        "recording_timestamp": recording.timestamp,
        "recording_id": recording.id,
        "event_type": event_type,
        "start_time": start_time,
        "end_time": end_time,
    }
    _insert(session, event_perf_stat, PerformanceStat, performance_stats)


def insert_memory_stat(
    session: SaSession,
    recording: Recording,
    memory_usage_bytes: int,
    timestamp: int,
) -> None:
    """Insert memory stat into db.

    Args:
        session (sa.orm.Session): The database session.
        recording (Recording): The recording object.
        memory_usage_bytes (int): The memory usage in bytes.
        timestamp (int): The timestamp of the event.
    """
    memory_stat = {
        "recording_timestamp": recording.timestamp,
        "recording_id": recording.id,
        "memory_usage_bytes": memory_usage_bytes,
        "timestamp": timestamp,
    }
    _insert(session, memory_stat, MemoryStat, memory_stats)


def insert_recording(session: SaSession, recording_data: dict) -> Recording:
    """Insert the recording into to the db.

    Args:
        session (sa.orm.Session): The database session.
        recording_data (dict): The data of the recording.

    Returns:
        Recording: The recording object.
    """
    db_obj = Recording(**recording_data)
    session.add(db_obj)
    session.commit()
    session.refresh(db_obj)
    return db_obj


def _get(
    session: SaSession,
    table: BaseModelType,
    recording_id: int,
) -> list:
    """Retrieve records from the database table based on the recording id.

    Args:
        session (sa.orm.Session): The database session.
        table: The database table to query.
        recording_id (int): The recording id.

    Returns:
        list: A list of records retrieved from the database table,
          ordered by timestamp.
    """
    return (
        session.query(table)
        .filter(table.recording_id == recording_id)
        .order_by(table.timestamp)
        .all()
    )


def update_video_start_time(
    session: SaSession, recording: Recording, video_start_time: float
) -> None:
    """Update the video start time of a specific recording.

    Args:
        session (sa.orm.Session): The database session.
        recording (Recording): The recording object to update.
        video_start_time (float): The new video start time to set.
    """
    recording_id = recording.id

    # This is the first thing the video writer process does, and it runs while
    # the screen, action, performance and memory writer processes are already
    # committing to the same database file. It therefore has to wait for the
    # one SQLite write lock like every other writer, and it fails the whole
    # recording if that wait is not bounded and retried.
    #
    # Read nothing first: a read that finds no row is answered below by the
    # affected row count, and a session that has already read holds a
    # transaction the retry would have to unwind.
    session.rollback()

    def _write() -> int:
        result = session.execute(
            sa.update(Recording)
            .where(Recording.id == recording_id)
            .values(video_start_time=video_start_time)
        )
        return result.rowcount

    updated_rows = _write_with_lock_retry(
        session,
        _write,
        "video start time update",
    )

    if not updated_rows:
        logger.error(f"No recording found with id {recording_id}.")
        return

    logger.info(
        f"Updated video start time for recording {recording.timestamp} to"
        f" {video_start_time}."
    )


def insert_audio_info(
    session: SaSession,
    audio_data: bytes,
    transcribed_text: str,
    recording: Recording,
    timestamp: float,
    sample_rate: int,
    word_list: list,
) -> None:
    """Create an AudioInfo entry in the database.

    Args:
        session (sa.orm.Session): The database session.
        audio_data (bytes): The audio data.
        transcribed_text (str): The transcribed text.
        recording (Recording): The recording object.
        timestamp (float): The timestamp of the audio.
        sample_rate (int): The sample rate of the audio.
        word_list (list): A list of words with timestamps.
    """
    audio_info = AudioInfo(
        flac_data=audio_data,
        transcribed_text=transcribed_text,
        recording_timestamp=recording.timestamp,
        recording_id=recording.id,
        timestamp=timestamp,
        sample_rate=sample_rate,
        words_with_timestamps=json.dumps(word_list),
    )
    session.add(audio_info)
    session.commit()


def post_process_events(session: SaSession, recording: Recording) -> None:
    """Post-process events.

    Links action events to their screenshots and window events via IDs
    (during recording, only timestamps are stored; IDs are resolved after).

    Args:
        session (sa.orm.Session): The database session.
        recording (Recording): The recording to post-process.
    """
    screenshots_list = _get(session, Screenshot, recording.id)
    action_events_list = _get(session, ActionEvent, recording.id)
    window_events_list = _get(session, WindowEvent, recording.id)
    browser_events_list = _get(session, BrowserEvent, recording.id)

    screenshot_timestamp_to_id_map = {
        screenshot.timestamp: screenshot.id for screenshot in screenshots_list
    }
    screenshot_ordinal_to_id_map = {
        screenshot.source_ordinal: screenshot.id
        for screenshot in screenshots_list
        if screenshot.source_ordinal is not None
    }
    window_event_timestamp_to_id_map = {
        window_event.timestamp: window_event.id for window_event in window_events_list
    }
    window_event_ordinal_to_id_map = {
        window_event.source_ordinal: window_event.id
        for window_event in window_events_list
        if window_event.source_ordinal is not None
    }
    browser_event_timestamp_to_id_map = {
        browser_event.timestamp: browser_event.id
        for browser_event in browser_events_list
    }

    for action_event in action_events_list:
        action_event.screenshot_id = (
            screenshot_ordinal_to_id_map.get(action_event.screenshot_source_ordinal)
            if action_event.screenshot_source_ordinal is not None
            else screenshot_timestamp_to_id_map.get(action_event.screenshot_timestamp)
        )
        action_event.window_event_id = (
            window_event_ordinal_to_id_map.get(action_event.window_event_source_ordinal)
            if action_event.window_event_source_ordinal is not None
            else window_event_timestamp_to_id_map.get(action_event.window_event_timestamp)
        )
        action_event.browser_event_id = browser_event_timestamp_to_id_map.get(
            action_event.browser_event_timestamp
        )
    session.commit()

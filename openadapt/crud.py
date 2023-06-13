from loguru import logger
import sqlalchemy as sa

from openadapt.db import Session
from openadapt.models import (
    ActionEvent,
    Screenshot,
    Recording,
    WindowEvent,
    PerformanceStat,
)


BATCH_SIZE = 1

db = Session()
action_events = []
screenshots = []
window_events = []
performance_stats = []

def _insert(event_data, table, buffer=None):
    """Insert using Core API for improved performance (no rows are returned)"""

    db_obj = {
        column.name: None
        for column in table.__table__.columns
    }
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
        result = db.execute(sa.insert(table), to_insert)
        db.commit()
        if buffer:
            buffer.clear()
        # Note: this does not contain the inserted row(s)
        return result


def insert_action_event(recording_timestamp, event_timestamp, event_data):
    event_data = {
        **event_data,
        "timestamp": event_timestamp,
        "recording_timestamp": recording_timestamp,
    }
    _insert(event_data, ActionEvent, action_events)


def insert_screenshot(recording_timestamp, event_timestamp, event_data):
    event_data = {
        **event_data,
        "timestamp": event_timestamp,
        "recording_timestamp": recording_timestamp,
    }
    _insert(event_data, Screenshot, screenshots)


def insert_window_event(recording_timestamp, event_timestamp, event_data):
    event_data = {
        **event_data,
        "timestamp": event_timestamp,
        "recording_timestamp": recording_timestamp,
    }
    _insert(event_data, WindowEvent, window_events)

def insert_perf_stat(recording_timestamp, event_type, start_time, end_time):
    """
    Insert event performance stat into db
    """

    event_perf_stat = {
        "recording_timestamp": recording_timestamp,
        "event_type": event_type,
        "start_time": start_time,
        "end_time": end_time,
    }
    _insert(event_perf_stat, PerformanceStat, performance_stats)

def get_perf_stats(recording_timestamp):
    """
    return performance stats for a given recording
    """

    return (
        db
        .query(PerformanceStat)
        .filter(PerformanceStat.recording_timestamp == recording_timestamp)
        .order_by(PerformanceStat.start_time)
        .all()
    )

def insert_recording(recording_data):
    db_obj = Recording(**recording_data)
    db.add(db_obj)
    db.commit()
    db.refresh(db_obj)
    return db_obj


def get_latest_recording():
    return (
        db
        .query(Recording)
        .order_by(sa.desc(Recording.timestamp))
        .limit(1)
        .first()
    )


def get_recording(timestamp):
    return (
        db
        .query(Recording)
        .filter(Recording.timestamp == timestamp)
        .first()
    )


def _get(table, recording_timestamp):
    return (
        db
        .query(table)
        .filter(table.recording_timestamp == recording_timestamp)
        .order_by(table.timestamp)
        .all()
    )


def get_action_events(recording):
    return _get(ActionEvent, recording.timestamp)


def get_screenshots(recording, precompute_diffs=False):
    screenshots = _get(Screenshot, recording.timestamp)

    for prev, cur in zip(screenshots, screenshots[1:]):
        cur.prev = prev
    screenshots[0].prev = screenshots[0]

    # TODO: store diffs
    if precompute_diffs:
        logger.info(f"precomputing diffs...")
        [(screenshot.diff, screenshot.diff_mask) for screenshot in screenshots]

    return screenshots


def get_window_events(recording):
    return _get(WindowEvent, recording.timestamp)

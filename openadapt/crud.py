from loguru import logger
import sqlalchemy as sa

from openadapt.db import Session
from openadapt.models import (
    ActionEvent,
    Screenshot,
    Recording,
    WindowEvent,
    PerformanceStat,
    MemoryStat
)
from openadapt.config import STOP_SEQUENCES

BATCH_SIZE = 1

db = Session()
action_events = []
screenshots = []
window_events = []
performance_stats = []
memory_stats = []



def _insert(event_data, table, buffer=None):
    """Insert using Core API for improved performance (no rows are returned)"""

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
        db.query(PerformanceStat)
        .filter(PerformanceStat.recording_timestamp == recording_timestamp)
        .order_by(PerformanceStat.start_time)
        .all()
    )


  def insert_memory_stat(recording_timestamp, memory_usage_bytes, timestamp):
    """
    Insert memory stat into db
    """

    memory_stat = {
        "recording_timestamp": recording_timestamp,
        "memory_usage_bytes": memory_usage_bytes,
        "timestamp": timestamp,
    }
    _insert(memory_stat, MemoryStat, memory_stats)


def get_memory_stats(recording_timestamp):
    """
    return memory stats for a given recording
    """

    return (
        db
            .query(MemoryStat)
            .filter(MemoryStat.recording_timestamp == recording_timestamp)
            .order_by(MemoryStat.timestamp)
            .all()
    )


def insert_recording(recording_data):
    db_obj = Recording(**recording_data)
    db.add(db_obj)
    db.commit()
    db.refresh(db_obj)
    return db_obj


def get_latest_recording():
    return db.query(Recording).order_by(sa.desc(Recording.timestamp)).limit(1).first()


def get_recording(timestamp):
    return db.query(Recording).filter(Recording.timestamp == timestamp).first()


def _get(table, recording_timestamp):
    return (
        db.query(table)
        .filter(table.recording_timestamp == recording_timestamp)
        .order_by(table.timestamp)
        .all()
    )


def get_action_events(recording):
    action_events = _get(ActionEvent, recording.timestamp)
    # filter out stop sequences listed in STOP_SEQUENCES and Ctrl + C
    filter_stop_sequences(action_events)
    return action_events


def filter_stop_sequences(action_events):
    # check for ctrl c first
    # TODO: want to handle sequences like ctrl c the same way as normal sequences
    if len(action_events) >= 2:
        if (
            action_events[-1].canonical_key_char == "c"
            and action_events[-2].canonical_key_name == "ctrl"
        ):
            # remove ctrl c
            # ctrl c must be held down at same time, so no release event
            action_events.pop()
            action_events.pop()
            return

    # create list of indices for sequence detection
    # one index for each stop sequence in STOP_SEQUENCES
    # start from the back of the sequence
    stop_sequence_indices = [len(sequence) - 1 for sequence in STOP_SEQUENCES]

    # index of sequence to remove, -1 if none found
    sequence_to_remove = -1
    # number of events to remove
    num_to_remove = 0

    for i in range(0, len(STOP_SEQUENCES)):
        # iterate backwards through list of action events
        for j in range(len(action_events) - 1, -1, -1):
            # never go past 1st action event, so if a sequence is longer than
            # len(action_events), it can't have been in the recording
            if (
                action_events[j].canonical_key_char
                == STOP_SEQUENCES[i][stop_sequence_indices[i]]
                or action_events[j].canonical_key_name
                == STOP_SEQUENCES[i][stop_sequence_indices[i]]
            ) and action_events[j].name == "press":
                # for press events, compare the characters
                stop_sequence_indices[i] -= 1
                num_to_remove += 1
            elif action_events[j].name == "release" and (
                action_events[j].canonical_key_char in STOP_SEQUENCES[i]
                or action_events[j].canonical_key_name in STOP_SEQUENCES[i]
            ):
                # can consider any release event with any sequence char as part of the sequence
                num_to_remove += 1
            else:
                # not part of the sequence, so exit inner loop
                break

        if stop_sequence_indices[i] == -1:
            # completed whole sequence, so set sequence_to_remove to
            # current sequence and exit outer loop
            sequence_to_remove = i
            break

    if sequence_to_remove != -1:
        # remove that sequence
        for _ in range(0, num_to_remove):
            action_events.pop()


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

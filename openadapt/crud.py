import os
import time
import shutil

from datetime import datetime
from loguru import logger
from sqlalchemy.orm import sessionmaker
import sqlalchemy as sa

from openadapt.db import Session, get_base, get_engine, engine
from openadapt import config
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

def get_recording_by_id(recording_id):
    return (
        db
        .query(Recording)
        .filter_by(id=recording_id)
        .first()
    )


# Export to SQL
def export_sql(recording_id):
    engine = sa.create_engine(config.DB_URL)
    Session = sessionmaker(bind=engine)
    session = Session()

    # Query the recording based on the ID
    recording = get_recording_by_id(recording_id)

    if recording:
        # Generate SQL statements to insert the recording into the output file
        sql = f"INSERT INTO recording VALUES ({recording.id}, {recording.timestamp}, {recording.monitor_width}, {recording.monitor_height}, {recording.double_click_interval_seconds}, {recording.double_click_distance_pixels}, '{recording.platform}', '{recording.task_description}')"
        print(f"Recording with ID {recording_id} exported successfully.")
    else:
        print(f"No recording found with ID {recording_id}.")
    

    return sql
def create_db(recording_id):
    db.close()
    # fname_parts = [
    #     config.DB_FNAME,
    #     str(recording_id),
    #     datetime.now().strftime(config.DT_FMT),
    # ]
    # db_fname = "-".join(fname_parts)
    db_fname = f"recording_{recording_id}.db"

    # append to .env before running alembic
    # backup first
    t = time.time()
    # USE WINDOWS
    shutil.copyfile(config.ENV_FILE_PATH, f"{config.ENV_FILE_PATH}-{t}")
    # update current running configuration
    config.set_db_url(db_fname)

    with open(config.ENV_FILE_PATH, "r") as f:
        lines = f.readlines()

    # Replace the second line with the new value
    lines[1] = f"DB_FNAME='{db_fname}'\n"

    # Write the modified contents back to the .env file
    with open(config.ENV_FILE_PATH, "w") as f:
        f.writelines(lines)

    engine = sa.create_engine(config.DB_URL)
    Session = sessionmaker(bind=engine)
    session = Session()
    os.system("alembic upgrade head")
    db.engine = get_engine()

    # Retrieve the file path of the new database
    db_file_path = config.DB_FPATH.resolve()

    return t, db_file_path

# Restore database
def restore_db(timestamp):
    # TODO: Implement the restoration logic
    backup_file = "{}-{}".format(config.ENV_FILE_PATH, timestamp)
    shutil.copyfile(backup_file, config.ENV_FILE_PATH)

    # Undo other configuration changes if needed
    config.set_db_fname("openadapt.db")  # Reset the DB_FNAME to its initial state or set it to the appropriate value
    db.engine = get_engine()  # Revert the database engine to its previous state


def export_recording(recording_id):
    sql = export_sql(recording_id)
    t, db_file_path = create_db(recording_id)
    restore_db(t)
    # TODO: undo configuration changes made in create_db
    return db_file_path


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
        logger.info("precomputing diffs...")
        [(screenshot.diff, screenshot.diff_mask) for screenshot in screenshots]

    return screenshots


def get_window_events(recording):
    return _get(WindowEvent, recording.timestamp)

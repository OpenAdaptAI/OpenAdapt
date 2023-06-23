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


def insert_recording(recording_data):
    db_obj = Recording(**recording_data)
    db.add(db_obj)
    db.commit()
    db.refresh(db_obj)
    return db_obj


def get_latest_recording():
    return db.query(Recording).order_by(sa.desc(Recording.timestamp)).limit(1).first()


def get_recording_by_id(recording_id):
    return db.query(Recording).filter_by(id=recording_id).first()


def export_sql(recording_id):
    """Export the recording data as SQL statements.

    Args:
        recording_id (int): The ID of the recording.

    Returns:
        str: The SQL statement to insert the recording into the output file.
    """
    engine = sa.create_engine(config.DB_URL)
    Session = sessionmaker(bind=engine)
    session = Session()

    recording = get_recording_by_id(recording_id)

    if recording:
        sql = """
            INSERT INTO recording
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        values = (
            recording.id,
            recording.timestamp,
            recording.monitor_width,
            recording.monitor_height,
            recording.double_click_interval_seconds,
            recording.double_click_distance_pixels,
            recording.platform,
            recording.task_description
        )

        logger.info(f"Recording with ID {recording_id} exported successfully.")
    else:
        sql = ""
        logger.info(f"No recording found with ID {recording_id}.")

    return sql, values


def create_db(recording_id, sql, values):
    """Create a new database and import the recording data.

    Args:
        recording_id (int): The ID of the recording.
        sql (str): The SQL statements to import the recording.

    Returns:
        tuple: A tuple containing the timestamp and the file path of the new database.
    """
    db.close()
    db_fname = f"recording_{recording_id}.db"

    t = time.time()
    source_file_path = config.ENV_FILE_PATH
    target_file_path = f"{config.ENV_FILE_PATH}-{t}"
    logger.info(f"source_file_path={source_file_path}, target_file_path={target_file_path}")
    shutil.copyfile(source_file_path, target_file_path)
    config.set_db_url(db_fname)

    with open(config.ENV_FILE_PATH, "r") as env_file:
        env_file_lines = [
            f"DB_FNAME={db_fname}\n" if env_file_line.startswith("DB_FNAME") else env_file_line
            for env_file_line in env_file.readlines()
        ]

    with open(config.ENV_FILE_PATH, "w") as env_file:
        env_file.writelines(env_file_lines)

    engine = sa.create_engine(config.DB_URL)
    Session = sessionmaker(bind=engine)
    session = Session()
    os.system("alembic upgrade head")
    db.engine = engine

    with engine.begin() as connection:
        connection.execute(sql, values)

    db_file_path = config.DB_FPATH.resolve()

    return t, db_file_path


def restore_db(timestamp):
    """Restore the database to a previous state.

    Args:
        timestamp (float): The timestamp associated with the backup file.
    """
    backup_file = f"{config.ENV_FILE_PATH}-{timestamp}"
    shutil.copyfile(backup_file, config.ENV_FILE_PATH)
    config.set_db_url("openadapt.db")
    db.engine = get_engine()


def export_recording(recording_id):
    """Export a recording by creating a new database, importing the recording, and then restoring the previous state.

    Args:
        recording_id (int): The ID of the recording to export.

    Returns:
        str: The file path of the new database.
    """
    sql, values = export_sql(recording_id)
    t, db_file_path = create_db(recording_id, sql, values)
    restore_db(t)
    return db_file_path

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
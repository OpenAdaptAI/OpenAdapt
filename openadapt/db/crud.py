"""Implements basic CRUD operations for interacting with a database.

Module: crud.py
"""

from typing import Any, TypeVar
import asyncio
import json
import os
import time

from loguru import logger
from sqlalchemy.orm import Session as SaSession
from sqlalchemy.orm import joinedload
import psutil
import sqlalchemy as sa


from openadapt import utils
from openadapt.config import DATABASE_LOCK_FILE_PATH, config
from openadapt.db.db import Session, get_read_only_session_maker
from openadapt.models import (
    ActionEvent,
    AudioInfo,
    MemoryStat,
    PerformanceStat,
    Recording,
    Screenshot,
    ScrubbedRecording,
    WindowEvent,
    copy_sa_instance,
)
from openadapt.privacy.base import ScrubbingProvider

BATCH_SIZE = 1

lock = asyncio.Event()
lock.set()
action_events = []
screenshots = []
window_events = []
performance_stats = []
memory_stats = []


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
        result = session.execute(sa.insert(table), to_insert)
        session.commit()
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


def get_perf_stats(
    session: SaSession,
    recording: Recording,
) -> list[PerformanceStat]:
    """Get performance stats for a given recording.

    Args:
        session (sa.orm.Session): The database session.
        recording (Recording): The recording object.

    Returns:
        list[PerformanceStat]: A list of performance stats for the recording.
    """
    return (
        session.query(PerformanceStat)
        .filter(PerformanceStat.recording_id == recording.id)
        .order_by(PerformanceStat.start_time)
        .all()
    )


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


def get_memory_stats(
    session: SaSession,
    recording: Recording,
) -> list[MemoryStat]:
    """Return memory stats for a given recording.

    Args:
        session (sa.orm.Session): The database session.
        recording (Recording): The recording object.

    Returns:
        list[MemoryStat]: A list of memory stats for the recording.

    """
    return (
        session.query(MemoryStat)
        .filter(MemoryStat.recording_id == recording.id)
        .order_by(MemoryStat.timestamp)
        .all()
    )


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


def delete_recording(session: SaSession, recording: Recording) -> None:
    """Remove the recording from the db.

    Args:
        session (sa.orm.Session): The database session.
        recording (Recording): The recording object.
    """
    recording_timestamp = recording.timestamp
    session.query(Recording).filter(Recording.id == recording.id).delete()
    session.commit()

    utils.delete_performance_plot(recording_timestamp)

    from openadapt.video import delete_video_file

    delete_video_file(recording_timestamp)


def get_all_recordings(session: SaSession) -> list[Recording]:
    """Get all recordings.

    Args:
        session (sa.orm.Session): The database session.

    Returns:
        list[Recording]: A list of all original recordings.
    """
    return (
        session.query(Recording)
        .filter(Recording.original_recording_id == None)  # noqa: E711
        .order_by(sa.desc(Recording.timestamp))
        .all()
    )


def get_all_scrubbed_recordings(
    session: SaSession,
) -> list[ScrubbedRecording]:
    """Get all scrubbed recordings.

    Args:
        session (sa.orm.Session): The database session.

    Returns:
        list[ScrubbedRecording]: A list of all scrubbed recordings.
    """
    return session.query(ScrubbedRecording).all()


def get_latest_recording(session: SaSession) -> Recording:
    """Get the latest recording.

    Args:
        session (sa.orm.Session): The database session.

    Returns:
        Recording: The latest recording object.
    """
    return (
        session.query(Recording).order_by(sa.desc(Recording.timestamp)).limit(1).first()
    )


def get_recording_by_id(session: SaSession, recording_id: int) -> Recording:
    """Get the recording by an id.

    Args:
        session (sa.orm.Session): The database session.
        recording_id (int): The id of the recording.

    Returns:
        Recording: The latest recording object.
    """
    return session.query(Recording).filter_by(id=recording_id).first()


def get_recording(session: SaSession, timestamp: float) -> Recording:
    """Get a recording by timestamp.

    Args:
        session (sa.orm.Session): The database session.
        timestamp (float): The timestamp of the recording.

    Returns:
        Recording: The recording object.
    """
    return session.query(Recording).filter(Recording.timestamp == timestamp).first()


BaseModelType = TypeVar("BaseModelType")


def _get(
    session: SaSession,
    table: BaseModelType,
    recording_id: int,
    eager: bool = False,
    relationships: list[str] = None,
) -> list[BaseModelType]:
    """Retrieve records from the database table based on the recording timestamp.

    Args:
        session (sa.orm.Session): The database session.
        table (BaseModel): The database table to query.
        recording_id (int): The recording id.
        eager (bool, optional): If true, implement eager loading. Defaults to False.
        relationships (list[str], optional): List of relationships to eagerly load. Defaults to None.

    Returns:
        list[BaseModel]: A list of records retrieved from the database table,
          ordered by timestamp.
    """
    query = (
        session.query(table)
        .filter(table.recording_id == recording_id)
        .order_by(table.timestamp)
    )

    if eager and relationships:
        for rel in relationships:
            query = query.options(joinedload(getattr(table, rel)))

    return query.all()


def get_action_events(
    session: SaSession,
    recording: Recording,
) -> list[ActionEvent]:
    """Get action events for a given recording.

    Args:
        session (sa.orm.Session): The database session.
        recording (Recording): The recording object.

    Returns:
        list[ActionEvent]: A list of action events for the recording.
    """
    assert recording, "Invalid recording."
    action_events = _get(
        session, ActionEvent, recording.id, eager=True, relationships=["screenshot"]
    )
    action_events = filter_disabled_action_events(action_events)
    # filter out stop sequences listed in STOP_SEQUENCES and Ctrl + C
    filter_stop_sequences(action_events)
    return action_events


def filter_disabled_action_events(
    action_events: list[ActionEvent],
) -> list[ActionEvent]:
    """Filter out disabled action events.

    Args:
        action_events (list[ActionEvent]): A list of action events.

    Returns:
        list[ActionEvent]: A list of action events with disabled events removed.
    """
    return [event for event in action_events if not event.disabled]


def filter_stop_sequences(action_events: list[ActionEvent]) -> None:
    """Filter stop sequences.

    Args:
        List[ActionEvent]: A list of action events for the recording.

    Returns:
        None
    """
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
    stop_sequence_indices = [len(sequence) - 1 for sequence in config.STOP_SEQUENCES]

    # index of sequence to remove, -1 if none found
    sequence_to_remove = -1
    # number of events to remove
    num_to_remove = 0

    for i in range(0, len(config.STOP_SEQUENCES)):
        # iterate backwards through list of action events
        for j in range(len(action_events) - 1, -1, -1):
            # never go past 1st action event, so if a sequence is longer than
            # len(action_events), it can't have been in the recording
            if (
                action_events[j].canonical_key_char
                == config.STOP_SEQUENCES[i][stop_sequence_indices[i]]
                or action_events[j].canonical_key_name
                == config.STOP_SEQUENCES[i][stop_sequence_indices[i]]
            ) and action_events[j].name == "press":
                # for press events, compare the characters
                stop_sequence_indices[i] -= 1
                num_to_remove += 1
            elif action_events[j].name == "release" and (
                action_events[j].canonical_key_char in config.STOP_SEQUENCES[i]
                or action_events[j].canonical_key_name in config.STOP_SEQUENCES[i]
            ):
                # can consider any release event with any sequence char as
                # part of the sequence
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


def save_screenshot_diff(
    session: SaSession, screenshots: list[Screenshot]
) -> list[Screenshot]:
    """Save screenshot diff data to the database.

    Args:
        session (sa.orm.Session): The database session.
        screenshots (list[Screenshot]): A list of screenshots.

    Returns:
        list[Screenshot]: A list of screenshots with diff data saved to the db.
    """
    data_updated = False
    logger.info("verifying diffs for screenshots...")

    for screenshot in screenshots:
        if not screenshot.prev:
            continue
        if not screenshot.png_diff_data:
            screenshot.png_diff_data = screenshot.convert_png_to_binary(screenshot.diff)
            data_updated = True
        if not screenshot.png_diff_mask_data:
            screenshot.png_diff_mask_data = screenshot.convert_png_to_binary(
                screenshot.diff_mask
            )
            data_updated = True

    if data_updated:
        logger.info("saving screenshot diff data to db...")
        session.bulk_save_objects(screenshots)
        session.commit()

    return screenshots


def get_screenshots(
    session: SaSession,
    recording: Recording,
    save_diff: bool = False,
) -> list[Screenshot]:
    """Get screenshots for a given recording.

    Args:
        session (sa.orm.Session): The database session.
        recording (Recording): The recording object.

    Returns:
        list[Screenshot]: A list of screenshots for the recording.
    """
    screenshots = _get(
        session,
        Screenshot,
        recording.id,
        eager=True,
        relationships=["action_event", "recording"],
    )

    for prev, cur in zip(screenshots, screenshots[1:]):
        cur.prev = prev
    if screenshots:
        screenshots[0].prev = screenshots[0]

    if save_diff:
        screenshots = save_screenshot_diff(session, screenshots)
    return screenshots


def get_window_events(
    session: SaSession,
    recording: Recording,
) -> list[WindowEvent]:
    """Get window events for a given recording.

    Args:

        recording (Recording): The recording object.

    Returns:
        list[WindowEvent]: A list of window events for the recording.
    """
    return _get(
        session, WindowEvent, recording.id, eager=True, relationships=["action_events"]
    )


def disable_action_event(session: SaSession, event_id: int) -> None:
    """Disable an action event.

    Args:
        session (sa.orm.Session): The database session.
        event_id (int): The id of the event.
    """
    action_event: ActionEvent = (
        session.query(ActionEvent).filter(ActionEvent.id == event_id).first()
    )
    if action_event.recording.original_recording_id:
        raise ValueError("Cannot disable action events in a scrubbed recording.")
    if not action_event:
        raise ValueError(f"No action event found with id {event_id}.")
    action_event.disabled = True
    session.commit()


def get_new_session(
    read_only: bool = False,
    read_and_write: bool = False,
) -> sa.orm.Session:
    """Get a new database session.

    Args:
        read_only (bool): Whether to open the session in read-only mode.
        read_and_write (bool): Whether to open the session in read-and-write mode.

    Returns:
        sa.orm.Session: A new database session.
    """
    assert not (
        read_only and read_and_write
    ), "Cannot be both read-only and read-and-write."
    assert read_only or read_and_write, "Must be either read-only or read-and-write."
    if read_only:
        session = get_read_only_session_maker()()

        def raise_error_on_write(*args: Any, **kwargs: Any) -> None:
            """Raise an error when trying to write to a read-only session."""
            raise PermissionError("This session is read-only.")

        session.add = raise_error_on_write
        session.delete = raise_error_on_write
        session.commit = raise_error_on_write
        session.flush = raise_error_on_write

        return session
    return Session()


def update_video_start_time(
    session: SaSession, recording: Recording, video_start_time: float
) -> None:
    """Update the video start time of a specific recording.

    Args:
        session (sa.orm.Session): The database session.
        recording (Recording): The recording object to update.
        video_start_time (float): The new video start time to set.
    """
    # Find the recording by its timestamp
    recording = session.query(Recording).filter(Recording.id == recording.id).first()

    if not recording:
        logger.error(f"No recording found with timestamp {recording.timestamp}.")
        return

    # Update the video start time
    recording.video_start_time = video_start_time

    # the function is called from a different process which uses a different
    # session from the one used to create the recording object, so we need to
    # add the recording object to the session
    session.add(recording)
    # Commit the changes to the database
    session.commit()

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


def get_audio_info(
    session: SaSession,
    recording: Recording,
) -> AudioInfo:
    """Get the audio info for a given recording.

    Args:
        session (sa.orm.Session): The database session.
        recording (Recording): The recording object.

    Returns:
        AudioInfo: Audio info for the recording.
    """
    audio_infos = _get(session, AudioInfo, recording.id)
    return audio_infos[0] if audio_infos else None


def post_process_events(session: SaSession, recording: Recording) -> None:
    """Post-process events.

    Args:
        session (sa.orm.Session): The database session.
        recording (Recording): The recording to post-process.
    """
    screenshots = _get(session, Screenshot, recording.id)
    action_events = _get(session, ActionEvent, recording.id)
    window_events = _get(session, WindowEvent, recording.id)

    screenshot_timestamp_to_id_map = {
        screenshot.timestamp: screenshot.id for screenshot in screenshots
    }
    window_event_timestamp_to_id_map = {
        window_event.timestamp: window_event.id for window_event in window_events
    }

    for action_event in action_events:
        action_event.screenshot_id = screenshot_timestamp_to_id_map.get(
            action_event.screenshot_timestamp
        )
        action_event.window_event_id = window_event_timestamp_to_id_map.get(
            action_event.window_event_timestamp
        )
    session.commit()


def copy_recording(session: SaSession, recording_id: int) -> int:
    """Copy a recording.

    Args:
        session (sa.orm.Session): The database session.
        recording_id (int): The recording id to copy.

    Returns:
        int: The id of the new recording.
    """
    from openadapt.events import get_events

    try:
        recording = session.query(Recording).get(recording_id)
        new_recording = copy_sa_instance(recording, original_recording_id=recording.id)
        session.add(new_recording)
        session.commit()
        session.refresh(new_recording)

        def copy_action_event(
            action_event: ActionEvent, recording_id: int
        ) -> ActionEvent:
            new_action_event = copy_sa_instance(action_event, recording_id=recording_id)
            for child in action_event.children:
                new_child = copy_action_event(child, recording_id=recording_id)
                new_action_event.children.append(new_child)
            return new_action_event

        read_only_session = get_new_session(read_only=True)
        action_events = get_events(read_only_session, recording)
        new_action_events = [
            copy_action_event(action_event, recording_id=new_recording.id)
            for action_event in action_events
        ]

        screenshots = [action_event.screenshot for action_event in action_events]
        window_events = [action_event.window_event for action_event in action_events]

        for i, action_event in enumerate(new_action_events):
            action_event.screenshot = copy_sa_instance(
                screenshots[i], recording_id=new_recording.id
            )
            action_event.window_event = copy_sa_instance(
                window_events[i], recording_id=new_recording.id
            )
            session.add(action_event)

        session.commit()

        return new_recording.id
    except Exception as e:
        logger.error(f"Error copying recording: {e}")
        return None


@utils.trace(logger)
def scrub_item(item_id: int, table: sa.Table, scrubber: ScrubbingProvider) -> None:
    """Scrub an item in the database.

    Args:
        session (sa.orm.Session): The database session.
        item_id (int): The item id to scrub.
        table (sa.Table): The table to scrub the item from.
        scrubber (ScrubbingProvider): The scrubbing provider to use.
    """
    with get_new_session(read_and_write=True) as session:
        item = session.query(table).get(item_id)
        item.scrub(scrubber)
        session.commit()


def insert_scrubbed_recording(
    session: SaSession, recording_id: int, provider: str
) -> int:
    """Insert a scrubbed recording into the database.

    Args:
        session (sa.orm.Session): The database session.
        recording_id (int): The recording id to scrub.
        provider (str): The scrubbing provider to use.

    Returns:
        int: The id of the scrubbed recording.
    """
    scrubbed_recording = ScrubbedRecording(
        recording_id=recording_id,
        provider=provider,
        timestamp=utils.set_start_time(),
    )
    session.add(scrubbed_recording)
    session.commit()
    session.refresh(scrubbed_recording)
    scrubbed_recording_id = scrubbed_recording.id
    return scrubbed_recording_id


def mark_scrubbing_complete(session: SaSession, scrubbed_recording_id: int) -> None:
    """Mark scrubbing as complete for a recording."""
    scrubbed_recording = session.query(ScrubbedRecording).get(scrubbed_recording_id)
    scrubbed_recording.scrubbed = True
    session.commit()


def acquire_db_lock(timeout: int = 60) -> bool:
    """Check if the database is locked.

    Args:
        timeout (int): The timeout in seconds. Defaults to 60.
        Set to a negative value to wait indefinitely.

    Returns:
        bool: True if acquired the lock, False otherwise.
    """
    start = time.time()
    while True:
        if timeout > 0 and time.time() - start > timeout:
            logger.error("Failed to acquire database lock.")
            return False
        if os.path.exists(DATABASE_LOCK_FILE_PATH):
            with open(DATABASE_LOCK_FILE_PATH, "r") as lock_file:
                lock_info = json.load(lock_file)
            # check if the process is still running
            if psutil.pid_exists(lock_info["pid"]):
                logger.info("Database is locked. Waiting...")
                time.sleep(1)
            else:
                release_db_lock(raise_exception=False)
        else:
            with open(DATABASE_LOCK_FILE_PATH, "w") as lock_file:
                lock_file.write(json.dumps({"pid": os.getpid(), "time": time.time()}))
                logger.info("Database lock acquired.")
            break
    return True


def release_db_lock(raise_exception: bool = True) -> None:
    """Release the database lock.

    Args:
        raise_exception (bool): Whether to raise an exception if the lock file is
        not found.
    """
    try:
        os.remove(DATABASE_LOCK_FILE_PATH)
    except FileNotFoundError:
        if raise_exception:
            logger.error("Database lock file not found.")
            raise
    logger.info("Database lock released.")

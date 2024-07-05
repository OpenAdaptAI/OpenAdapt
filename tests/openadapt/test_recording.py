"""Module for testing the recording module."""

import multiprocessing
import time
import os
import pytest
from unittest.mock import patch
from openadapt import record, playback, db, config, utils
from openadapt.db import crud
from openadapt.models import Recording
from loguru import logger

RECORD_STARTED_TIMEOUT = 30  # seconds


@pytest.fixture
def setup_db():
    # Setup the database connection and return the session
    db_session = crud.get_new_session(read_and_write=True)
    yield db_session
    db_session.close()


def test_recording(setup_db):
    terminate_processing = multiprocessing.Event()
    terminate_recording = multiprocessing.Event()
    status_pipe, child_conn = multiprocessing.Pipe()

    # Start the recording process
    record_proc = multiprocessing.Process(
        target=record.record,
        args=(
            "test task",
            terminate_processing,
            terminate_recording,
            child_conn,
            False,
        ),
    )
    record_proc.start()

    # Wait for the recording.started signal
    start_time = time.time()
    while time.time() - start_time < RECORD_STARTED_TIMEOUT:
        if status_pipe.poll():
            message = status_pipe.recv()
            if message.get("type") == "record.started":
                logger.info(f"Received signal: {message}")
                break
        time.sleep(0.1)
    else:
        pytest.fail(
            f"recording.started signal not received within {RECORD_STARTED_TIMEOUT} seconds"
        )

    # Stop the recording by emitting the stop sequence
    with patch("openadapt.playback.emit_stop_sequence") as mock_emit_stop_sequence:
        mock_emit_stop_sequence()

    # Wait for the recording to stop
    terminate_processing.set()
    record_proc.join()

    # Assert the state of the database
    with setup_db as session:
        recording = (
            session.query(Recording).filter_by(task_description="test task").first()
        )
        assert recording is not None, "Recording not found in the database"

    # Assert the state of the filesystem
    video_file_path = utils.get_video_file_path(recording.timestamp)
    assert os.path.exists(video_file_path), "Video file not found"

    performance_plot_path = utils.get_performance_plot_file_path(recording.timestamp)
    assert os.path.exists(performance_plot_path), "Performance plot not found"


def test_record_functionality():
    # Set up multiprocessing communication
    parent_conn, child_conn = multiprocessing.Pipe()

    # Set up termination events
    terminate_processing = multiprocessing.Event()
    terminate_recording = multiprocessing.Event()

    # Start the recording process
    record_process = multiprocessing.Process(
        target=record.record,
        args=(
            "Test recording",
            terminate_processing,
            terminate_recording,
            child_conn,
            False,
        ),
    )
    record_process.start()

    logger.info("Waiting for the 'record.started' signal")
    logger.info(f"Parent conn: {parent_conn}")
    logger.info(f"Child conn: {child_conn}")
    logger.info(f"Parent conn poll: {parent_conn.poll()}")
    logger.info(f"Parent conn recv: {parent_conn.recv()}")
    logger.info(f"Message: {parent_conn.recv()['type']}")
    # Wait for the 'record.started' signal
    start_time = time.time()
    while time.time() - start_time < RECORD_STARTED_TIMEOUT:
        if parent_conn.poll(1):  # 1 second timeout for poll
            logger.info(f"Message: {parent_conn.recv()['type']}")
            message = parent_conn.recv()
            logger.info(f"Message: {message}")
            if message["type"] == "record.started":
                break
    else:
        pytest.fail("Timed out waiting for 'record.started' signal")

    # Emit stop sequence using playback
    keyboard_controller = playback.get_keyboard_controller()
    for char in config.STOP_SEQUENCES[0]:
        playback.play_key_event(
            playback.create_key_event("press", char), keyboard_controller
        )
        playback.play_key_event(
            playback.create_key_event("release", char), keyboard_controller
        )

    # Wait for the recording to stop
    terminate_recording.wait(timeout=RECORD_STARTED_TIMEOUT)
    if not terminate_recording.is_set():
        pytest.fail("Recording did not stop within the expected time")

    # Clean up the recording process
    record_process.join()
    if record_process.is_alive():
        record_process.terminate()

    # Assert database state
    with crud.get_new_session() as session:
        recording = session.query(Recording).order_by(Recording.id.desc()).first()
        assert recording is not None, "No recording was created in the database"
        assert recording.task_description == "Test recording"

    # Assert filesystem state
    video_path = utils.get_video_file_path(recording.timestamp)
    assert os.path.exists(video_path), f"Video file not found at {video_path}"

    performance_plot_path = utils.get_performance_plot_file_path(recording.timestamp)
    assert os.path.exists(
        performance_plot_path
    ), f"Performance plot not found at {performance_plot_path}"


if __name__ == "__main__":
    pytest.main([__file__])

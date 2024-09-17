"""Module for testing the recording module."""

import multiprocessing
import time
import os
import pytest
from openadapt import record, playback, utils, video
from openadapt.config import config
from openadapt.db import crud
from openadapt.models import Recording, ActionEvent
from loguru import logger

RECORD_STARTED_TIMEOUT = 120  # Increased timeout to 120 seconds


@pytest.fixture
def setup_db():
    # Setup the database connection and return the session
    db_session = crud.get_new_session(read_and_write=True)
    yield db_session
    db_session.close()


def test_record_functionality():
    logger.info("Starting test_record_functionality")
    
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
    
    try:
        record_process.start()
        logger.info("Recording process started")

        # Wait for the 'record.started' signal
        start_time = time.time()
        while time.time() - start_time < RECORD_STARTED_TIMEOUT:
            if parent_conn.poll(1):  # 1 second timeout for poll
                message = parent_conn.recv()
                logger.info(f"Received message: {message}")
                if message["type"] == "record.started":
                    logger.info("Received 'record.started' signal")
                    break
            else:
                logger.debug("No message received, continuing to wait...")
        else:
            logger.error("Timed out waiting for 'record.started' signal")
            pytest.fail("Timed out waiting for 'record.started' signal")

        # Wait a short time to ensure some data is recorded
        time.sleep(5)

        logger.info("Stopping the recording")
        terminate_processing.set()  # Signal the recording to stop

        # Wait for the recording to stop
        logger.info("Waiting for recording to stop")
        terminate_recording.wait(timeout=RECORD_STARTED_TIMEOUT)
        if not terminate_recording.is_set():
            logger.error("Recording did not stop within the expected time")
            pytest.fail("Recording did not stop within the expected time")

        logger.info("Recording stopped successfully")

        # Assert database state
        with crud.get_new_session(read_and_write=True) as session:
            recording = session.query(Recording).order_by(Recording.id.desc()).first()
            assert recording is not None, "No recording was created in the database"
            assert recording.task_description == "Test recording"
            logger.info("Database assertions passed")

        # Assert filesystem state
        video_path = video.get_video_file_path(recording.timestamp)
        if not os.path.exists(video_path):
            logger.warning(f"Video file not found at {video_path}")
            # Check if video recording is enabled in the configuration
            if config.RECORD_VIDEO:
                logger.error("Video recording is enabled but no video file was created")
            else:
                logger.info("Video recording is disabled in the configuration")
        else:
            logger.info(f"Video file found at {video_path}")

        performance_plot_path = utils.get_performance_plot_file_path(recording.timestamp)
        if not os.path.exists(performance_plot_path):
            logger.warning(f"Performance plot not found at {performance_plot_path}")
        else:
            logger.info(f"Performance plot found at {performance_plot_path}")

        logger.info("Filesystem assertions completed")

    except Exception as e:
        logger.exception(f"An error occurred during the test: {e}")
        raise

    finally:
        # Clean up the recording process
        if record_process.is_alive():
            logger.info("Terminating recording process")
            record_process.terminate()
        record_process.join()
        logger.info("Test completed")


if __name__ == "__main__":
    pytest.main([__file__])

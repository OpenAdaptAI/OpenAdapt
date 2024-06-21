"""Copy a recording from one computer to another.

Usage:
    python -m openadapt.share send --recording_id=1
    python -m openadapt.share receive <wormhole_code>
"""

from threading import Thread
from zipfile import ZIP_DEFLATED, ZipFile
import os
import re
import subprocess

from openadapt.build_utils import redirect_stdout_stderr
from openadapt.custom_logger import logger

with redirect_stdout_stderr():
    import fire

from openadapt import db, utils
from openadapt.config import RECORDING_DIR_PATH
from openadapt.db import crud
from openadapt.video import get_video_file_path

LOG_LEVEL = "INFO"
utils.configure_logging(logger, LOG_LEVEL)


def export_recording_to_folder(recording_id: int) -> None:
    """Export a recording to a zip file.

    Args:
        recording_id (int): The ID of the recording to export.

    Returns:
        str: The path of the created zip file.
    """
    # Create the directory if it doesn't exist
    os.makedirs(RECORDING_DIR_PATH, exist_ok=True)

    recording_db_path = db.export_recording(recording_id)

    assert recording_db_path, recording_db_path

    # Get the timestamp from the recording_db_path
    timestamp = extract_timestamp_from_filename(os.path.basename(recording_db_path))

    # Path to the source db file
    db_filename = f"recording_{recording_id}_{timestamp}.db"

    # Path to the compressed file
    zip_filename = f"recording_{recording_id}_{timestamp}.zip"
    zip_path = os.path.join(RECORDING_DIR_PATH, zip_filename)

    zipfile = ZipFile(zip_path, "w", ZIP_DEFLATED, compresslevel=9)
    zipfile.write(recording_db_path, arcname=db_filename)

    with crud.get_new_session(read_only=True) as session:
        recording = crud.get_recording_by_id(session, recording_id)
        recording_timestamp = recording.timestamp

    performance_plot_path = utils.get_performance_plot_file_path(recording_timestamp)
    if os.path.exists(performance_plot_path):
        zipfile.write(
            performance_plot_path, arcname=os.path.basename(performance_plot_path)
        )
        logger.info(f"added {performance_plot_path=}")

    video_file_path = get_video_file_path(recording_timestamp)
    if os.path.exists(video_file_path):
        zipfile.write(video_file_path, arcname=os.path.basename(video_file_path))
        logger.info(f"added {video_file_path=}")

    zipfile.close()

    logger.info(f"created {zip_path=}")

    # delete db file
    os.remove(recording_db_path)
    logger.info(f"deleted {recording_db_path=}")

    return zip_path


def extract_timestamp_from_filename(filename: str) -> int:
    """Extract timestamp from the filename.

    Args:
        filename (str): The filename containing the timestamp.

    Returns:
        int: The extracted timestamp.
    """
    # Regular expression to match the timestamp in the filename
    pattern = r"recording_\d+_(\d+)\.db"
    match = re.match(pattern, filename)
    if match:
        timestamp_str = match.group(1)
        return int(timestamp_str)
    else:
        raise ValueError("Invalid filename format.")


def send_file(file_path: str) -> None:
    """Send a file using the 'wormhole' command-line tool.

    Args:
        file_path (str): The path of the file to send.
    """
    # Construct the command
    command = ["wormhole", "send", file_path]

    # Execute the command
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as e:
        raise Exception(f"Error occurred while running 'wormhole send': {e}")


def send_recording(recording_id: int) -> None:
    """Export a recording to a zip file and send it to another computer.

    Args:
        recording_id (int): The ID of the recording to send.
    """
    zip_file_path = export_recording_to_folder(recording_id)
    print(zip_file_path)

    assert zip_file_path, zip_file_path
    try:
        send_file(zip_file_path)
        # File sent successfully
    except Exception as exc:
        logger.exception(exc)
    finally:
        # Delete the zip file after sending or in case of exception
        if os.path.exists(zip_file_path):
            os.remove(zip_file_path)
            logger.info(f"deleted {zip_file_path=}")


def receive_recording(wormhole_code: str) -> None:
    """Receive a recording zip file from another computer using a wormhole code.

    Args:
        wormhole_code (str): The wormhole code to receive the recording.
    """
    # Set the output directory path
    output_directory = RECORDING_DIR_PATH

    # Create the output directory if it doesn't exist
    os.makedirs(output_directory, exist_ok=True)

    zip_filename = "recording.zip"
    zip_path = os.path.join(output_directory, zip_filename)

    # Construct the command with the output directory option
    command = ["wormhole", "receive", "-o", zip_path, wormhole_code]

    # Execute the command
    try:
        subprocess.run(command, check=True)

        # Now, extract the database file from the zip file
        with ZipFile(zip_path, "r") as zip_ref:
            zip_ref.extractall(output_directory)

    except subprocess.CalledProcessError as exc:
        logger.exception(exc)
    finally:
        # Delete the zip file after receiving or in case of exception
        if os.path.exists(zip_path):
            os.remove(zip_path)
            logger.info(f"deleted {zip_path=}")


def upload_recording_to_s3(recording_id):
    """Upload a recording to an S3 bucket.

    Args:
        recording_id (int): The ID of the recording to upload.
    """

    def _inner():
        try:
            # Export the recording to a zip file
            zip_file_path = export_recording_to_folder(recording_id)

            # Upload the zip file to the S3 bucket
            utils.upload_file_to_s3(zip_file_path)

            # Delete the zip file after uploading
            if os.path.exists(zip_file_path):
                os.remove(zip_file_path)
                logger.info(f"deleted {zip_file_path=}")
        except Exception as exc:
            logger.exception(exc)

    Thread(target=_inner).start()


# Create a command-line interface using python-fire and utils.get_functions
if __name__ == "__main__":
    fire.Fire(
        {
            "send": send_recording,
            "receive": receive_recording,
        }
    )

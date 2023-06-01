"""Copy a recording from one computer to another

Usage:

    python -m openadapt.share send --recording_id=1 
    python -m openadapt.share receive --wormhole_code=<wormhole_code>
"""
from zipfile import ZipFile, ZIP_DEFLATED
import os
import subprocess

from loguru import logger
import fire

from openadapt import config, crud, utils


LOG_LEVEL = "INFO"
utils.configure_logging(logger, LOG_LEVEL)


def export_recording_to_folder(recording_id):
    """YOUR DOCSTRING HERE"""
    # TODO: export recording db file instead of the entire db file
    recording_db_path = crud.export_recording(recording_id)

    if recording_db_path:
        # Create the directory if it doesn't exist
        os.makedirs(config.ZIPPED_RECORDING_FOLDER_PATH, exist_ok=True)

        # Path to the source db file
        db_filename = f"recording_{recording_id}.db"

        # Path to the compressed file
        zip_filename = f"recording_{recording_id}.zip"
        zip_path = os.path.join(config.ZIPPED_RECORDING_FOLDER_PATH, zip_filename)

        # Create an in-memory zip file and add the db file
        with ZipFile(zip_path, "w", ZIP_DEFLATED, compresslevel=9) as zip_file:
            zip_file.write(config.ROOT_DIRPATH, arcname=db_filename)

        logger.info(f"Created zip file of the recording: {zip_path}")

        return zip_path


def send_file(file_path):
    # Construct the command
    command = ['wormhole', 'send', file_path]

    # Execute the command
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as e:
        raise Exception(f"Error occurred while running 'wormhole send': {e}")


def send_recording(recording_id):
    zip_file_path = export_recording_to_folder(recording_id)

    if zip_file_path:
        try:
            send_file(zip_file_path)
        except Exception as e:
            logger.error(str(e))
            # Handle the error as neede


def receive_recording(wormhole_code):
    # Construct the command
    command = ['wormhole', 'receive', wormhole_code]

    # Execute the command
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as e:
        logger.warning(f"Error occurred while running 'wormhole receive {wormhole_code}': {e}")

# Create a command-line interface using python-fire and utils.get_functions
if __name__ == "__main__":
    fire.Fire({
        'send': send_recording,
        'receive': receive_recording,
    })

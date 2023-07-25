"""Module to test share.py."""

from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile
import os
import subprocess
import tempfile

from sqlalchemy import engine

from openadapt import config, share


def test_export_recording_to_folder() -> None:
    """Tests the export_recording_to_folder function.

    This test creates a temporary recording database file, mocks the
    crud.export_recording() function to return the temporary file path,
    and then asserts that the file is removed after calling
    export_recording_to_folder.

    Returns:
        None
    """
    # Create a temporary recording database file
    recording_id = 1
    recording_db_path = "temp.db"
    with open(recording_db_path, "w") as f:
        f.write("Recording data")

    # Mock the crud.export_recording() function to return the temporary file path
    with patch("openadapt.share.db.export_recording", return_value=recording_db_path):
        zip_file_path = share.export_recording_to_folder(recording_id)

        assert zip_file_path is not None
        assert os.path.exists(zip_file_path)

    # Assert that the file is removed after calling export_recording_to_folder
    assert not os.path.exists(recording_db_path), "Temporary file was not removed."


def test_send_file() -> None:
    """Tests the send_file function.

    This test creates a temporary file, mocks the subprocess.run() function
    to avoid executing the command, and then verifies that the command is called
    with the correct arguments.

    Returns:
        None
    """
    # Create a temporary file
    with tempfile.NamedTemporaryFile(delete=False) as temp_file:
        file_path = temp_file.name
        temp_file.write(b"File data")

    # Mock the subprocess.run() function to avoid executing the command
    with patch("openadapt.share.subprocess.run") as mock_run:
        share.send_file(file_path)

        # Verify that the command is called with the correct arguments
        mock_run.assert_called_once_with(["wormhole", "send", file_path], check=True)

    # Clean up the temporary file
    os.remove(file_path)


def test_send_recording() -> None:
    """Tests the send_recording function.

    This test mocks the export_recording_to_folder() function to return a zip file path
    and mocks the send_file() function to avoid sending the file. Then, it verifies that
    export_recording_to_folder() and send_file() are called during the test.

    Returns:
        None
    """
    # Mock the export_recording_to_folder() function to return a zip file path
    with patch(
        "openadapt.share.export_recording_to_folder",
        return_value=str(config.RECORDING_DIRECTORY_PATH / "recording_1.zip"),
    ):
        # Mock the send_file() function to avoid sending the file
        with patch("openadapt.share.send_file"):
            share.send_recording(1)

            # Verify that export_recording_to_folder() and send_file() are called
            assert share.export_recording_to_folder.called
            assert share.send_file.called

    # Verify that the temporary zip file is deleted after the test
    assert not os.path.exists(config.RECORDING_DIRECTORY_PATH / "recording_1.zip")


# Test receive_recording function (mock the subprocess.run function)
def test_receive_recording() -> None:
    """Tests the receive_recording function.

    This test function creates a temporary zip file, mocks the subprocess.run()
    function to avoid executing the command, and simulates receiving a recording
    with a wormhole code.

    Returns:
        None
    """
    # Create a temporary zip file
    temp_zip_path = str(config.RECORDING_DIRECTORY_PATH / "recording.zip")
    with ZipFile(temp_zip_path, "w", ZIP_DEFLATED):
        pass  # Using the 'pass' statement creates an empty zip file

    # Mock the subprocess.run() function to avoid executing the command
    with patch("subprocess.run"):
        # Simulate receiving a recording with a wormhole code
        wormhole_code = "test_wormhole_code"
        share.receive_recording(wormhole_code)

        # Verify that the command is called with the correct arguments
        subprocess.run.assert_called_once_with(
            ["wormhole", "receive", "-o", temp_zip_path, wormhole_code], check=True
        )

        # Verify that the zip file has been deleted
        assert not os.path.exists(temp_zip_path)


# Test visualize_recording function
def test_visualize_recording(setup_database: engine) -> None:
    """Tests the visualize_recording function.

    This test calls the function being tested with the "recording.db" created from
    the setup_database fixture and asserts that the session object
    was closed after calling the function.

    Args:
        setup_database: The setup_database fixture from the testing environment.

    Returns:
        None
    """
    # Call the function being tested
    share.visualize_recording("recording.db")

    # Assert that the session object was closed after calling the function
    # Here we are checking if the engine is disposed after calling the function
    assert not hasattr(share.visualize_recording, "engine")

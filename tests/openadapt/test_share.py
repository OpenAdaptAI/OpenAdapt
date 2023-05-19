import os
import unittest
from unittest.mock import patch
from tempfile import TemporaryDirectory
from openadapt.share import export_recording_to_folder, send_file, send_recording, receive_recording

class ShareTestCase(unittest.TestCase):

    def setUp(self):
        self.recording_id = 1

    def test_export_recording_to_folder(self):
        with TemporaryDirectory() as temp_dir:
            os.environ['RECORDING_DIR_PATH'] = temp_dir

            # Create a dummy recording
            dummy_db_file = os.path.join(temp_dir, "recording_1.db")
            with open(dummy_db_file, "w") as file:
                file.write("dummy data")

            # Export the recording
            zip_file_path = export_recording_to_folder(self.recording_id)

            # Verify the zip file was created
            self.assertTrue(zip_file_path.endswith(".zip"))
            self.assertTrue(os.path.exists(zip_file_path))

    def test_send_file(self):
        with TemporaryDirectory() as temp_dir:
            dummy_file_path = os.path.join(temp_dir, "dummy.txt")
            with open(dummy_file_path, "w") as file:
                file.write("dummy data")

            with patch('subprocess.run') as mock_run:
                send_file(dummy_file_path)

                # Verify that the 'wormhole send' command was executed
                mock_run.assert_called_once_with(['wormhole', 'send', dummy_file_path], check=True)

    def test_send_recording(self):
        with TemporaryDirectory() as temp_dir:
            os.environ['RECORDING_DIR_PATH'] = temp_dir

            # Create a dummy recording
            dummy_db_file = os.path.join(temp_dir, "recording_1.db")
            with open(dummy_db_file, "w") as file:
                file.write("dummy data")

            # Export the recording
            zip_file_path = export_recording_to_folder(self.recording_id)

            with patch('openadapt.share.export_recording_to_folder') as mock_export:
                mock_export.return_value = zip_file_path

                with patch('openadapt.share.send_file') as mock_send:
                    send_recording(self.recording_id)

                    # Verify that export_recording_to_folder and send_file were called
                    mock_export.assert_called_once_with(self.recording_id)
                    mock_send.assert_called_once_with(zip_file_path)

    def test_receive_recording(self):
        wormhole_code = "123456"

        with patch('subprocess.run') as mock_run:
            receive_recording(wormhole_code)

            # Verify that the 'wormhole receive' command was executed
            mock_run.assert_called_once_with(['wormhole', 'receive', wormhole_code], check=True)

if __name__ == '__main__':
    unittest.main()

"""Copy a recording from one computer to another

Usage:

    python -m puterbot.share send --recording_id=1 --output_folder=output
    python -m puterbot.share receive --output_folder=output
"""

import os
import sys
import fire
import socket
import datetime
import wormhole
import utils  # Import the utils module
import config  # Import the config module

def send_recording(recording_id, output_folder):
    # Export the recording to a folder
    export_recording_to_folder(recording_id, output_folder)

    # Get the current hostname (of the sender)
    hostname = socket.gethostname()

    # Get the current date and time
    dt_str = utils.get_now_dt_str()

    # Format the recording file name
    recording_file = os.path.join(output_folder, f'puterbot.{hostname}.{dt_str}.db')

    # Create a wormhole
    with wormhole.create() as w:
        # Send the recording file
        w.send_file(recording_file)

        # Print the wormhole code
        print("Wormhole code:", w.get_code())

        # Wait for the transfer to complete
        w.wait_for_transfer_to_finish()

def receive_recording(output_folder):
    # Get the wormhole code from the user
    code = input("Enter the wormhole code: ")

    # Create a wormhole
    with wormhole.create() as w:
        # Set the wormhole code
        w.set_code(code)

        # Receive the recording file
        result = w.get_file()

        # Save the received file to the output folder
        # Use the filename provided by the sender
        output_file = os.path.join(output_folder, result['filename'])
        with open(output_file, 'wb') as f:
            f.write(result['file_data'])

        # Wait for the transfer to complete
        w.wait_for_transfer_to_finish()

# Create a command-line interface using python-fire and utils.get_functions
if __name__ == "__main__":
    fire.Fire(utils.get_functions(sys.modules[__name__]))
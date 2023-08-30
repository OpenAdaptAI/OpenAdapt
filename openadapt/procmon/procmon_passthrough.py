import mmap
import os
import signal
import subprocess
import time

# Define paths and settings
LOG_PATH = "test.pml"
BATCH_PATH = "start_procmon.bat"
CONVERSION_BATCH_PATH = "convert_to_csv.bat"  # Path to the conversion batch script
SLEEP_INTERVAL = 1  # Sleep for 1 second between checks


def exit_gracefully(signum, frame):
    # This will be executed when Ctrl+C is pressed.
    raise SystemExit("Exiting gracefully...")


# Register the exit_gracefully function to SIGINT (which is triggered by Ctrl+C)
signal.signal(signal.SIGINT, exit_gracefully)

# Call the batch script to initiate ProcMon
subprocess.call([BATCH_PATH])

# Wait for a moment to ensure ProcMon has started and the file is created
time.sleep(5)  # Wait for 5 seconds, adjust if necessary

# Start monitoring the .pml file
with open(LOG_PATH, "rb") as f:
    mmapped_file = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    last_position = 0

    while True:
        try:
            mmapped_file.seek(0, 2)  # Seek to end of file
            size = mmapped_file.tell()

            if size > last_position:
                mmapped_file.seek(last_position)
                new_data = mmapped_file.read(size - last_position)
                print(new_data)  # Or process the data as required
                last_position = size

            time.sleep(SLEEP_INTERVAL)
        except SystemExit:
            print("Stopping monitoring...")
            mmapped_file.close()
            break

# Call the batch script to convert .pml to .csv
subprocess.call([CONVERSION_BATCH_PATH])

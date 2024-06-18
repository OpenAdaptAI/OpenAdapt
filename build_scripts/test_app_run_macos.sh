#!/bin/bash

# unzip the app

ZIPFILE_PATH="$(pwd)/dist/OpenAdapt.app.zip"
unzip -o "$ZIPFILE_PATH" -d "$(pwd)/dist"

APP_PATH="$(pwd)/dist/OpenAdapt.app/Contents/MacOS/OpenAdapt.app"

# print current directory
echo "Current directory: $(pwd)"
echo "App path: $APP_PATH"

# Run the app
open "$APP_PATH"

# Allow some time for the application to launch
sleep 30

# Verify that the executable exists
if [ -z "$APP_PATH" ]; then
    echo "Error: Could not find executable in $APP_PATH"
    exit 1
fi

# Get the process IDs
PIDS=$(pgrep -f "$APP_PATH")

# Verify that the process IDs were found
if [ -z "$PIDS" ]; then
    echo "Error: Could not find process IDs for $APP_PATH"
    exit 1
fi

# Variable to track if any process is still running
ALL_PROCESSES_RUNNING=true

# Check if the processes are still running
for PID in $PIDS; do
    if ! ps -p $PID > /dev/null; then
        echo "Process $PID is not running"
        ALL_PROCESSES_RUNNING=false
        break
    fi
done

# Set the exit code variable based on the processes' status
if [ "$ALL_PROCESSES_RUNNING" = true ]; then
    EXIT_CODE=0
else
    EXIT_CODE=1
fi

echo "Exit code: $EXIT_CODE"
exit $EXIT_CODE

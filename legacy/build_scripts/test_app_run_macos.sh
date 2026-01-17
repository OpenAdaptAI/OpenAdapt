#!/bin/bash

# Unzip the app
ZIPFILE_PATH="$(pwd)/dist/OpenAdapt.app.zip"
unzip -o "$ZIPFILE_PATH" -d "$(pwd)/dist"

# Current directory for reference
echo "Current directory: $(pwd)"

# Path to the app
APP_PATH="$(pwd)/dist/OpenAdapt.app"
EXECUTABLE_PATH="$APP_PATH/Contents/MacOS/OpenAdapt"
echo "App path: $APP_PATH"
echo "Executable path: $EXECUTABLE_PATH"

# Verify app exists
if [ ! -d "$APP_PATH" ]; then
    echo "Error: Could not find app at $APP_PATH"
    exit 1
fi

if [ ! -f "$EXECUTABLE_PATH" ]; then
    echo "Error: Could not find executable at $EXECUTABLE_PATH"
    exit 1
fi

# Run the app
open "$APP_PATH"

# Wait for app to start
sleep 30

# Check for running process - try both the app name and binary name
if pgrep -f "OpenAdapt" > /dev/null || pgrep -f "OpenAdapt.app" > /dev/null; then
    echo "Process is running"
    EXIT_CODE=0
else
    echo "Error: Could not find process IDs for OpenAdapt"
    EXIT_CODE=1
fi

echo "Exit code: $EXIT_CODE"
exit $EXIT_CODE

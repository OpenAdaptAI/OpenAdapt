#!/bin/bash

# This script is used to run the OpenAdapt app on macOS from a terminal, because otherwise there are times
# when the app doesn't record events properly, and is extremely slow when updating events to the database.
# This bash script replaces the location of the actual executable file, which is copied to a different location
# when the app is built. This script is used to run the app from the terminal, and is not used in the actual app.

BASH_LOCATION=$(dirname $0)

osascript -e 'tell app "Terminal"
    do script "cd '$BASH_LOCATION' && ./OpenAdapt.app; exit;"
end tell'

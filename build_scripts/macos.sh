#!/bin/bash

BASH_LOCATION=$(dirname $0)

osascript -e 'tell app "Terminal"
    do script "cd '$BASH_LOCATION' && ./OpenAdapt.app; exit;"
end tell'

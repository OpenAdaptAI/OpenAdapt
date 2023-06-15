#!/bin/bash
set -e

# Remove OpenAdapt
Cleanup(){
    if [ -d "../OpenAdapt" ]; then
        cd ..
        rm -rf OpenAdapt
        echo "Deleted OpenAdapt directory"
    fi
} 

# Run a command and ensure it did not fail
RunAndCheck() {
    res=$($1)

    if [[ -z $res ]] || [[ $res == 0 ]]; then
        echo "Success: $2 : $res"
    else
        echo "Failed: $2 : $res"
        Cleanup
        exit 1
    fi
}

RunAndCheck "curl --output https://www.python.org/ftp/python/3.10.11/python-3.10.11-macos11.pkg" "download Python"

[ -d "OpenAdapt" ] && mv OpenAdapt OpenAdapt-$(date +%Y-%m-%d_%H-%M-%S)
RunAndCheck "git clone https://github.com/MLDSAI/OpenAdapt.git" "clone git repo"

cd OpenAdapt

RunAndCheck "python3.10 -m venv .venv" "create python virtual environment"
source .venv/bin/activate
pip install wheel

# the following line generates a warning:
#   Ignoring pywin32: markers 'sys_platform == "win32"' don't match your environment
pip install -r requirements.txt

# the following line generates a warning:
#   [notice] To update, run: pip install --upgrade pip
pip install -e .

RunAndCheck "alembic upgrade head" "update database"
RunAndCheck "python3.10 -m spacy download en_core_web_trf" "download spacy model"
RunAndCheck "pytest" "run tests"
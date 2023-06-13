#!/bin/bash
set -e

# TODO: Add Tesseract OCR installation: https://tesseract-ocr.github.io/tessdoc/Installation.html

# Run a command and ensure it did not fail
RunAndCheck() {
    res=$($1)

    if [[ -z $res ]] || [[ $res == 0 ]]; then
        echo "Success: $2 : $res"
    else
        echo "Failed: $2 : $res"
        exit 1
    fi
}
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

RunAndCheck "alembic upgrade head"
RunAndCheck "python3.10 -m spacy download en_core_web_trf"
RunAndCheck "pytest"
#!/bin/bash
set -e

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

# Install the required dependencies for the SVG Mixin
# Specifically for the Pypotrace and Cairo Python libraries
InstallSVGDependencies() {
    if [[ "$OSTYPE" == "linux-gnu"* ]]; then
        . /etc/os-release
        if [ "$ID" == "ubuntu"* ]; then
            RunAndCheck "sudo apt-get install build-essential python-dev libagg-dev libpotrace-dev pkg-config" "install Pypotrace's dependencies"
        elif [ "$ID" == "centos"* ]; then
            RunAndCheck "sudo yum -y groupinstall 'Development Tools' " "install Development Tools"
            RunAndCheck "sudo yum -y install agg-devel potrace-devel python-devel" "install Pypotrace's dependencies"
        else   
            echo "Unsupported Linux Distribution : $ID"
            exit 1
        fi
        pip install weasyprint
    elif [[ "$OSTYPE" == "darwin"* ]]; then
        RunAndCheck "brew install libagg pkg-config potrace" "install Pypotrace's dependencies"
        RunAndCheck "brew install cairo libffi" "install Cairo's dependencies"
    else
        echo "Unsupported Operating System : $OSTYPE"
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

InstallSVGDependencies

# the following line generates a warning:
#   [notice] To update, run: pip install --upgrade pip
pip install -e .

RunAndCheck "alembic upgrade head"
RunAndCheck "python3.10 -m spacy download en_core_web_trf"
RunAndCheck "pytest"
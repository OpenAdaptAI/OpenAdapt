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
# Specifically for Vtracer and Cairo
InstallSVGDependencies() {
    if [[ "$OSTYPE" == "linux-gnu"* ]]; then
        RunAndCheck "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh" "install rust"
        RunAndCheck "source $HOME/.cargo/env" "sourced the environment"
        RunAndCheck "cargo install vtracer" "download vtracer"
    elif [[ "$OSTYPE" == "darwin"* ]]; then
        RunAndCheck "brew install rust" "install rust"
        RunAndCheck "cargo install vtracer" "install vtracer"
        
        # need to install cairo
        cpu=$(sysctl machdep.cpu.brand_string)

        # Check if the computer has an intel chip
        if [[ $cpu == *"Intel"* ]]; then
            RunAndCheck "brew install cairo" "install cairo for Apple Intel chip"
        else
            RunAndCheck "arch -arm64 brew install cairo" "install cairo for Apple Silicon"
        fi
    else
        echo "Unsupported Operating System : $OSTYPE"
        exit 1
    fi
}

[ -d "OpenAdapt" ] && mv OpenAdapt OpenAdapt-$(date +%Y-%m-%d_%H-%M-%S)
RunAndCheck "git clone https://github.com/dianzrong/OpenAdapt.git" "clone git repo"

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
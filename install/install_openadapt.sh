#!/bin/bash
set -x
set -e

# Change these if a different version  is required
exactPythonVersion="3.10.11"
pythonCmd="python3.10"
pythonVerStr="Python 3.10*"
pythonInstaller="python-3.10.11-macos11 21.11.47.pkg"
pythonInstallerLoc="https://www.python.org/ftp/python/3.10.11/python-3.10.11-macos11.pkg"
pythonInstallerPath="$HOME/downloads/$pythonInstaller"

# Remove OpenAdapt if it exists
Cleanup() {
    if [ -d "../OpenAdapt" ]; then
        cd ..
        rm -rf OpenAdapt
        echo "Deleted OpenAdapt directory"
    fi
}

# Refresh Path Environment variable
Refresh() {
    export PATH="$PATH:$(echo "$PATH" | tr ':' '\n' | grep -v -e '^$' -e '^/usr/local/share/dotnet' -e '^/usr/local/bin' | uniq | tr '\n' ':')$(echo "$PATH" | tr ':' '\n' | grep -e '^/usr/local/share/dotnet' -e '^/usr/local/bin' | uniq | tr '\n' ':')"
}

# Run a command and ensure it did not fail
RunAndCheck() {
    res=$($1)

    if [[ -z $res ]] || [[ $res -eq 0 ]]; then
        echo "Success: $2 : $res"
    else
        echo "Failed: $2 : $res"
        Cleanup
        exit 1
    fi
}

# Install a command using brew
BrewInstall() {
    dependency=$1

    brew install "$dependency"
    if ! CheckCMDExists "$dependency"; then
        echo "Failed to download $dependency"
        Cleanup
        exit 1
    fi
}

# Return true if a command/exe is available
CheckCMDExists() {
    command=$1

    if command -v "$command" >/dev/null 2>&1; then
        return 0
    else
        return 1
    fi
}

CheckPythonExists() {
    # Use Python alias of required version if it exists
    if CheckCMDExists $pythonCmd; then
        return
    fi

    # Use Python exe if it exists and is the required version
    pythonGenCmd="python3"
    if CheckCMDExists $pythonGenCmd; then
        res=$(python --version)
        if echo "$res" | grep -q "$pythonVerStr"; then
            return
        fi
    fi

    # Install required Python version
    echo Installing Python

    # Check if Pyenv needs to be installed
    if ! CheckCMDExists "pyenv"; then
        brew install pyenv
    fi
    
    pyenv install $exactPythonVersion

    # to add pyenv to the PATH
    echo 'export PATH="$PYENV_ROOT/bin:$PATH"' >> ~/.bash_profile
    echo -e 'if command -v pyenv 1>/dev/null 2>&1; then\n  eval "$(pyenv init -)"\nfi' >> ~/.bash_profile
    reset
    
    # Make sure python is now available and the right version
    if CheckCMDExists $pythonCmd; then
        res=$(python --version)
        if [[ "$res" =~ $pythonVerStr ]]; then
            return
        fi
    fi

    # Otherwise, Python is not available
    echo "Error after installing python"
<<<<<<< HEAD
=======
    rm -r "$pythonInstallerPath"
>>>>>>> 374dd666093cde783f61a782f1e9046766478e30

    Cleanup
    exit
}

############################################################################################
# Installation starts here
############################################################################################

# Download brew
if ! CheckCMDExists "brew"; then
    echo Downloading brew
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh > /dev/null 2>&1)"
    Refresh # necessary ?
    brewExists=$(CheckCMDExists "brew")
    if ! CheckCMDExists "brew"; then
        echo "Failed to download brew"
        Cleanup
        exit 1
    fi
fi

if ! CheckCMDExists "git"; then
    BrewInstall "git"
fi

if ! CheckCMDExists "tesseract"; then
    BrewInstall "tesseract"
fi

CheckPythonExists

[ -d "OpenAdapt" ] && mv OpenAdapt OpenAdapt-$(date +%Y-%m-%d_%H-%M-%S)
RunAndCheck "git clone https://github.com/MLDSAI/OpenAdapt.git" "Clone git repo"

cd OpenAdapt

RunAndCheck "python3.10 -m venv .venv" "Create python virtual environment"
source .venv/bin/activate
pip install wheel

# the following line generates a warning:
#   Ignoring pywin32: markers 'sys_platform == "win32"' don't match your environment
pip install -r requirements.txt

# the following line generates a warning:
#   [notice] To update, run: pip install --upgrade pip
pip install -e .

RunAndCheck "alembic upgrade head" "Update database"
RunAndCheck "python3.10 -m spacy download en_core_web_trf" "Download spacy model"
RunAndCheck "pytest" "Run tests"

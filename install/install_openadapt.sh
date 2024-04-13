#!/bin/bash
set -e
set -x

################################    PARAMETERS   ################################

# Change these if a different version of Python is required
pythonCmd="python3.10"
pythonVerStr="Python 3.10*"
pythonInstallerLoc="https://www.python.org/ftp/python/3.10.11/python-3.10.11-macos11.pkg"
nvmCmd="nvm"
nvmInstallerLoc="https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh"

# Set default values when no parameters are provided
BRANCH=${BRANCH:-main}
REPO=${REPO:-OpenAdaptAI/OpenAdapt.git}

REPO_URL="https://github.com/$REPO"

################################ HELPER FUNCTIONS ################################

# Remove OpenAdapt if it exists
Cleanup() {
    if [ -d "../OpenAdapt" ]; then
        cd ..
        rm -rf OpenAdapt
        echo "Deleted OpenAdapt directory"
    fi
}

# Run a command and ensure it did not fail
RunAndCheck() {
    if $1 ; then
        echo "Success: $2"
    else
        echo "Failed: $2"
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
    pythonGenCmd="python"
    if CheckCMDExists $pythonGenCmd; then
        res=$(python3 --version)
        if echo "$res" | grep -q "$pythonVerStr"; then
            return
        fi
    fi

    # Install required Python version
    echo Installing Python
    brew install python@3.10

    # Make sure python is now available and the right version
    if CheckCMDExists $pythonCmd; then
        res=$(python3.10 --version)
        if [[ "$res" =~ $pythonVerStr ]]; then
            return
        fi
    fi

    # Otherwise, Python is not available
    echo "Error after installing python"
    Cleanup
    exit 1
}

CheckNVMExists() {
    if CheckCMDExists $nvmCmd; then
        return
    fi

    # Install NVM
    echo Installing NVM

    curl -o- $nvmInstallerLoc | bash

    export NVM_DIR="$HOME/.nvm"
    [ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"  # This loads nvm
    [ -s "$NVM_DIR/bash_completion" ] && \. "$NVM_DIR/bash_completion"  # This loads nvm bash_completion

    if ! CheckCMDExists $nvmCmd; then
        echo "Failed to install NVM"
        Cleanup
        exit 1
    fi
}

################################ INSTALLATION ################################

# Download brew
if ! CheckCMDExists "brew"; then
    echo Downloading brew, follow the instructions
    bash -c "`curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh`"

    # Check the type of chip
    cpu=$(sysctl machdep.cpu.brand_string)
    if [[ $cpu == *"Apple"* ]]; then
        # Add brew to PATH
        echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
        eval "$(/opt/homebrew/bin/brew shellenv)"
    fi

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
CheckNVMExists

[ -d "OpenAdapt" ] && mv OpenAdapt OpenAdapt-$(date +%Y-%m-%d_%H-%M-%S)
RunAndCheck "git clone $REPO_URL" "Clone git repo"
cd OpenAdapt
RunAndCheck "git checkout $BRANCH" "Checkout branch $BRANCH"

RunAndCheck "pip3.10 install poetry" "Install Poetry"
RunAndCheck "poetry install" "Install Python dependencies"
RunAndCheck "poetry run alembic upgrade head" "Update database"
RunAndCheck "poetry run install-dashboard" "Install dashboard dependencies"
RunAndCheck "poetry run pytest" "Run tests"
if [ -z "$SKIP_POETRY_SHELL" ]; then
    RunAndCheck "poetry shell" "Activate virtual environment"
fi
echo OpenAdapt installed successfully!

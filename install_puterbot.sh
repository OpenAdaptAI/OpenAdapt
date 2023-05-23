#!/bin/bash

# Check if Git is installed
if ! [ -x "$(command -v git)" ]; then
    echo "Git is not installed. Installing Git..."
    sudo apt-get update
    sudo apt-get install -y git
fi

# Check if Python 3.10 is installed
if ! [ -x "$(command -v python3.10)" ]; then
    echo "Python 3.10 is not installed. Installing Python 3.10..."
    sudo apt-get update
    sudo apt-get install -y software-properties-common
    sudo add-apt-repository -y ppa:deadsnakes/ppa
    sudo apt-get update
    sudo apt-get install -y python3.10 python3.10-venv
fi

# Clone the OpenAdapt repository
git clone https://github.com/MLDSAI/puterbot.git
cd puterbot || exit

# Create and activate a Python virtual environment
python3.10 -m venv .venv
source .venv/bin/activate

# Install required packages and libraries
pip install wheel
pip install -r requirements.txt
pip install -e .

# Run the database migration
alembic upgrade head

# Download the spaCy model for Scrubbing
python3.10 -m spacy download en_core_web_lg

# Run the test suite
pytest

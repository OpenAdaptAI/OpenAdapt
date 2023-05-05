# Check if Git is installed
if (!(Test-Path -Path "C:\Program Files\Git\cmd\git.exe")) {
    Write-Host "Git is not installed. Installing Git..." -ForegroundColor Yellow
    Start-Process -FilePath "https://git-scm.com/download/win" -Wait
}

# Check if Python 3.10 is installed
if (!(Test-Path -Path "C:\Program Files\Python310\python.exe")) {
    Write-Host "Python 3.10 is not installed. Installing Python 3.10..." -ForegroundColor Yellow
    Start-Process -FilePath "https://www.python.org/ftp/python/3.10.0/python-3.10.0-amd64.exe" -Wait
}

# Clone the OpenAdapt repository
git clone https://github.com/MLDSAI/puterbot.git
cd puterbot

# Create and activate a Python virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install required packages and libraries
pip install wheel
pip install -r requirements.txt
pip install -e .

# Run the database migration
alembic upgrade head

# Run the test suite
pytest

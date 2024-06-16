@echo off

REM Set PowerShell execution policy to unrestricted
powershell -Command "Set-ExecutionPolicy Unrestricted -Scope Process"

cd P:\OpenAdapt

REM Activate the virtual environment
call .\.venv\Scripts\activate

REM Run the Python script
python -u "P:\\OpenAdapt\\chrome_extension\\browser.py"

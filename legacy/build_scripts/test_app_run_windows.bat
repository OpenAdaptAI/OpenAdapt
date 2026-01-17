@echo off

REM Unzip the distribution
set "ZIP_PATH=%cd%\dist\OpenAdapt.zip"
7z x %ZIP_PATH% -o%cd%\dist

REM Path to the .exe file
set "APP_PATH=%cd%\dist\OpenAdapt\OpenAdapt.exe"

REM Run the app
start %APP_PATH%

REM Allow some time for the application to launch
ping -n 30 127.0.0.1 >nul

REM Verify that the executable exists
if not exist "%APP_PATH%" (
    echo Error: Could not find executable in %APP_PATH%
    exit /b 1
)

REM Get the process IDs
for /f "tokens=2" %%i in ('tasklist /fi "imagename eq OpenAdapt.exe" /nh') do (
    set "PID=%%i"
)

REM Verify that the process ID was found
if not defined PID (
    echo Error: Could not find process IDs for %APP_PATH%
    exit /b 1
)

REM Variable to track if any process is still running
set "ALL_PROCESSES_RUNNING=true"

REM Check if the processes are still running
tasklist /fi "PID eq %PID%" 2>nul | find /i "OpenAdapt.exe" >nul
if errorlevel 1 (
    echo Process %PID% is not running
    set "ALL_PROCESSES_RUNNING=false"
)

REM Set the exit code variable based on the processes' status
if "%ALL_PROCESSES_RUNNING%"=="true" (
    set "EXIT_CODE=0"
) else (
    set "EXIT_CODE=1"
)

echo Exit code: %EXIT_CODE%
exit /b %EXIT_CODE%

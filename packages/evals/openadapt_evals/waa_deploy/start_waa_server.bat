@echo off
REM start_waa_server.bat - Start WAA Flask server on Windows boot
REM This script ensures the WAA server starts automatically on every boot

echo [WAA Startup] Starting WAA server...

REM Wait for network to be available
ping -n 5 127.0.0.1 > nul

REM Check if server is already running
netstat -an | find ":5000" | find "LISTENING" > nul
if %errorlevel% == 0 (
    echo [WAA Startup] Server already running on port 5000
    exit /b 0
)

REM Try multiple possible server locations
REM Location 1: OEM server path (official WAA location)
if exist "C:\oem\server\main.py" (
    cd /d C:\oem\server
    start /b python main.py
    echo [WAA Startup] Started from C:\oem\server
    exit /b 0
)

REM Location 2: Network share (Samba)
if exist "\\host.lan\Data\server\main.py" (
    cd /d \\host.lan\Data\server
    start /b python main.py
    echo [WAA Startup] Started from network share
    exit /b 0
)

REM Location 3: Legacy path
if exist "C:\waa\server\main.py" (
    cd /d C:\waa\server
    start /b python main.py
    echo [WAA Startup] Started from C:\waa\server
    exit /b 0
)

REM If none found, try running from network directly
echo [WAA Startup] Trying network server path...
cd /d \\host.lan\Data\server 2>nul
if %errorlevel% == 0 (
    start /b python main.py
    echo [WAA Startup] Started from network path
    exit /b 0
)

echo [WAA Startup] ERROR: WAA server not found in any expected location
echo Checked: C:\oem\server, \\host.lan\Data\server, C:\waa\server
exit /b 1

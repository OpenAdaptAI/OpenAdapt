@echo off
REM ---------------------------------------------------------------------------
REM Start the openadapt-flow in-guest Windows agent in the INTERACTIVE session.
REM
REM This .bat is meant to be launched by the logged-on user (or a logon
REM scheduled task running as that user -- see README.md), so the agent lands
REM in session 1 and can screenshot / drive the real desktop. It must NOT be
REM started from a session-0 SYSTEM context (a Windows service / raw
REM `prlctl exec`) or screenshots and input address a blank desktop.
REM
REM Env:
REM   OAFLOW_AGENT_PY    full path to the guest python.exe (optional)
REM   OAFLOW_AGENT_HOST     bind address (default 127.0.0.1; 0.0.0.0 to expose)
REM   OAFLOW_AGENT_PORT     TCP port     (default 5000)
REM   OAFLOW_AGENT_TOKEN    bearer token (REQUIRED when HOST is 0.0.0.0)
REM   OAFLOW_AGENT_CERTFILE PEM cert; with KEYFILE serves HTTPS (PHI in transit)
REM   OAFLOW_AGENT_KEYFILE  PEM private key matching CERTFILE
REM ---------------------------------------------------------------------------
setlocal

if "%OAFLOW_AGENT_PY%"=="" set "OAFLOW_AGENT_PY=C:\Program Files\Python312-arm64\python.exe"
if "%OAFLOW_AGENT_HOST%"=="" set "OAFLOW_AGENT_HOST=127.0.0.1"
if "%OAFLOW_AGENT_PORT%"=="" set "OAFLOW_AGENT_PORT=5000"

REM %~dp0 is the folder this .bat lives in; server.py sits beside it.
"%OAFLOW_AGENT_PY%" "%~dp0server.py" --host %OAFLOW_AGENT_HOST% --port %OAFLOW_AGENT_PORT%

endlocal

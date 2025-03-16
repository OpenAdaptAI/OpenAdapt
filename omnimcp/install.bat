@echo off
REM OmniMCP installation script for Windows

echo Creating virtual environment...
uv venv

echo Activating virtual environment...
call .venv\Scripts\activate.bat

echo Installing OmniMCP with minimal dependencies...
uv pip install -e .

echo.
echo OmniMCP installed successfully!
echo.
echo To activate the environment in the future:
echo   call .venv\Scripts\activate.bat
echo.
echo To run OmniMCP:
echo   omnimcp cli    # For CLI mode
echo   omnimcp server # For MCP server mode
echo   omnimcp debug  # For debug mode
echo.
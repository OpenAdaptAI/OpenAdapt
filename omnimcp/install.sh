#!/bin/bash

# OmniMCP installation script

# Create virtual environment
echo "Creating virtual environment..."
uv venv

# Activate virtual environment
echo "Activating virtual environment..."
if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "win32" ]]; then
    source .venv/Scripts/activate
else
    source .venv/bin/activate
fi

# Install OmniMCP
echo "Installing OmniMCP with minimal dependencies..."
uv pip install -e .

echo ""
echo "OmniMCP installed successfully!"
echo ""
echo "To activate the environment in the future:"
if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "win32" ]]; then
    echo "  source .venv/Scripts/activate"
else
    echo "  source .venv/bin/activate"
fi
echo ""
echo "To run OmniMCP:"
echo "  omnimcp cli    # For CLI mode"
echo "  omnimcp server # For MCP server mode"
echo "  omnimcp debug  # For debug mode"
echo ""
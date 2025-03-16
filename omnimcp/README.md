# OmniMCP

OmniMCP is a UI automation system that enables Claude to control the computer through the Model Control Protocol (MCP). It combines OmniParser's visual understanding with Claude's natural language capabilities to automate UI interactions.

## Installation

```bash
# Clone the OpenAdapt repository
git clone https://github.com/OpenAdaptAI/OpenAdapt.git
cd OpenAdapt

# Install OmniMCP in development mode (this avoids installing the full OpenAdapt package)
cd omnimcp
python -m pip install -e .
```

This installation method:
1. Only installs the dependencies needed for OmniMCP
2. Sets up Python to find OpenAdapt modules without installing the full package
3. Allows you to run OmniMCP commands directly

## Usage

```bash
# Run CLI mode (direct command input)
omnimcp cli

# Run MCP server (for Claude Desktop)
omnimcp server

# Run in debug mode to visualize screen elements
omnimcp debug

# With additional options
omnimcp cli --use-normalized-coordinates
omnimcp debug --debug-dir=/path/to/debug/folder
```

## Features

- Visual UI analysis with OmniParser
- Natural language understanding with Claude
- Keyboard and mouse control with pynput
- Model Control Protocol integration
- Debug visualizations

## Structure

OmniMCP uses code from the OpenAdapt repository but with a minimal set of dependencies. The key components are:

- `omnimcp/pyproject.toml`: Minimal dependency list
- `omnimcp/setup.py`: Setup script that adds OpenAdapt to the Python path
- Original modules from OpenAdapt:
  - `openadapt/omnimcp.py`: Core functionality
  - `openadapt/run_omnimcp.py`: CLI interface
  - `openadapt/adapters/omniparser.py`: OmniParser integration
  - `openadapt/mcp/`: Model Control Protocol implementation
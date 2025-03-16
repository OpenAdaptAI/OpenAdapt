# OmniMCP

OmniMCP is a UI automation system that enables Claude to control the computer through the Model Control Protocol (MCP). It combines OmniParser's visual understanding with Claude's natural language capabilities to automate UI interactions.

## Installation

### Prerequisites

- Python 3.10 or 3.11
- [uv](https://github.com/astral-sh/uv) - Fast Python package installer and resolver
  ```bash
  # Install uv
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

### Install OmniMCP

```bash
# Clone the OpenAdapt repository
git clone https://github.com/OpenAdaptAI/OpenAdapt.git
cd OpenAdapt/omnimcp

# Run the installation script (creates a virtual environment using uv)
# For Unix/Mac:
./install.sh
# Note: If you get a permission error, run: chmod +x ./install.sh

# For Windows:
install.bat
```

This installation method:
1. Creates an isolated virtual environment using uv
2. Only installs the dependencies needed for OmniMCP
3. Sets up Python to find OpenAdapt modules without installing the full package
4. Allows you to run OmniMCP commands directly without polluting your system Python

## Usage

After installation, activate the virtual environment:

```bash
# For Unix/Mac
source .venv/bin/activate

# For Windows
.venv\Scripts\activate.bat
```

Then run OmniMCP:

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
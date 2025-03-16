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

### Development

For development and testing, you can reset the environment with:

```bash
# Reset the virtual environment and reinstall dependencies
cd /path/to/OpenAdapt/omnimcp
rm -rf .venv && chmod +x install.sh && ./install.sh
```

### Running OmniMCP

```bash
# Run CLI mode (direct command input)
omnimcp cli

# Run MCP server (for Claude Desktop)
omnimcp server

# Run in debug mode to visualize screen elements
omnimcp debug

# Connect to a remote OmniParser server
omnimcp cli --server-url=https://your-omniparser-server.example.com

# Allow running even if OmniParser isn't available (limited functionality)
omnimcp cli --allow-no-parser

# With additional options
omnimcp cli --use-normalized-coordinates
omnimcp debug --debug-dir=/path/to/debug/folder
```

### OmniParser Configuration

OmniMCP requires access to an OmniParser server for analyzing screenshots:

1. **Use a Remote OmniParser Server** (Recommended)
   ```bash
   omnimcp cli --server-url=https://your-omniparser-server.example.com
   ```

2. **Use the Default Local Server**
   - OmniMCP will try to connect to `http://localhost:8000` by default
   - This requires running an OmniParser server locally

By default, OmniMCP will fail if it can't connect to an OmniParser server. Use the `--allow-no-parser` flag to run with limited functionality when no parser is available.

### Future Direction: Anthropic ComputerUse Integration

OmniMCP and Anthropic's [ComputerUse](https://docs.anthropic.com/en/docs/agents-and-tools/computer-use) both enable Claude to control computers, but with different architectural approaches:

#### Key Differences

**Integration Approach:**
- **OmniMCP** uses OmniParser for understanding UI elements
- **ComputerUse** captures screenshots and provides them directly to Claude

**Environment:**
- **OmniMCP** runs directly on the host system with minimal dependencies
- **ComputerUse** operates in a containerized virtual desktop environment

**MCP vs. Anthropic-defined Tools:**
- **OmniMCP** uses the Model Control Protocol (MCP), a structured protocol for AI models to interact with tools
- **ComputerUse** uses Anthropic-defined tools (`computer`, `text_editor`, and `bash`) via Claude's tool use API

#### Potential Integration Paths

Future OmniMCP development could:
1. **Dual Protocol Support**: Support both MCP and Anthropic-defined tools
2. **Container Option**: Provide a containerized deployment similar to ComputerUse
3. **Unified Approach**: Create a bridge between MCP and ComputerUse tools
4. **Feature Parity**: Incorporate ComputerUse capabilities while maintaining MCP compatibility

Both approaches have merits, and integrating aspects of ComputerUse could enhance OmniMCP's capabilities while preserving its lightweight nature and existing MCP integration.

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
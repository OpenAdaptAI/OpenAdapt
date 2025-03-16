"""Run OmniMCP with Model Control Protocol.

This script provides a user-friendly interface to run OmniMCP in different modes.

OmniMCP combines OmniParser (for visual UI understanding) with the Model Control
Protocol (MCP) to enable Claude to control the computer through natural language.

Usage:
------
    # Run CLI mode (direct command input)
    python -m openadapt.run_omnimcp cli
    
    # Run MCP server (for Claude Desktop)
    python -m openadapt.run_omnimcp server
    
    # Run in debug mode to visualize screen elements
    python -m openadapt.run_omnimcp debug
    
    # Run with custom OmniParser server URL
    python -m openadapt.run_omnimcp server --server-url=http://your-server:8000
    
    # Use normalized coordinates (0-1) instead of absolute pixels
    python -m openadapt.run_omnimcp cli --use-normalized-coordinates
    
    # Save debug visualization to specific directory
    python -m openadapt.run_omnimcp debug --debug-dir=/path/to/debug/folder

Components:
----------
1. OmniParser Client (adapters/omniparser.py):
   - Connects to the OmniParser server running on AWS
   - Parses screenshots to identify UI elements

2. OmniMCP Core (omnimcp.py):
   - Manages the visual state of the screen
   - Provides UI interaction methods (click, type, etc.)
   - Implements natural language understanding with Claude

3. MCP Server (mcp/server.py):
   - Implements the Model Control Protocol server
   - Exposes UI automation tools to Claude
"""

import datetime
import os
import sys

import fire

from openadapt.omnimcp import OmniMCP
from openadapt.custom_logger import logger

# TODO: Consider Anthropic ComputerUse integration
# Anthropic's ComputerUse (https://docs.anthropic.com/en/docs/agents-and-tools/computer-use)
# provides an official approach for Claude to control computers. While OmniMCP already
# implements a similar agent loop pattern, future work could:
# 1. Refine our existing agent loop to better align with ComputerUse's approach
# 2. Support Anthropic's containerized environment as a deployment option
# 3. Offer compatibility with Anthropic's official computer control tools


class OmniMCPRunner:
    """OmniMCP runner with different modes of operation."""
    
    def cli(
        self,
        server_url=None,
        claude_api_key=None,
        use_normalized_coordinates=False,
        debug_dir=None,
        allow_no_parser=False
    ):
        """Run OmniMCP in CLI mode.
        
        In CLI mode, you can enter natural language commands directly in the terminal.
        OmniMCP will:
        1. Take a screenshot
        2. Analyze it with OmniParser to identify UI elements
        3. Use Claude to decide what action to take based on your command
        4. Execute the action (click, type, etc.)
        
        This mode is convenient for testing and doesn't require Claude Desktop.
        
        Args:
            server_url: URL of the OmniParser server
            claude_api_key: Claude API key (if not provided, uses value from config.py)
            use_normalized_coordinates: Use normalized (0-1) coordinates instead of pixels
            debug_dir: Directory to save debug visualizations
            allow_no_parser: If True, continue even if OmniParser is not available
        """
        # Create OmniMCP instance
        omnimcp = OmniMCP(
            server_url=server_url,
            claude_api_key=claude_api_key,  # Will use config.ANTHROPIC_API_KEY if None
            use_normalized_coordinates=use_normalized_coordinates,
            allow_no_parser=allow_no_parser
        )
        
        # Handle debug directory if specified
        if debug_dir:
            os.makedirs(debug_dir, exist_ok=True)
            
            # Take initial screenshot and save debug visualization
            logger.info(f"Saving debug visualization to {debug_dir}")
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            debug_path = os.path.join(debug_dir, f"initial_state_{timestamp}.png")
            omnimcp.update_visual_state()
            omnimcp.save_visual_debug(debug_path)
        
        logger.info("Starting OmniMCP in CLI mode")
        logger.info(f"Coordinate mode: {'normalized (0-1)' if use_normalized_coordinates else 'absolute (pixels)'}")
        
        # Run CLI interaction loop
        omnimcp.run_interactive()
    
    def server(
        self,
        server_url=None,
        claude_api_key=None,
        use_normalized_coordinates=False,
        debug_dir=None,
        allow_no_parser=False
    ):
        """Run OmniMCP as an MCP server.
        
        In server mode, OmniMCP provides UI automation tools to Claude through the
        Model Control Protocol. The server exposes tools for:
        1. Getting the current screen state with UI elements
        2. Finding UI elements by description
        3. Clicking on elements or coordinates
        4. Typing text and pressing keys
        
        To use with Claude Desktop:
        1. Configure Claude Desktop to use this server
        2. Ask Claude to perform UI tasks
        
        Args:
            server_url: URL of the OmniParser server
            claude_api_key: Claude API key (if not provided, uses value from config.py)
            use_normalized_coordinates: Use normalized (0-1) coordinates instead of pixels
            debug_dir: Directory to save debug visualizations
            allow_no_parser: If True, continue even if OmniParser is not available
        """
        # Create OmniMCP instance
        omnimcp = OmniMCP(
            server_url=server_url,
            claude_api_key=claude_api_key,  # Will use config.ANTHROPIC_API_KEY if None
            use_normalized_coordinates=use_normalized_coordinates,
            allow_no_parser=allow_no_parser
        )
        
        # Handle debug directory if specified
        if debug_dir:
            os.makedirs(debug_dir, exist_ok=True)
            
            # Take initial screenshot and save debug visualization
            logger.info(f"Saving debug visualization to {debug_dir}")
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            debug_path = os.path.join(debug_dir, f"initial_state_{timestamp}.png")
            omnimcp.update_visual_state()
            omnimcp.save_visual_debug(debug_path)
        
        logger.info("Starting OmniMCP Model Control Protocol server")
        logger.info(f"Coordinate mode: {'normalized (0-1)' if use_normalized_coordinates else 'absolute (pixels)'}")
        
        # Run MCP server
        omnimcp.run_mcp_server()
    
    def debug(
        self,
        server_url=None,
        claude_api_key=None,
        use_normalized_coordinates=False,
        debug_dir=None,
        allow_no_parser=False
    ):
        """Run OmniMCP in debug mode.
        
        Debug mode takes a screenshot, analyzes it with OmniParser, and saves
        a visualization showing the detected UI elements with their descriptions.
        
        This is useful for:
        - Understanding what UI elements OmniParser detects
        - Debugging issues with element detection
        - Fine-tuning OmniParser integration
        
        Args:
            server_url: URL of the OmniParser server
            claude_api_key: Claude API key (if not provided, uses value from config.py)
            use_normalized_coordinates: Use normalized (0-1) coordinates instead of pixels
            debug_dir: Directory to save debug visualizations
            allow_no_parser: If True, continue even if OmniParser is not available
        """
        # Create OmniMCP instance
        omnimcp = OmniMCP(
            server_url=server_url,
            claude_api_key=claude_api_key,  # Will use config.ANTHROPIC_API_KEY if None
            use_normalized_coordinates=use_normalized_coordinates,
            allow_no_parser=allow_no_parser
        )
        
        # Create debug directory if not specified
        if not debug_dir:
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            debug_dir = os.path.join(os.path.expanduser("~"), "omnimcp_debug", f"debug_{timestamp}")
        
        os.makedirs(debug_dir, exist_ok=True)
        logger.info(f"Saving debug visualization to {debug_dir}")
        
        # Generate debug filename
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        debug_path = os.path.join(debug_dir, f"screen_state_{timestamp}.png")
        
        # Update visual state and save debug
        logger.info("Taking screenshot and analyzing with OmniParser...")
        omnimcp.update_visual_state()
        omnimcp.save_visual_debug(debug_path)
        logger.info(f"Saved debug visualization to {debug_path}")
        
        # Print some stats about detected elements
        num_elements = len(omnimcp.visual_state.elements)
        logger.info(f"Detected {num_elements} UI elements")
        
        if num_elements > 0:
            # Show a few example elements
            logger.info("Example elements:")
            for i, element in enumerate(omnimcp.visual_state.elements[:5]):
                content = element.content[:50] + "..." if len(element.content) > 50 else element.content
                logger.info(f"  {i+1}. '{content}' at ({element.x1},{element.y1},{element.x2},{element.y2})")
            
            if num_elements > 5:
                logger.info(f"  ... and {num_elements - 5} more elements")


def main():
    """Main entry point for OmniMCP."""
    fire.Fire(OmniMCPRunner)


if __name__ == "__main__":
    main()
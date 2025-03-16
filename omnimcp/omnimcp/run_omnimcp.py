"""Entry point for OmniMCP CLI.

This module provides a command-line interface for OmniMCP, allowing you to run
it in various modes (CLI, MCP server, debug visualizations).
"""

import datetime
import fire
import os
from loguru import logger

# Setup path to include OpenAdapt modules
from . import pathing
from .omnimcp import OmniMCP
from .config import config


class OmniMCPRunner:
    """OmniMCP runner with different modes of operation."""
    
    def cli(
        self,
        server_url=None,
        claude_api_key=None,
        use_normalized_coordinates=False,
        debug_dir=None,
        allow_no_parser=False,
        auto_deploy_parser=True,
        skip_confirmation=False
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
            auto_deploy_parser: If True, attempt to deploy OmniParser if not available (default: True)
            skip_confirmation: If True, skip user confirmation for OmniParser deployment
        """
        # Create OmniMCP instance
        omnimcp = OmniMCP(
            server_url=server_url,
            claude_api_key=claude_api_key,  # Will use config.ANTHROPIC_API_KEY if None
            use_normalized_coordinates=use_normalized_coordinates,
            allow_no_parser=allow_no_parser,
            auto_deploy_parser=auto_deploy_parser,
            skip_confirmation=skip_confirmation
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
        allow_no_parser=False,
        auto_deploy_parser=True,
        skip_confirmation=False
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
            auto_deploy_parser: If True, attempt to deploy OmniParser if not available (default: True)
            skip_confirmation: If True, skip user confirmation for OmniParser deployment
        """
        # Create OmniMCP instance
        omnimcp = OmniMCP(
            server_url=server_url,
            claude_api_key=claude_api_key,  # Will use config.ANTHROPIC_API_KEY if None
            use_normalized_coordinates=use_normalized_coordinates,
            allow_no_parser=allow_no_parser,
            auto_deploy_parser=auto_deploy_parser,
            skip_confirmation=skip_confirmation
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
        allow_no_parser=False,
        auto_deploy_parser=True,
        skip_confirmation=False
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
            auto_deploy_parser: If True, attempt to deploy OmniParser if not available (default: True)
            skip_confirmation: If True, skip user confirmation for OmniParser deployment
        """
        # Create OmniMCP instance
        omnimcp = OmniMCP(
            server_url=server_url,
            claude_api_key=claude_api_key,  # Will use config.ANTHROPIC_API_KEY if None
            use_normalized_coordinates=use_normalized_coordinates,
            allow_no_parser=allow_no_parser,
            auto_deploy_parser=auto_deploy_parser,
            skip_confirmation=skip_confirmation
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
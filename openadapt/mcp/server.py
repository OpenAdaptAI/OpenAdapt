"""MCP server implementation for OmniMCP.

This module implements a Model Control Protocol server that exposes
UI automation capabilities to Claude through a standardized interface.

Usage:
    # Import and create server instance
    from openadapt.mcp.server import create_omnimcp_server
    from openadapt.omnimcp import OmniMCP

    # Create OmniMCP instance
    omnimcp = OmniMCP()
    
    # Create and run server
    server = create_omnimcp_server(omnimcp)
    server.run()
"""

import datetime
import io
import json
import os
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from openadapt.custom_logger import logger


def create_debug_directory() -> str:
    """Create a timestamped directory for debug outputs.
    
    Returns:
        str: Path to debug directory
    """
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    debug_dir = os.path.join(
        os.path.expanduser("~"), 
        "omnimcp_debug", 
        f"session_{timestamp}"
    )
    os.makedirs(debug_dir, exist_ok=True)
    logger.info(f"Created debug directory: {debug_dir}")
    return debug_dir


def create_omnimcp_server(omnimcp_instance) -> FastMCP:
    """Create an MCP server for the given OmniMCP instance.
    
    Args:
        omnimcp_instance: An instance of the OmniMCP class
        
    Returns:
        FastMCP: The MCP server instance
    """
    # Initialize FastMCP server
    server = FastMCP("omnimcp")
    
    # Create debug directory
    debug_dir = create_debug_directory()
    
    @server.tool()
    async def get_screen_state() -> Dict[str, Any]:
        """Get the current state of the screen with UI elements.
        
        Returns a structured representation of all UI elements detected on screen,
        including their positions, descriptions, and other metadata.
        """
        # Update visual state
        omnimcp_instance.update_visual_state()
        
        # Save screenshot with timestamp for debugging
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        debug_path = os.path.join(debug_dir, f"screen_state_{timestamp}.png")
        omnimcp_instance.save_visual_debug(debug_path)
        
        # Get structured description and parse into JSON
        mcp_description = omnimcp_instance.visual_state.to_mcp_description(
            omnimcp_instance.use_normalized_coordinates
        )
        
        return json.loads(mcp_description)
    
    @server.tool()
    async def find_ui_element(descriptor: str, partial_match: bool = True) -> Dict[str, Any]:
        """Find a UI element by its descriptor.
        
        Args:
            descriptor: Descriptive text to search for in element content
            partial_match: Whether to allow partial matching
            
        Returns:
            Information about the matched element or error if not found
        """
        # Update visual state
        omnimcp_instance.update_visual_state()
        
        # Find element
        element = omnimcp_instance.visual_state.find_element_by_content(
            descriptor, 
            partial_match
        )
        
        if not element:
            return {
                "found": False,
                "error": f"No UI element matching '{descriptor}' was found",
                "possible_elements": [
                    el.content for el in omnimcp_instance.visual_state.elements[:10]
                ]
            }
        
        # Return element details
        return {
            "found": True,
            "content": element.content,
            "type": element.type,
            "confidence": element.confidence,
            "bounds": {
                "x1": element.x1,
                "y1": element.y1,
                "x2": element.x2,
                "y2": element.y2,
                "width": element.width,
                "height": element.height
            },
            "center": {
                "x": element.center_x,
                "y": element.center_y
            },
            "normalized": {
                "bounds": element.bbox,
                "center": {
                    "x": element.normalized_center_x, 
                    "y": element.normalized_center_y
                }
            }
        }
    
    @server.tool()
    async def click_element(
        descriptor: str, 
        button: str = "left", 
        partial_match: bool = True
    ) -> Dict[str, Any]:
        """Click on a UI element by its descriptor.
        
        Args:
            descriptor: Descriptive text to identify the element
            button: Mouse button to use (left, right, middle)
            partial_match: Whether to allow partial matching
            
        Returns:
            Result of the click operation
        """
        # Find and click the element
        success = omnimcp_instance.click_element(descriptor, button, partial_match)
        
        if success:
            # Save debug screenshot after clicking
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            debug_path = os.path.join(debug_dir, f"click_{descriptor}_{timestamp}.png")
            omnimcp_instance.save_visual_debug(debug_path)
            
            return {
                "success": True,
                "message": f"Successfully clicked element: {descriptor}"
            }
        else:
            return {
                "success": False,
                "message": f"Failed to find element: {descriptor}",
                "possible_elements": [
                    el.content for el in omnimcp_instance.visual_state.elements[:10]
                ]
            }
    
    @server.tool()
    async def click_coordinates(
        x: float, 
        y: float, 
        button: str = "left"
    ) -> Dict[str, Any]:
        """Click at specific coordinates on the screen.
        
        Args:
            x: X coordinate (absolute or normalized based on settings)
            y: Y coordinate (absolute or normalized based on settings)
            button: Mouse button to use (left, right, middle)
            
        Returns:
            Result of the click operation
        """
        try:
            # Perform click
            omnimcp_instance.click(x, y, button)
            
            # Save debug screenshot after clicking
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            debug_path = os.path.join(debug_dir, f"click_coords_{x}_{y}_{timestamp}.png")
            omnimcp_instance.save_visual_debug(debug_path)
            
            # Determine coordinate format for message
            format_type = "normalized" if omnimcp_instance.use_normalized_coordinates else "absolute"
            
            return {
                "success": True,
                "message": f"Successfully clicked at {format_type} coordinates ({x}, {y})"
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"Failed to click: {str(e)}"
            }
    
    @server.tool()
    async def type_text(text: str) -> Dict[str, Any]:
        """Type text using the keyboard.
        
        Args:
            text: Text to type
            
        Returns:
            Result of the typing operation
        """
        try:
            omnimcp_instance.type_text(text)
            return {
                "success": True,
                "message": f"Successfully typed: {text}"
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"Failed to type text: {str(e)}"
            }
    
    @server.tool()
    async def press_key(key: str) -> Dict[str, Any]:
        """Press a single key on the keyboard.
        
        Args:
            key: Key to press (e.g., enter, tab, escape)
            
        Returns:
            Result of the key press operation
        """
        try:
            omnimcp_instance.press_key(key)
            return {
                "success": True,
                "message": f"Successfully pressed key: {key}"
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"Failed to press key: {str(e)}"
            }
    
    @server.tool()
    async def list_ui_elements() -> List[Dict[str, Any]]:
        """List all detected UI elements on the current screen.
        
        Returns:
            List of all UI elements with basic information
        """
        # Update visual state
        omnimcp_instance.update_visual_state()
        
        # Extract basic info for each element
        elements = []
        for element in omnimcp_instance.visual_state.elements:
            elements.append({
                "content": element.content,
                "type": element.type,
                "confidence": element.confidence,
                "center": {
                    "x": element.center_x,
                    "y": element.center_y
                },
                "dimensions": {
                    "width": element.width,
                    "height": element.height
                }
            })
        
        return elements
    
    @server.tool()
    async def save_debug_screenshot(description: str = "debug") -> Dict[str, Any]:
        """Save a debug screenshot with an optional description.
        
        The description is used to name the screenshot file, making it easier to identify
        the purpose of the screenshot (e.g., "before_clicking_submit_button").
        
        Args:
            description: Description to include in the filename
            
        Returns:
            Result of the save operation
        """
        try:
            # Create sanitized description for filename
            safe_description = "".join(c if c.isalnum() else "_" for c in description)
            
            # Generate timestamped filename
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = os.path.join(
                debug_dir, 
                f"{safe_description}_{timestamp}.png"
            )
            
            # Save the debug visualization
            omnimcp_instance.save_visual_debug(output_path)
            
            return {
                "success": True,
                "message": f"Debug screenshot saved to {output_path}",
                "path": output_path
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"Failed to save debug screenshot: {str(e)}"
            }
    
    return server
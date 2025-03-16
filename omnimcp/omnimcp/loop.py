"""Agentic sampling loop for Computer Use with OmniMCP.

This module implements the agent loop for Computer Use integration,
handling the interaction between Claude and OmniMCP's UI automation tools.

Usage:
    from omnimcp.loop import computer_use_loop
    from omnimcp.omnimcp import OmniMCP
    
    omnimcp = OmniMCP()
    asyncio.run(
        computer_use_loop(
            model="claude-3-sonnet-20240229",
            system_prompt=system_prompt,
            messages=messages,
            output_callback=output_callback,
            tool_output_callback=tool_output_callback,
            api_key=api_key,
            omnimcp_instance=omnimcp,
        )
    )
"""

import asyncio
import base64
import io
import json
import time
from typing import Any, Callable, Dict, List, Optional, cast

from anthropic import Anthropic
from anthropic.types.beta import (
    BetaContentBlockParam,
    BetaMessage,
    BetaMessageParam,
    BetaTextBlockParam,
    BetaToolResultBlockParam,
    BetaToolUseBlockParam,
)
from loguru import logger


class ToolResult:
    """Result from a tool execution."""
    
    def __init__(
        self,
        output: str = "",
        base64_image: str = "",
        error: str = "",
        system: str = ""
    ):
        """Initialize tool result.
        
        Args:
            output: Text output from the tool
            base64_image: Base64-encoded image output
            error: Error message if tool execution failed
            system: System message to include with the result
        """
        self.output = output
        self.base64_image = base64_image
        self.error = error
        self.system = system


class ComputerUseTools:
    """Implementation of Computer Use tools using OmniMCP."""
    
    def __init__(self, omnimcp_instance):
        """Initialize with an OmniMCP instance.
        
        Args:
            omnimcp_instance: Instance of OmniMCP
        """
        self.omnimcp = omnimcp_instance
    
    def get_screen_state(self) -> ToolResult:
        """Get the current state of the screen with UI elements.
        
        Returns:
            ToolResult: Structured representation of UI elements and a screenshot
        """
        try:
            # Update visual state
            self.omnimcp.update_visual_state()
            
            # Get structured description
            description = self.omnimcp.visual_state.to_mcp_description(
                self.omnimcp.use_normalized_coordinates
            )
            
            # Get screenshot as base64
            img_byte_arr = io.BytesIO()
            screenshot = self.omnimcp.visual_state.screenshot
            screenshot.save(img_byte_arr, format='PNG')
            base64_image = base64.b64encode(img_byte_arr.getvalue()).decode('utf-8')
            
            return ToolResult(
                output=description,
                base64_image=base64_image
            )
        except Exception as e:
            logger.error(f"Error getting screen state: {e}")
            return ToolResult(error=f"Failed to get screen state: {str(e)}")
    
    def click_element(self, descriptor: str, button: str = "left") -> ToolResult:
        """Click on a UI element by its descriptor.
        
        Args:
            descriptor: Descriptive text to identify the element
            button: Mouse button to use (left, right, middle)
            
        Returns:
            ToolResult: Result of the click operation
        """
        try:
            # Click the element
            success = self.omnimcp.click_element(descriptor, button, True)
            
            if success:
                # Get updated screenshot as base64
                self.omnimcp.update_visual_state()
                img_byte_arr = io.BytesIO()
                screenshot = self.omnimcp.visual_state.screenshot
                screenshot.save(img_byte_arr, format='PNG')
                base64_image = base64.b64encode(img_byte_arr.getvalue()).decode('utf-8')
                
                return ToolResult(
                    output=f"Successfully clicked element: {descriptor}",
                    base64_image=base64_image
                )
            else:
                possible_elements = [
                    el.content for el in self.omnimcp.visual_state.elements[:10]
                ]
                return ToolResult(
                    error=f"Failed to find element: '{descriptor}'",
                    system=f"Similar elements found: {', '.join(possible_elements)}"
                )
        except Exception as e:
            logger.error(f"Error clicking element: {e}")
            return ToolResult(error=f"Failed to click element: {str(e)}")
    
    def click_coordinates(self, x: float, y: float, button: str = "left") -> ToolResult:
        """Click at specific coordinates on the screen.
        
        Args:
            x: X coordinate (absolute or normalized based on settings)
            y: Y coordinate (absolute or normalized based on settings)
            button: Mouse button to use (left, right, middle)
            
        Returns:
            ToolResult: Result of the click operation
        """
        try:
            # Perform click
            self.omnimcp.click(x, y, button)
            
            # Get updated screenshot as base64
            self.omnimcp.update_visual_state()
            img_byte_arr = io.BytesIO()
            screenshot = self.omnimcp.visual_state.screenshot
            screenshot.save(img_byte_arr, format='PNG')
            base64_image = base64.b64encode(img_byte_arr.getvalue()).decode('utf-8')
            
            # Determine coordinate format for message
            format_type = "normalized" if self.omnimcp.use_normalized_coordinates else "absolute"
            
            return ToolResult(
                output=f"Successfully clicked at {format_type} coordinates ({x}, {y})",
                base64_image=base64_image
            )
        except Exception as e:
            logger.error(f"Error clicking coordinates: {e}")
            return ToolResult(error=f"Failed to click: {str(e)}")
    
    def type_text(self, text: str) -> ToolResult:
        """Type text using the keyboard.
        
        Args:
            text: Text to type
            
        Returns:
            ToolResult: Result of the typing operation
        """
        try:
            self.omnimcp.type_text(text)
            
            # Get updated screenshot as base64
            self.omnimcp.update_visual_state()
            img_byte_arr = io.BytesIO()
            screenshot = self.omnimcp.visual_state.screenshot
            screenshot.save(img_byte_arr, format='PNG')
            base64_image = base64.b64encode(img_byte_arr.getvalue()).decode('utf-8')
            
            return ToolResult(
                output=f"Successfully typed: {text}",
                base64_image=base64_image
            )
        except Exception as e:
            logger.error(f"Error typing text: {e}")
            return ToolResult(error=f"Failed to type text: {str(e)}")
    
    def press_key(self, key: str) -> ToolResult:
        """Press a single key on the keyboard.
        
        Args:
            key: Key to press (e.g., enter, tab, escape)
            
        Returns:
            ToolResult: Result of the key press operation
        """
        try:
            self.omnimcp.press_key(key)
            
            # Get updated screenshot as base64
            self.omnimcp.update_visual_state()
            img_byte_arr = io.BytesIO()
            screenshot = self.omnimcp.visual_state.screenshot
            screenshot.save(img_byte_arr, format='PNG')
            base64_image = base64.b64encode(img_byte_arr.getvalue()).decode('utf-8')
            
            return ToolResult(
                output=f"Successfully pressed key: {key}",
                base64_image=base64_image
            )
        except Exception as e:
            logger.error(f"Error pressing key: {e}")
            return ToolResult(error=f"Failed to press key: {str(e)}")
    
    def scroll(self, amount: int, direction: str = "vertical") -> ToolResult:
        """Scroll the screen.
        
        Args:
            amount: Amount to scroll (positive or negative)
            direction: "vertical" or "horizontal"
            
        Returns:
            ToolResult: Result of the scroll operation
        """
        try:
            vertical = direction.lower() == "vertical"
            self.omnimcp.scroll(amount, vertical)
            
            # Get updated screenshot as base64
            self.omnimcp.update_visual_state()
            img_byte_arr = io.BytesIO()
            screenshot = self.omnimcp.visual_state.screenshot
            screenshot.save(img_byte_arr, format='PNG')
            base64_image = base64.b64encode(img_byte_arr.getvalue()).decode('utf-8')
            
            dir_word = "vertically" if vertical else "horizontally"
            direction_word = ""
            if vertical:
                direction_word = "down" if amount < 0 else "up"
            else:
                direction_word = "right" if amount < 0 else "left"
                
            return ToolResult(
                output=f"Successfully scrolled {dir_word} {direction_word} by {abs(amount)}",
                base64_image=base64_image
            )
        except Exception as e:
            logger.error(f"Error scrolling: {e}")
            return ToolResult(error=f"Failed to scroll: {str(e)}")
    
    def run(self, name: str, tool_input: Dict[str, Any]) -> ToolResult:
        """Run a tool by name with the specified input.
        
        Args:
            name: Tool name
            tool_input: Tool input parameters
            
        Returns:
            ToolResult: Tool execution result
        """
        # Map tool names to methods
        tool_map = {
            "get_screen_state": self.get_screen_state,
            "click_element": self.click_element,
            "click_coordinates": self.click_coordinates,
            "type_text": self.type_text,
            "press_key": self.press_key,
            "scroll": self.scroll,
        }
        
        if name not in tool_map:
            return ToolResult(error=f"Unknown tool: {name}")
        
        try:
            tool_func = tool_map[name]
            result = tool_func(**tool_input)
            return result
        except Exception as e:
            logger.error(f"Error running tool {name}: {e}")
            return ToolResult(error=f"Error running tool {name}: {str(e)}")
    
    def to_params(self) -> List[Dict[str, Any]]:
        """Return tool parameters for Anthropic API.
        
        Returns:
            List[Dict[str, Any]]: Tool descriptions
        """
        return [
            {
                "name": "get_screen_state",
                "description": "Get the current state of the screen with UI elements",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            {
                "name": "click_element",
                "description": "Click on a UI element by its text content",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "descriptor": {
                            "type": "string",
                            "description": "Text content of the element to click"
                        },
                        "button": {
                            "type": "string",
                            "enum": ["left", "right", "middle"],
                            "default": "left",
                            "description": "Mouse button to use"
                        }
                    },
                    "required": ["descriptor"]
                }
            },
            {
                "name": "click_coordinates",
                "description": "Click at specific coordinates on the screen",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "x": {
                            "type": "number",
                            "description": "X coordinate (absolute or normalized based on settings)"
                        },
                        "y": {
                            "type": "number",
                            "description": "Y coordinate (absolute or normalized based on settings)"
                        },
                        "button": {
                            "type": "string",
                            "enum": ["left", "right", "middle"],
                            "default": "left",
                            "description": "Mouse button to use"
                        }
                    },
                    "required": ["x", "y"]
                }
            },
            {
                "name": "type_text",
                "description": "Type text using the keyboard",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "Text to type"
                        }
                    },
                    "required": ["text"]
                }
            },
            {
                "name": "press_key",
                "description": "Press a single key on the keyboard",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "key": {
                            "type": "string",
                            "description": "Key to press (e.g., enter, tab, escape)"
                        }
                    },
                    "required": ["key"]
                }
            },
            {
                "name": "scroll",
                "description": "Scroll the screen",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "amount": {
                            "type": "integer",
                            "description": "Amount to scroll (positive for up/left, negative for down/right)"
                        },
                        "direction": {
                            "type": "string",
                            "enum": ["vertical", "horizontal"],
                            "default": "vertical",
                            "description": "Direction to scroll"
                        }
                    },
                    "required": ["amount"]
                }
            }
        ]


async def computer_use_loop(
    *,
    model: str,
    system_prompt: str,
    messages: List[BetaMessageParam],
    output_callback: Callable[[BetaContentBlockParam], None],
    tool_output_callback: Callable[[ToolResult, str], None],
    api_key: str,
    omnimcp_instance,
    max_tokens: int = 4096,
):
    """Agent loop for Computer Use with Claude.
    
    Args:
        model: Claude model to use
        system_prompt: System prompt
        messages: Initial messages
        output_callback: Callback for Claude outputs
        tool_output_callback: Callback for tool outputs
        api_key: Anthropic API key
        omnimcp_instance: OmniMCP instance
        max_tokens: Maximum tokens in Claude's response
    """
    # Create tool collection
    tools = ComputerUseTools(omnimcp_instance)
    
    # Set up client
    client = Anthropic(api_key=api_key)
    
    # Setup system message
    system = BetaTextBlockParam(
        type="text",
        text=system_prompt,
    )
    
    while True:
        # Call the Claude API
        try:
            logger.info(f"Calling Claude API with model {model}...")
            start_time = time.time()
            
            response = client.beta.messages.create(
                max_tokens=max_tokens,
                messages=messages,
                model=model,
                system=[system],
                tools=tools.to_params(),
            )
            
            end_time = time.time()
            logger.info(f"Claude API call completed in {end_time - start_time:.2f}s")
            
        except Exception as e:
            logger.error(f"Error calling Claude API: {e}")
            return messages
        
        # Process response
        response_params = response_to_params(response)
        messages.append(
            {
                "role": "assistant",
                "content": response_params,
            }
        )
        
        tool_result_content: List[BetaToolResultBlockParam] = []
        for content_block in response_params:
            # Send content to callback
            output_callback(content_block)
            
            # Process tool use blocks
            if content_block["type"] == "tool_use":
                # Run the tool
                result = tools.run(
                    name=content_block["name"],
                    tool_input=cast(Dict[str, Any], content_block["input"]),
                )
                
                # Create tool result content
                tool_result_content.append(
                    make_tool_result(result, content_block["id"])
                )
                
                # Send result to callback
                tool_output_callback(result, content_block["id"])
        
        # If no tools were used, we're done
        if not tool_result_content:
            logger.info("No tools used, ending conversation")
            return messages
        
        # Add tool results to messages
        messages.append({"content": tool_result_content, "role": "user"})


def response_to_params(
    response: BetaMessage,
) -> List[BetaContentBlockParam]:
    """Convert Claude response to parameters.
    
    Args:
        response: Claude response
        
    Returns:
        List[BetaContentBlockParam]: Content blocks
    """
    res: List[BetaContentBlockParam] = []
    for block in response.content:
        if block.type == "text":
            if block.text:
                res.append(BetaTextBlockParam(type="text", text=block.text))
        else:
            # Handle tool use blocks
            res.append(cast(BetaToolUseBlockParam, block.model_dump()))
    return res


def make_tool_result(
    result: ToolResult, tool_use_id: str
) -> BetaToolResultBlockParam:
    """Convert a ToolResult to an API ToolResultBlockParam.
    
    Args:
        result: Tool result
        tool_use_id: ID of the tool use
        
    Returns:
        BetaToolResultBlockParam: Tool result block
    """
    tool_result_content = []
    is_error = False
    
    if result.error:
        is_error = True
        error_text = result.error
        if result.system:
            error_text = f"<system>{result.system}</system>\n{error_text}"
        tool_result_content.append({"type": "text", "text": error_text})
    else:
        if result.output:
            output_text = result.output
            if result.system:
                output_text = f"<system>{result.system}</system>\n{output_text}"
            tool_result_content.append({"type": "text", "text": output_text})
        
        if result.base64_image:
            tool_result_content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": result.base64_image,
                    },
                }
            )
    
    return {
        "type": "tool_result",
        "content": tool_result_content,
        "tool_use_id": tool_use_id,
        "is_error": is_error,
    }
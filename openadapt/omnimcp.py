"""OmniMCP: Model Context Protocol implementation with OmniParser.

This module enables Claude to understand screen content via OmniParser and
take actions through keyboard and mouse primitives based on natural language requests.

Usage:
    # Basic usage with MCP server
    from openadapt.omnimcp import OmniMCP
    from openadapt.mcp.server import create_omnimcp_server
    
    # Create OmniMCP instance
    omnimcp = OmniMCP()
    
    # Create and run MCP server
    server = create_omnimcp_server(omnimcp)
    server.run()
    
    # Alternatively, run interactively (no MCP)
    omnimcp = OmniMCP()
    omnimcp.run_interactive()
"""

import asyncio
import base64
import datetime
import io
import json
import os
import time
from typing import Dict, List, Any, Optional, Tuple, Union, Callable

from PIL import Image, ImageDraw
import fire
from pynput import keyboard, mouse

from openadapt import utils
from openadapt.adapters.omniparser import OmniParserProvider
from openadapt.config import config
from openadapt.custom_logger import logger
from openadapt.drivers import anthropic


class ScreenElement:
    """Represents a UI element on the screen with bounding box and description."""
    
    def __init__(self, element_data: Dict[str, Any]):
        """Initialize from OmniParser element data.
        
        Args:
            element_data: Element data from OmniParser
        """
        self.content = element_data.get("content", "")
        self.bbox = element_data.get("bbox", [0, 0, 0, 0])  # Normalized coordinates
        self.confidence = element_data.get("confidence", 0.0)
        self.type = element_data.get("type", "unknown")
        self.screen_width = 0
        self.screen_height = 0
    
    def set_screen_dimensions(self, width: int, height: int):
        """Set screen dimensions for coordinate calculations.
        
        Args:
            width: Screen width in pixels
            height: Screen height in pixels
        """
        self.screen_width = width
        self.screen_height = height
    
    @property
    def x1(self) -> int:
        """Get left coordinate in pixels."""
        return int(self.bbox[0] * self.screen_width)
    
    @property
    def y1(self) -> int:
        """Get top coordinate in pixels."""
        return int(self.bbox[1] * self.screen_height)
    
    @property
    def x2(self) -> int:
        """Get right coordinate in pixels."""
        return int(self.bbox[2] * self.screen_width)
    
    @property
    def y2(self) -> int:
        """Get bottom coordinate in pixels."""
        return int(self.bbox[3] * self.screen_height)
    
    @property
    def center_x(self) -> int:
        """Get center x coordinate in pixels."""
        return (self.x1 + self.x2) // 2
    
    @property
    def center_y(self) -> int:
        """Get center y coordinate in pixels."""
        return (self.y1 + self.y2) // 2
    
    @property
    def width(self) -> int:
        """Get width in pixels."""
        return self.x2 - self.x1
    
    @property
    def height(self) -> int:
        """Get height in pixels."""
        return self.y2 - self.y1
    
    @property
    def normalized_center_x(self) -> float:
        """Get normalized center x coordinate (0-1)."""
        if self.screen_width == 0:
            return 0.5
        return (self.x1 + self.x2) / (2 * self.screen_width)
    
    @property
    def normalized_center_y(self) -> float:
        """Get normalized center y coordinate (0-1)."""
        if self.screen_height == 0:
            return 0.5
        return (self.y1 + self.y2) / (2 * self.screen_height)
    
    def __str__(self) -> str:
        """String representation with content and coordinates."""
        return f"{self.content} at ({self.x1},{self.y1},{self.x2},{self.y2})"


class VisualState:
    """Represents the current visual state of the screen with UI elements."""
    
    def __init__(self):
        """Initialize empty visual state."""
        self.elements: List[ScreenElement] = []
        self.screenshot: Optional[Image.Image] = None
        self.timestamp: float = time.time()
    
    def update_from_omniparser(self, omniparser_result: Dict[str, Any], screenshot: Image.Image):
        """Update visual state from OmniParser result.
        
        Args:
            omniparser_result: Result from OmniParser
            screenshot: Screenshot image
        """
        self.screenshot = screenshot
        self.timestamp = time.time()
        
        # Extract parsed content
        parsed_content = omniparser_result.get("parsed_content_list", [])
        
        # Create screen elements
        self.elements = []
        for content in parsed_content:
            element = ScreenElement(content)
            element.set_screen_dimensions(screenshot.width, screenshot.height)
            self.elements.append(element)
    
    def find_element_by_content(self, content: str, partial_match: bool = True) -> Optional[ScreenElement]:
        """Find element by content text.
        
        Args:
            content: Text to search for
            partial_match: If True, match substrings
            
        Returns:
            ScreenElement if found, None otherwise
        """
        for element in self.elements:
            if partial_match and content.lower() in element.content.lower():
                return element
            elif element.content.lower() == content.lower():
                return element
        return None
    
    def find_element_by_position(self, x: int, y: int) -> Optional[ScreenElement]:
        """Find element at position.
        
        Args:
            x: X coordinate
            y: Y coordinate
            
        Returns:
            ScreenElement if found, None otherwise
        """
        for element in self.elements:
            if element.x1 <= x <= element.x2 and element.y1 <= y <= element.y2:
                return element
        return None
    
    def to_mcp_description(self, use_normalized_coordinates: bool = False) -> str:
        """Convert visual state to MCP description format.
        
        Args:
            use_normalized_coordinates: If True, use normalized (0-1) coordinates
        
        Returns:
            str: JSON string with structured description
        """
        ui_elements = []
        for element in self.elements:
            if use_normalized_coordinates:
                ui_elements.append({
                    "type": element.type,
                    "text": element.content,
                    "bounds": {
                        "x": element.bbox[0],
                        "y": element.bbox[1],
                        "width": element.bbox[2] - element.bbox[0],
                        "height": element.bbox[3] - element.bbox[1]
                    },
                    "center": {
                        "x": element.normalized_center_x,
                        "y": element.normalized_center_y
                    },
                    "confidence": element.confidence
                })
            else:
                ui_elements.append({
                    "type": element.type,
                    "text": element.content,
                    "bounds": {
                        "x": element.x1,
                        "y": element.y1,
                        "width": element.width,
                        "height": element.height
                    },
                    "center": {
                        "x": element.center_x,
                        "y": element.center_y
                    },
                    "confidence": element.confidence
                })
        
        visual_state = {
            "ui_elements": ui_elements,
            "screenshot_timestamp": self.timestamp,
            "screen_width": self.screenshot.width if self.screenshot else 0,
            "screen_height": self.screenshot.height if self.screenshot else 0,
            "element_count": len(self.elements),
            "coordinates": "normalized" if use_normalized_coordinates else "absolute"
        }
        
        return json.dumps(visual_state, indent=2)
    
    def visualize(self) -> Image.Image:
        """Create visualization of elements on screenshot.
        
        Returns:
            Image: Annotated screenshot with bounding boxes
        """
        if not self.screenshot:
            return Image.new('RGB', (800, 600), color='white')
        
        # Create a copy of the screenshot
        img = self.screenshot.copy()
        draw = ImageDraw.Draw(img)
        
        # Draw bounding boxes
        for i, element in enumerate(self.elements):
            # Generate a different color for each element based on its index
            r = (i * 50) % 255
            g = (i * 100) % 255
            b = (i * 150) % 255
            color = (r, g, b)
            
            # Draw rectangle
            draw.rectangle(
                [(element.x1, element.y1), (element.x2, element.y2)], 
                outline=color, 
                width=2
            )
            
            # Draw element identifier
            identifier = f"{i}: {element.content[:15]}"
            
            # Create text background
            text_bg_padding = 2
            text_position = (element.x1, element.y1 - 20)
            draw.rectangle(
                [
                    (text_position[0] - text_bg_padding, text_position[1] - text_bg_padding),
                    (text_position[0] + len(identifier) * 7, text_position[1] + 15)
                ],
                fill=(255, 255, 255, 180)
            )
            
            # Draw text
            draw.text(
                text_position,
                identifier,
                fill=color
            )
        
        return img


class OmniMCP:
    """Main OmniMCP class implementing Model Context Protocol."""
    
    def __init__(
        self,
        server_url: Optional[str] = None,
        claude_api_key: Optional[str] = None,
        use_normalized_coordinates: bool = False
    ):
        """Initialize OmniMCP.
        
        Args:
            server_url: URL of OmniParser server
            claude_api_key: API key for Claude (overrides config)
            use_normalized_coordinates: If True, use normalized (0-1) coordinates
        """
        self.omniparser = OmniParserProvider(server_url)
        self.visual_state = VisualState()
        self.claude_api_key = claude_api_key or config.ANTHROPIC_API_KEY
        self.use_normalized_coordinates = use_normalized_coordinates
        
        # Initialize controllers for keyboard and mouse
        self.keyboard_controller = keyboard.Controller()
        self.mouse_controller = mouse.Controller()
        
        # Get screen dimensions from a screenshot
        initial_screenshot = utils.take_screenshot()
        self.screen_width, self.screen_height = initial_screenshot.size
        logger.info(f"Screen dimensions: {self.screen_width}x{self.screen_height}")
        
        # Ensure OmniParser is running
        if not self.omniparser.is_available():
            logger.info("OmniParser not available, attempting to deploy...")
            self.omniparser.deploy()
    
    def update_visual_state(self) -> VisualState:
        """Take screenshot and update visual state using OmniParser.
        
        Returns:
            VisualState: Updated visual state
        """
        # Take screenshot
        screenshot = utils.take_screenshot()
        
        # Convert to bytes
        img_byte_arr = io.BytesIO()
        screenshot.save(img_byte_arr, format='PNG')
        img_bytes = img_byte_arr.getvalue()
        
        # Parse with OmniParser
        result = self.omniparser.parse_screenshot(img_bytes)
        
        # Update visual state
        self.visual_state.update_from_omniparser(result, screenshot)
        
        return self.visual_state
    
    def click(self, x: Union[int, float], y: Union[int, float], button: str = "left") -> None:
        """Click at specific coordinates.
        
        Args:
            x: X coordinate (absolute or normalized based on configuration)
            y: Y coordinate (absolute or normalized based on configuration)
            button: Mouse button ('left', 'right', 'middle')
        """
        if self.use_normalized_coordinates:
            # Convert normalized coordinates to absolute
            x_abs = int(x * self.screen_width)
            y_abs = int(y * self.screen_height)
            logger.info(f"Clicking at normalized ({x}, {y}) -> absolute ({x_abs}, {y_abs}) with {button} button")
            x, y = x_abs, y_abs
        else:
            logger.info(f"Clicking at ({x}, {y}) with {button} button")
        
        # Map button string to pynput button object
        button_obj = getattr(mouse.Button, button)
        
        # Move to position and click
        self.mouse_controller.position = (x, y)
        self.mouse_controller.click(button_obj, 1)
    
    def move_mouse(self, x: Union[int, float], y: Union[int, float]) -> None:
        """Move mouse to coordinates without clicking.
        
        Args:
            x: X coordinate (absolute or normalized)
            y: Y coordinate (absolute or normalized)
        """
        if self.use_normalized_coordinates:
            # Convert normalized coordinates to absolute
            x_abs = int(x * self.screen_width)
            y_abs = int(y * self.screen_height)
            logger.info(f"Moving mouse to normalized ({x}, {y}) -> absolute ({x_abs}, {y_abs})")
            x, y = x_abs, y_abs
        else:
            logger.info(f"Moving mouse to ({x}, {y})")
        
        # Move to position
        self.mouse_controller.position = (x, y)
    
    def drag_mouse(
        self, 
        start_x: Union[int, float], 
        start_y: Union[int, float],
        end_x: Union[int, float],
        end_y: Union[int, float],
        button: str = "left",
        duration: float = 0.5
    ) -> None:
        """Drag mouse from start to end coordinates.
        
        Args:
            start_x: Starting X coordinate
            start_y: Starting Y coordinate
            end_x: Ending X coordinate
            end_y: Ending Y coordinate
            button: Mouse button to use for dragging
            duration: Duration of drag in seconds
        """
        if self.use_normalized_coordinates:
            # Convert normalized coordinates to absolute
            start_x_abs = int(start_x * self.screen_width)
            start_y_abs = int(start_y * self.screen_height)
            end_x_abs = int(end_x * self.screen_width)
            end_y_abs = int(end_y * self.screen_height)
            
            logger.info(
                f"Dragging from normalized ({start_x}, {start_y}) -> "
                f"({end_x}, {end_y}) over {duration}s"
            )
            
            start_x, start_y = start_x_abs, start_y_abs
            end_x, end_y = end_x_abs, end_y_abs
        else:
            logger.info(
                f"Dragging from ({start_x}, {start_y}) -> "
                f"({end_x}, {end_y}) over {duration}s"
            )
        
        # Map button string to pynput button object
        button_obj = getattr(mouse.Button, button)
        
        # Move to start position
        self.mouse_controller.position = (start_x, start_y)
        
        # Press button
        self.mouse_controller.press(button_obj)
        
        # Calculate steps for smooth movement
        steps = max(int(duration * 60), 10)  # Aim for 60 steps per second, minimum 10 steps
        sleep_time = duration / steps
        
        # Perform drag in steps
        for i in range(1, steps + 1):
            progress = i / steps
            current_x = start_x + (end_x - start_x) * progress
            current_y = start_y + (end_y - start_y) * progress
            self.mouse_controller.position = (current_x, current_y)
            time.sleep(sleep_time)
        
        # Release button at final position
        self.mouse_controller.position = (end_x, end_y)
        self.mouse_controller.release(button_obj)
    
    def scroll(self, amount: int, vertical: bool = True) -> None:
        """Scroll the screen.
        
        Args:
            amount: Amount to scroll (positive for up/left, negative for down/right)
            vertical: If True, scroll vertically, otherwise horizontally
        """
        # pynput's scroll logic: positive values scroll up, negative scroll down
        # This is the opposite of pyautogui's convention
        scroll_amount = amount
        
        if vertical:
            self.mouse_controller.scroll(0, scroll_amount)
            direction = "up" if amount > 0 else "down"
            logger.info(f"Scrolled {direction} by {abs(amount)}")
        else:
            self.mouse_controller.scroll(scroll_amount, 0)
            direction = "left" if amount > 0 else "right"
            logger.info(f"Scrolled {direction} by {abs(amount)}")
    
    def scroll_at(
        self,
        x: Union[int, float],
        y: Union[int, float],
        amount: int,
        vertical: bool = True
    ) -> None:
        """Scroll at specific coordinates.
        
        Args:
            x: X coordinate
            y: Y coordinate
            amount: Amount to scroll (positive for down/right, negative for up/left)
            vertical: If True, scroll vertically, otherwise horizontally
        """
        # First move to the specified position
        self.move_mouse(x, y)
        
        # Then scroll
        self.scroll(amount, vertical)
    
    def click_element(
        self, 
        element_content: str, 
        button: str = "left", 
        partial_match: bool = True
    ) -> bool:
        """Click on element with specified content.
        
        Args:
            element_content: Text content to find
            button: Mouse button ('left', 'right', 'middle')
            partial_match: If True, match substrings
            
        Returns:
            bool: True if clicked, False if element not found
        """
        # Update visual state first
        self.update_visual_state()
        
        # Find element
        element = self.visual_state.find_element_by_content(element_content, partial_match)
        if not element:
            logger.warning(f"Element with content '{element_content}' not found")
            return False
        
        # Click at center of element
        if self.use_normalized_coordinates:
            self.click(element.normalized_center_x, element.normalized_center_y, button)
        else:
            self.click(element.center_x, element.center_y, button)
        return True
    
    def type_text(self, text: str) -> None:
        """Type text using keyboard.
        
        This method types a string of text as if typed on the keyboard.
        It's useful for entering text into forms, search fields, or documents.
        
        Args:
            text: Text to type
        """
        logger.info(f"Typing text: {text}")
        self.keyboard_controller.type(text)
    
    def press_key(self, key: str) -> None:
        """Press a single key.
        
        This method presses and releases a single key. It handles both regular character
        keys (like 'a', '5', etc.) and special keys (like 'enter', 'tab', 'escape').
        
        Use this method for individual key presses (e.g., pressing Enter to submit a form
        or Escape to close a dialog).
        
        Args:
            key: Key to press (e.g., 'a', 'enter', 'tab', 'escape')
            
        Examples:
            press_key('enter')
            press_key('tab')
            press_key('a')
        """
        logger.info(f"Pressing key: {key}")
        
        # Try to map to a special key if needed
        try:
            if len(key) == 1:
                # Regular character key
                self.keyboard_controller.press(key)
                self.keyboard_controller.release(key)
            else:
                # Special key (like enter, tab, etc.)
                key_obj = getattr(keyboard.Key, key.lower())
                self.keyboard_controller.press(key_obj)
                self.keyboard_controller.release(key_obj)
        except (AttributeError, KeyError) as e:
            logger.error(f"Unknown key '{key}': {e}")
    
    def press_hotkey(self, keys: List[str]) -> None:
        """Press a hotkey combination (multiple keys pressed simultaneously).
        
        This method handles keyboard shortcuts like Ctrl+C, Alt+Tab, etc.
        It presses all keys in the given list simultaneously, then releases them
        in reverse order.
        
        Unlike press_key() which works with a single key, this method allows
        for complex key combinations that must be pressed together.
        
        Args:
            keys: List of keys to press simultaneously (e.g., ['ctrl', 'c'])
            
        Examples:
            press_hotkey(['ctrl', 'c'])  # Copy
            press_hotkey(['alt', 'tab'])  # Switch window
            press_hotkey(['ctrl', 'alt', 'delete'])  # System operation
        """
        logger.info(f"Pressing hotkey: {'+'.join(keys)}")
        
        key_objects = []
        # First press all modifier keys
        for key in keys:
            try:
                if len(key) == 1:
                    key_objects.append(key)
                else:
                    key_obj = getattr(keyboard.Key, key.lower())
                    key_objects.append(key_obj)
                self.keyboard_controller.press(key_objects[-1])
            except (AttributeError, KeyError) as e:
                logger.error(f"Unknown key '{key}' in hotkey: {e}")
        
        # Then release all keys in reverse order
        for key_obj in reversed(key_objects):
            self.keyboard_controller.release(key_obj)
    
    async def describe_screen_with_claude(self) -> str:
        """Generate a detailed description of the current screen with Claude.
        
        Returns:
            str: Detailed screen description
        """
        # Update visual state
        self.update_visual_state()
        
        # Create a system prompt for screen description
        system_prompt = """You are an expert UI analyst.
Your task is to provide a detailed description of the user interface shown in the screen.
Focus on:
1. The overall layout and purpose of the screen
2. Key interactive elements and their likely functions
3. Text content and its meaning
4. Hierarchical organization of the interface
5. Possible user actions and workflows

Be detailed but concise. Organize your description logically."""
        
        # Generate a prompt with the visual state and captured screenshot
        prompt = f"""
Please analyze this user interface and provide a detailed description.

Here is the structured data of the UI elements:
```json
{self.visual_state.to_mcp_description(self.use_normalized_coordinates)}
```

Describe the overall screen, main elements, and possible interactions a user might perform.
"""
        
        # Get response from Claude
        response = anthropic.prompt(
            prompt=prompt, 
            system_prompt=system_prompt,
            api_key=self.claude_api_key
        )
        
        return response
    
    async def describe_element_with_claude(self, element: ScreenElement) -> str:
        """Generate a detailed description of a specific UI element with Claude.
        
        Args:
            element: The ScreenElement to describe
            
        Returns:
            str: Detailed element description
        """
        # Create a system prompt for element description
        system_prompt = """You are an expert UI element analyst.
Your task is to provide a detailed description of a specific UI element.
Focus on:
1. The element's type and function
2. Its visual appearance and text content
3. How a user might interact with it
4. Its likely purpose in the interface
5. Any accessibility considerations

Be detailed but concise."""
        
        # Create element details in JSON
        element_json = json.dumps({
            "content": element.content,
            "type": element.type,
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
            "confidence": element.confidence
        }, indent=2)
        
        # Generate a prompt with the element data
        prompt = f"""
Please analyze this UI element and provide a detailed description:

```json
{element_json}
```

Describe what this element is, what it does, and how a user might interact with it.
"""
        
        # Get response from Claude
        response = anthropic.prompt(
            prompt=prompt, 
            system_prompt=system_prompt,
            api_key=self.claude_api_key
        )
        
        return response
    
    def prompt_claude(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        """Prompt Claude with the current visual state.
        
        Args:
            prompt: User prompt
            system_prompt: Optional system prompt
            
        Returns:
            str: Claude's response
        """
        if not self.claude_api_key or self.claude_api_key == "<ANTHROPIC_API_KEY>":
            logger.warning("Claude API key not set in config or constructor")
        
        # Update visual state
        self.update_visual_state()
        
        # Create Claude prompt
        mcp_description = self.visual_state.to_mcp_description(self.use_normalized_coordinates)
        
        full_prompt = f"""
Here is a description of the current screen state:
```json
{mcp_description}
```

Based on this screen state, {prompt}
"""
        
        # Default system prompt if not provided
        if not system_prompt:
            system_prompt = """You are an expert UI assistant that helps users navigate applications.
You have access to a structured description of the current screen through the Model Context Protocol.
Analyze the UI elements and provide clear, concise guidance based on the current screen state."""
        
        # Get response from Claude
        response = anthropic.prompt(
            prompt=full_prompt, 
            system_prompt=system_prompt,
            api_key=self.claude_api_key
        )
        
        return response
    
    def execute_natural_language_request(self, request: str) -> str:
        """Execute a natural language request by prompting Claude and taking action.
        
        Args:
            request: Natural language request
            
        Returns:
            str: Result description
        """
        # Update visual state
        self.update_visual_state()
        
        # Create coordinate format string
        coord_format = "normalized (0-1)" if self.use_normalized_coordinates else "absolute (pixels)"
        
        # Create specialized system prompt for action execution
        system_prompt = f"""You are an expert UI automation assistant that helps users control applications.
You have access to a structured description of the current screen through the Model Context Protocol.
Analyze the UI elements and decide what action to take to fulfill the user's request.

You MUST respond with a JSON object containing the action to perform in the following format:
{{
  "action": "click" | "type" | "press" | "describe",
  "params": {{
    // For click action:
    "element_content": "text to find", // or
    "x": 0.5, // {coord_format}
    "y": 0.5, // {coord_format}
    "button": "left" | "right" | "middle",
    
    // For type action:
    "text": "text to type",
    
    // For press action:
    "key": "enter" | "tab" | "escape" | etc.,
    
    // For describe action (no additional params needed)
  }},
  "reasoning": "Brief explanation of why you chose this action"
}}

Only return valid JSON. Do not include any other text in your response."""
        
        # Prompt Claude for action decision
        response = self.prompt_claude(
            prompt=f"decide what action to perform to fulfill this request: '{request}'",
            system_prompt=system_prompt
        )
        
        # Parse response
        try:
            action_data = json.loads(response)
            action_type = action_data.get("action", "")
            params = action_data.get("params", {})
            reasoning = action_data.get("reasoning", "No reasoning provided")
            
            logger.info(f"Action: {action_type}, Params: {params}, Reasoning: {reasoning}")
            
            # Execute action
            if action_type == "click":
                if "element_content" in params:
                    success = self.click_element(
                        params["element_content"], 
                        params.get("button", "left"),
                        True
                    )
                    if success:
                        return f"Clicked element: {params['element_content']}"
                    else:
                        return f"Failed to find element: {params['element_content']}"
                elif "x" in params and "y" in params:
                    self.click(
                        params["x"], 
                        params["y"], 
                        params.get("button", "left")
                    )
                    return f"Clicked at coordinates ({params['x']}, {params['y']})"
            elif action_type == "type":
                self.type_text(params.get("text", ""))
                return f"Typed text: {params.get('text', '')}"
            elif action_type == "press":
                self.press_key(params.get("key", ""))
                return f"Pressed key: {params.get('key', '')}"
            elif action_type == "describe":
                # Just return the reasoning as the description
                return reasoning
            else:
                return f"Unknown action type: {action_type}"
        except json.JSONDecodeError:
            logger.error(f"Failed to parse Claude response as JSON: {response}")
            return "Failed to parse action from Claude response"
        except Exception as e:
            logger.error(f"Error executing action: {e}")
            return f"Error executing action: {str(e)}"
    
    def run_interactive(self):
        """Run command-line interface (CLI) mode.
        
        This provides a simple prompt where users can enter natural language commands.
        Each command is processed by taking a screenshot, analyzing it with OmniParser,
        and using Claude to determine and execute the appropriate action.
        """
        logger.info("Starting OmniMCP CLI mode")
        logger.info(f"Coordinate mode: {'normalized (0-1)' if self.use_normalized_coordinates else 'absolute (pixels)'}")
        logger.info("Type 'exit' or 'quit' to exit")
        
        while True:
            request = input("\nEnter command: ")
            if request.lower() in ("exit", "quit"):
                break
                
            result = self.execute_natural_language_request(request)
            print(f"Result: {result}")
            
            # Give some time for UI to update before next request
            time.sleep(1)
    
    def save_visual_debug(self, output_path: Optional[str] = None, debug_dir: Optional[str] = None) -> str:
        """Save visualization of current visual state for debugging.
        
        Args:
            output_path: Path to save the image. If None, generates a timestamped filename.
            debug_dir: Directory to save debug files. If None, uses ~/omnimcp_debug
            
        Returns:
            str: Path to the saved image
        """
        # Update visual state
        self.update_visual_state()
        
        # Generate timestamped filename if not provided
        if output_path is None:
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            
            # Use provided debug directory or default
            if debug_dir is None:
                debug_dir = os.path.join(os.path.expanduser("~"), "omnimcp_debug")
            
            # Ensure directory exists
            os.makedirs(debug_dir, exist_ok=True)
            
            # Create filename with timestamp
            output_path = os.path.join(debug_dir, f"debug_{timestamp}.png")
        
        # Create visualization and save
        vis_img = self.visual_state.visualize()
        vis_img.save(output_path)
        logger.info(f"Saved visual debug to {output_path}")
        
        return output_path
    
    def run_mcp_server(self):
        """Run the MCP server for this OmniMCP instance."""
        from openadapt.mcp.server import create_omnimcp_server
        
        server = create_omnimcp_server(self)
        server.run()
    
    async def run_mcp_server_async(self):
        """Run the MCP server asynchronously."""
        from openadapt.mcp.server import create_omnimcp_server
        
        server = create_omnimcp_server(self)
        await server.run_async()


def main():
    """Main entry point."""
    fire.Fire(OmniMCP)


if __name__ == "__main__":
    main()
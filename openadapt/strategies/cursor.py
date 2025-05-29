"""Implements a cursor-based replay strategy with visual feedback and self-correction.

This strategy allows:
1. Painting a red dot on suggested target locations
2. Analyzing screenshots with dots for accuracy
3. Self-correcting based on visual feedback
4. Evaluating different cursor movement approaches
"""

import cv2
import numpy as np
from pprint import pformat

from openadapt import models, strategies, utils
from openadapt.custom_logger import logger
from openadapt.strategies.mixins.openai import OpenAIReplayStrategyMixin


class CursorReplayStrategy(OpenAIReplayStrategyMixin, strategies.base.BaseReplayStrategy):
    """Cursor replay strategy that uses visual feedback and self-correction."""

    def __init__(
        self,
        recording: models.Recording,
        approach: str = "grid",  # One of: grid, joystick, quadrant, search, etc.
        dot_radius: int = 5,
        dot_color: tuple = (0, 0, 255),  # BGR format - Red
        max_corrections: int = 3,
        accuracy_threshold: int = 10,  # Pixel distance threshold for accuracy
    ) -> None:
        """Initialize the CursorReplayStrategy.

        Args:
            recording (models.Recording): The recording object.
            approach (str): The cursor movement approach to use.
            dot_radius (int): Radius of the painted dot in pixels.
            dot_color (tuple): BGR color tuple for the dot.
            max_corrections (int): Maximum number of self-corrections to attempt.
            accuracy_threshold (int): Maximum pixel distance considered accurate.
        """
        super().__init__(recording)
        self.approach = approach
        self.dot_radius = dot_radius
        self.dot_color = dot_color
        self.max_corrections = max_corrections
        self.accuracy_threshold = accuracy_threshold
        self.correction_count = 0
        self.action_history = []

    def paint_dot(self, screenshot: models.Screenshot, x: int, y: int) -> np.ndarray:
        """Paint a dot on the screenshot at the specified coordinates.

        Args:
            screenshot (models.Screenshot): The screenshot to paint on.
            x (int): X coordinate for the dot.
            y (int): Y coordinate for the dot.

        Returns:
            np.ndarray: The modified screenshot image with the dot.
        """
        img = screenshot.image.copy()
        cv2.circle(img, (x, y), self.dot_radius, self.dot_color, -1)
        return img

    def analyze_dot_accuracy(
        self, screenshot: models.Screenshot, target_x: int, target_y: int
    ) -> tuple[bool, str]:
        """Analyze if the painted dot accurately represents the target location.

        Args:
            screenshot (models.Screenshot): The screenshot with the painted dot.
            target_x (int): Target X coordinate.
            target_y (int): Target Y coordinate.

        Returns:
            tuple[bool, str]: (is_accurate, feedback) where feedback explains any issues.
        """
        # Create prompt for the model
        system_prompt = """You are a computer vision expert analyzing cursor placement accuracy.
Given a screenshot with a red dot indicating a suggested cursor position, determine:
1. If the dot is accurately placed on the intended target
2. If not accurate, explain why and suggest how to correct it
3. Provide coordinates for any suggested corrections"""

        # Prepare the image and prompt
        img_with_dot = screenshot.image
        height, width = img_with_dot.shape[:2]
        
        prompt = f"""Analyze this screenshot where we've placed a red dot at coordinates ({target_x}, {target_y}).
The image dimensions are {width}x{height} pixels.

Please determine:
1. Is the red dot accurately placed on the target?
2. If not, describe what's wrong and suggest corrections.
3. If corrections are needed, provide specific x,y coordinates.

Format your response as:
ACCURATE: true/false
FEEDBACK: your analysis
CORRECTION: x,y coordinates (only if needed)"""

        # Get model's analysis
        completion = self.get_completion(prompt, system_prompt)
        
        # Parse the response
        lines = completion.strip().split('\n')
        is_accurate = False
        feedback = "Could not parse model response"
        correction_coords = None
        
        for line in lines:
            if line.startswith('ACCURATE:'):
                is_accurate = line.split(':')[1].strip().lower() == 'true'
            elif line.startswith('FEEDBACK:'):
                feedback = line.split(':')[1].strip()
            elif line.startswith('CORRECTION:'):
                try:
                    coords = line.split(':')[1].strip()
                    x, y = map(int, coords.split(','))
                    correction_coords = (x, y)
                except (ValueError, IndexError):
                    pass
        
        # If we got correction coordinates, include them in the feedback
        if correction_coords and not is_accurate:
            feedback += f" Suggested coordinates: {correction_coords}"
            
            # Calculate distance to suggested correction
            distance = np.sqrt(
                (target_x - correction_coords[0]) ** 2 + 
                (target_y - correction_coords[1]) ** 2
            )
            
            # Update accuracy based on distance threshold
            if distance <= self.accuracy_threshold:
                is_accurate = True
                feedback += f" (within {self.accuracy_threshold}px threshold)"
        
        return is_accurate, feedback

    def get_next_action_event(
        self,
        screenshot: models.Screenshot,
        window_event: models.WindowEvent,
    ) -> models.ActionEvent:
        """Get the next ActionEvent for replay.

        Args:
            screenshot (models.Screenshot): The current screenshot.
            window_event (models.WindowEvent): The current window event.

        Returns:
            models.ActionEvent: The next ActionEvent for replay.
        """
        if not self.action_history:
            # First action - initialize based on approach
            action = self._initialize_approach(screenshot, window_event)
        else:
            # Get next action based on current state and approach
            action = self._get_next_approach_action(screenshot, window_event)

        # Paint dot at suggested location
        if action.name in ["mouse_move", "mouse_click"]:
            img_with_dot = self.paint_dot(screenshot, action.mouse_x, action.mouse_y)
            
            # Update screenshot with dot for analysis
            screenshot.image = img_with_dot
            
            # Analyze accuracy and potentially self-correct
            is_accurate, feedback = self.analyze_dot_accuracy(
                screenshot, action.mouse_x, action.mouse_y
            )
            
            if not is_accurate and self.correction_count < self.max_corrections:
                self.correction_count += 1
                logger.info(f"Self-correction attempt {self.correction_count}: {feedback}")
                
                # Try to extract correction coordinates from feedback
                import re
                coords_match = re.search(r'coordinates: \((\d+), (\d+)\)', feedback)
                if coords_match:
                    new_x, new_y = map(int, coords_match.groups())
                    # Create a new corrected action
                    action = models.ActionEvent(
                        name=action.name,
                        mouse_x=new_x,
                        mouse_y=new_y,
                        window_event=window_event,
                    )
            else:
                self.correction_count = 0

        self.action_history.append(action)
        return action

    def _initialize_approach(
        self, screenshot: models.Screenshot, window_event: models.WindowEvent
    ) -> models.ActionEvent:
        """Initialize the selected cursor movement approach.

        Args:
            screenshot (models.Screenshot): The current screenshot.
            window_event (models.WindowEvent): The current window event.

        Returns:
            models.ActionEvent: The initial action for the selected approach.
        """
        # TODO: Implement initialization for each approach
        if self.approach == "grid":
            return self._init_grid_approach(screenshot, window_event)
        elif self.approach == "joystick":
            return self._init_joystick_approach(screenshot, window_event)
        # Add other approaches as needed
        else:
            raise ValueError(f"Unknown approach: {self.approach}")

    def _get_next_approach_action(
        self, screenshot: models.Screenshot, window_event: models.WindowEvent
    ) -> models.ActionEvent:
        """Get the next action based on the current approach.

        Args:
            screenshot (models.Screenshot): The current screenshot.
            window_event (models.WindowEvent): The current window event.

        Returns:
            models.ActionEvent: The next action for the selected approach.
        """
        # TODO: Implement next action logic for each approach
        if self.approach == "grid":
            return self._next_grid_action(screenshot, window_event)
        elif self.approach == "joystick":
            return self._next_joystick_action(screenshot, window_event)
        # Add other approaches as needed
        else:
            raise ValueError(f"Unknown approach: {self.approach}")

    def _init_grid_approach(
        self, screenshot: models.Screenshot, window_event: models.WindowEvent
    ) -> models.ActionEvent:
        """Initialize the grid-based cursor movement approach.

        Args:
            screenshot (models.Screenshot): The current screenshot.
            window_event (models.WindowEvent): The current window event.

        Returns:
            models.ActionEvent: The initial action for the grid approach.
        """
        # TODO: Implement grid initialization
        raise NotImplementedError("Grid approach not implemented yet")

    def _init_joystick_approach(
        self, screenshot: models.Screenshot, window_event: models.WindowEvent
    ) -> models.ActionEvent:
        """Initialize the joystick-based cursor movement approach.

        Args:
            screenshot (models.Screenshot): The current screenshot.
            window_event (models.WindowEvent): The current window event.

        Returns:
            models.ActionEvent: The initial action for the joystick approach.
        """
        # TODO: Implement joystick initialization
        raise NotImplementedError("Joystick approach not implemented yet")

    def _next_grid_action(
        self, screenshot: models.Screenshot, window_event: models.WindowEvent
    ) -> models.ActionEvent:
        """Get the next action for the grid-based approach.

        Args:
            screenshot (models.Screenshot): The current screenshot.
            window_event (models.WindowEvent): The current window event.

        Returns:
            models.ActionEvent: The next action for the grid approach.
        """
        # TODO: Implement grid movement logic
        raise NotImplementedError("Grid approach not implemented yet")

    def _next_joystick_action(
        self, screenshot: models.Screenshot, window_event: models.WindowEvent
    ) -> models.ActionEvent:
        """Get the next action for the joystick-based approach.

        Args:
            screenshot (models.Screenshot): The current screenshot.
            window_event (models.WindowEvent): The current window event.

        Returns:
            models.ActionEvent: The next action for the joystick approach.
        """
        # TODO: Implement joystick movement logic
        raise NotImplementedError("Joystick approach not implemented yet")

    def __del__(self) -> None:
        """Log the action history."""
        action_history_dicts = [
            action.to_prompt_dict() for action in self.action_history
        ]
        logger.info(f"action_history=\n{pformat(action_history_dicts)}") 
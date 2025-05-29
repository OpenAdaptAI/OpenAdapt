"""Grid-based cursor movement experiment.

This approach divides the screen into a grid and uses AI feedback to identify
which cell contains the target, then refines the position within that cell.
"""

import cv2
import numpy as np

from openadapt import models
from openadapt.custom_logger import logger
from openadapt.strategies.cursor import CursorReplayStrategy


class GridCursorStrategy(CursorReplayStrategy):
    """Grid-based cursor movement strategy."""

    def __init__(
        self,
        recording: models.Recording,
        grid_size: tuple[int, int] = (4, 4),  # 4x4 grid by default
        refinement_steps: int = 2,  # Number of times to subdivide target cell
    ) -> None:
        """Initialize the GridCursorStrategy.

        Args:
            recording (models.Recording): The recording object.
            grid_size (tuple[int, int]): Number of rows and columns in the grid.
            refinement_steps (int): Number of times to subdivide the target cell.
        """
        super().__init__(recording, approach="grid")
        self.grid_size = grid_size
        self.refinement_steps = refinement_steps
        self.current_grid = None
        self.current_cell = None
        self.refinement_step = 0

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
        # Create initial grid
        height, width = screenshot.image.shape[:2]
        rows, cols = self.grid_size
        
        # Calculate cell dimensions
        cell_height = height // rows
        cell_width = width // cols
        
        # Create grid representation
        self.current_grid = {
            'height': height,
            'width': width,
            'rows': rows,
            'cols': cols,
            'cell_height': cell_height,
            'cell_width': cell_width,
            'target_row': None,
            'target_col': None,
        }
        
        # Draw grid on screenshot for visualization
        img_with_grid = self._draw_grid(screenshot.image.copy())
        
        # Ask model to identify target cell
        target_cell = self._identify_target_cell(img_with_grid, window_event)
        self.current_cell = target_cell
        
        # Get initial position (center of target cell)
        x, y = self._get_cell_center(target_cell)
        
        # Create mouse move action to initial position
        return models.ActionEvent(
            name="mouse_move",
            mouse_x=x,
            mouse_y=y,
            window_event=window_event,
        )

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
        if self.refinement_step >= self.refinement_steps:
            # We've finished refining, perform the click
            return models.ActionEvent(
                name="mouse_click",
                mouse_x=self.action_history[-1].mouse_x,
                mouse_y=self.action_history[-1].mouse_y,
                window_event=window_event,
            )
            
        # Subdivide current cell into smaller grid
        self._refine_grid()
        self.refinement_step += 1
        
        # Draw refined grid
        img_with_grid = self._draw_grid(screenshot.image.copy())
        
        # Ask model to identify target subcell
        target_subcell = self._identify_target_cell(img_with_grid, window_event)
        self.current_cell = target_subcell
        
        # Get refined position
        x, y = self._get_cell_center(target_subcell)
        
        # Create mouse move action to refined position
        return models.ActionEvent(
            name="mouse_move",
            mouse_x=x,
            mouse_y=y,
            window_event=window_event,
        )

    def _draw_grid(self, img: np.ndarray) -> np.ndarray:
        """Draw the current grid on the image.

        Args:
            img (np.ndarray): The image to draw on.

        Returns:
            np.ndarray: The image with grid lines drawn.
        """
        height, width = img.shape[:2]
        rows, cols = self.grid_size
        
        # Draw vertical lines
        for i in range(cols + 1):
            x = (width * i) // cols
            cv2.line(img, (x, 0), (x, height), (0, 255, 0), 1)
            
        # Draw horizontal lines
        for i in range(rows + 1):
            y = (height * i) // rows
            cv2.line(img, (0, y), (width, y), (0, 255, 0), 1)
            
        return img

    def _identify_target_cell(
        self, img_with_grid: np.ndarray, window_event: models.WindowEvent
    ) -> tuple[int, int]:
        """Ask the model to identify which grid cell contains the target.

        Args:
            img_with_grid (np.ndarray): Screenshot with grid overlay.
            window_event (models.WindowEvent): Current window event.

        Returns:
            tuple[int, int]: (row, col) of the identified target cell.
        """
        # TODO: Implement model prompting to identify target cell
        # For now, return center cell
        return (self.grid_size[0] // 2, self.grid_size[1] // 2)

    def _get_cell_center(self, cell: tuple[int, int]) -> tuple[int, int]:
        """Get the center coordinates of a grid cell.

        Args:
            cell (tuple[int, int]): (row, col) of the cell.

        Returns:
            tuple[int, int]: (x, y) coordinates of cell center.
        """
        row, col = cell
        x = (col * self.current_grid['cell_width'] + 
             (col + 1) * self.current_grid['cell_width']) // 2
        y = (row * self.current_grid['cell_height'] + 
             (row + 1) * self.current_grid['cell_height']) // 2
        return x, y

    def _refine_grid(self) -> None:
        """Subdivide the current cell into a finer grid."""
        row, col = self.current_cell
        
        # Calculate boundaries of current cell
        x1 = col * self.current_grid['cell_width']
        y1 = row * self.current_grid['cell_height']
        x2 = (col + 1) * self.current_grid['cell_width']
        y2 = (row + 1) * self.current_grid['cell_height']
        
        # Update grid to focus on current cell
        self.current_grid = {
            'height': y2 - y1,
            'width': x2 - x1,
            'rows': self.grid_size[0],
            'cols': self.grid_size[1],
            'cell_height': (y2 - y1) // self.grid_size[0],
            'cell_width': (x2 - x1) // self.grid_size[1],
            'offset_x': x1,
            'offset_y': y1,
        } 
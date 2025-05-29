"""Test script for evaluating the grid-based cursor movement approach."""

import cv2
import numpy as np
from pathlib import Path
import time

from openadapt import models, replay
from openadapt.custom_logger import logger
from experiments.cursor.grid import GridCursorStrategy


def create_test_recording(
    target_x: int,
    target_y: int,
    window_width: int = 800,
    window_height: int = 600,
) -> models.Recording:
    """Create a test recording with a target at the specified location.

    Args:
        target_x (int): Target X coordinate.
        target_y (int): Target Y coordinate.
        window_width (int): Width of the test window.
        window_height (int): Height of the test window.

    Returns:
        models.Recording: A recording object for testing.
    """
    # Create a blank image
    img = np.zeros((window_height, window_width, 3), dtype=np.uint8)
    
    # Draw target (red circle)
    cv2.circle(img, (target_x, target_y), 5, (0, 0, 255), -1)
    
    # Save image
    test_dir = Path("experiments/cursor/test_data")
    test_dir.mkdir(parents=True, exist_ok=True)
    img_path = test_dir / "test_target.png"
    cv2.imwrite(str(img_path), img)
    
    # Create screenshot
    screenshot = models.Screenshot(
        image=img,
        timestamp=time.time(),
    )
    
    # Create window event
    window_event = models.WindowEvent(
        left=0,
        top=0,
        width=window_width,
        height=window_height,
        timestamp=time.time(),
    )
    
    # Create recording
    recording = models.Recording(
        screenshots=[screenshot],
        window_events=[window_event],
        action_events=[],  # No actions needed for testing
        timestamp=time.time(),
    )
    
    return recording


def evaluate_grid_strategy(
    target_positions: list[tuple[int, int]],
    grid_sizes: list[tuple[int, int]] = [(2, 2), (4, 4), (8, 8)],
    refinement_steps: list[int] = [1, 2, 3],
) -> dict:
    """Evaluate the grid-based cursor movement strategy.

    Args:
        target_positions: List of (x, y) target positions to test.
        grid_sizes: List of (rows, cols) grid sizes to test.
        refinement_steps: List of refinement step counts to test.

    Returns:
        dict: Evaluation results including accuracy and timing metrics.
    """
    results = {
        'grid_size': [],
        'refinement_steps': [],
        'target_x': [],
        'target_y': [],
        'final_x': [],
        'final_y': [],
        'distance_error': [],
        'num_actions': [],
        'time_taken': [],
    }
    
    for grid_size in grid_sizes:
        for steps in refinement_steps:
            for target_x, target_y in target_positions:
                # Create test recording
                recording = create_test_recording(target_x, target_y)
                
                # Initialize strategy
                strategy = GridCursorStrategy(
                    recording=recording,
                    grid_size=grid_size,
                    refinement_steps=steps,
                )
                
                # Time the execution
                start_time = time.time()
                
                try:
                    # Run strategy
                    strategy.run()
                    
                    # Get final position
                    final_action = strategy.action_history[-1]
                    final_x = final_action.mouse_x
                    final_y = final_action.mouse_y
                    
                    # Calculate error
                    distance_error = np.sqrt(
                        (final_x - target_x) ** 2 + 
                        (final_y - target_y) ** 2
                    )
                    
                    # Record results
                    results['grid_size'].append(f"{grid_size[0]}x{grid_size[1]}")
                    results['refinement_steps'].append(steps)
                    results['target_x'].append(target_x)
                    results['target_y'].append(target_y)
                    results['final_x'].append(final_x)
                    results['final_y'].append(final_y)
                    results['distance_error'].append(distance_error)
                    results['num_actions'].append(len(strategy.action_history))
                    results['time_taken'].append(time.time() - start_time)
                    
                except Exception as e:
                    logger.exception(f"Error evaluating grid {grid_size} with {steps} "
                                   f"refinement steps at target ({target_x}, {target_y}): {e}")
    
    return results


def main():
    """Run the grid strategy evaluation."""
    # Define test cases
    window_width = 800
    window_height = 600
    target_positions = [
        (100, 100),  # Top-left region
        (700, 100),  # Top-right region
        (400, 300),  # Center region
        (100, 500),  # Bottom-left region
        (700, 500),  # Bottom-right region
    ]
    
    # Run evaluation
    results = evaluate_grid_strategy(target_positions)
    
    # Print summary
    print("\nGrid Strategy Evaluation Results:")
    print("---------------------------------")
    print(f"Total test cases: {len(results['grid_size'])}")
    print(f"Average distance error: {np.mean(results['distance_error']):.2f} pixels")
    print(f"Average actions per target: {np.mean(results['num_actions']):.2f}")
    print(f"Average time per target: {np.mean(results['time_taken']):.2f} seconds")
    
    # Group by grid size
    grid_sizes = sorted(set(results['grid_size']))
    print("\nResults by grid size:")
    for grid_size in grid_sizes:
        indices = [i for i, g in enumerate(results['grid_size']) if g == grid_size]
        errors = [results['distance_error'][i] for i in indices]
        actions = [results['num_actions'][i] for i in indices]
        times = [results['time_taken'][i] for i in indices]
        
        print(f"\nGrid size: {grid_size}")
        print(f"  Average error: {np.mean(errors):.2f} pixels")
        print(f"  Average actions: {np.mean(actions):.2f}")
        print(f"  Average time: {np.mean(times):.2f} seconds")


if __name__ == "__main__":
    main() 
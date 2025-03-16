"""Minimal utilities needed for OmniMCP.

This module provides standalone implementations of essential utility functions
with lazy imports to minimize dependencies.
"""

def take_screenshot():
    """Take a screenshot of the entire screen.
    
    Returns:
        PIL.Image.Image: The screenshot image.
    """
    # Lazy imports to minimize dependencies
    from PIL import Image
    import mss
    
    # Create an mss instance for screenshot capture
    with mss.mss() as sct:
        # monitor 0 is the entire screen
        monitor = sct.monitors[0]
        sct_img = sct.grab(monitor)
        # Convert to PIL Image
        image = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
        
    return image


def get_monitor_dims():
    """Get the dimensions of the primary monitor.
    
    Returns:
        tuple[int, int]: The width and height of the monitor.
    """
    # Lazy import to minimize dependencies
    import mss
    
    # Create an mss instance to get monitor info
    with mss.mss() as sct:
        monitor = sct.monitors[0]
        monitor_width = monitor["width"]
        monitor_height = monitor["height"]
        
    return monitor_width, monitor_height
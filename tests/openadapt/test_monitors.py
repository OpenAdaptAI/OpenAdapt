"""Tests the take_screenshot function in openadapt/utils.py"""

import mss
import pytest
from unittest.mock import MagicMock, patch

from openadapt.utils import take_screenshot
from PIL import Image

def test_take_screenshot():
    """Test the take_screenshot function."""
    image = take_screenshot()
    assert isinstance(image, Image.Image)
    assert image.size == (1920, 1080)

@patch('openadapt.utils.get_current_monitor')
@patch('mss.mss')
def test_take_screenshot_multiple_monitors(mock_mss, mock_get_current_monitor):
    """Test the take_screenshot function with multiple monitors."""
    # Mock the return value of get_current_monitor to simulate the current monitor
    mock_get_current_monitor.return_value = {'left': 0, 'top': 0, 'width': 1920, 'height': 1080}
    
    # Mock the mss instance and its grab method
    mock_sct = mock_mss.return_value.__enter__.return_value
    mock_screenshot = MagicMock()
    mock_screenshot.size = (1920, 1080)
    mock_screenshot.bgra = b'\x00' * (1920 * 1080 * 4)
    mock_sct.grab.return_value = mock_screenshot
    
    image = take_screenshot()
    assert isinstance(image, Image.Image)
    # Assuming the function should capture the primary monitor
    assert image.size == (1920, 1080)
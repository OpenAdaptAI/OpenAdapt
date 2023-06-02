from unittest.mock import Mock
from unittest import mock
from PIL import Image
import numpy as np
from openadapt.models import Screenshot

def test_crop_active_window():
    # Create a mock action event with a mock window event
    action_event_mock = Mock()
    window_event_mock = Mock()

    # Define window_event's attributes
    window_event_mock.left = 100
    window_event_mock.top = 100
    window_event_mock.width = 300
    window_event_mock.height = 300
    action_event_mock.window_event = window_event_mock

    # Mock the utils.get_scale_ratios to return some fixed ratios
    with mock.patch('openadapt.utils.get_scale_ratios', return_value=(1, 1)):
        # Create a dummy image and put it in a Screenshot object
        image = Image.new('RGB', (500, 500), color='white')
        screenshot = Screenshot()
        screenshot._image = image

        # Store original image size for comparison
        original_size = screenshot._image.size

        # Perform the cropping operation
        screenshot.crop_active_window(action_event=action_event_mock)

        # Verify that the image size has been reduced
        assert ((screenshot._image.size[0] < original_size[0]) or 
                (screenshot._image.size[1] < original_size[1]))


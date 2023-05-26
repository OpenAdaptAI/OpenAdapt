"""
Implements a ReplayStrategy mixin for getting segmenting images via SAM model.

Uses SAM model:https://github.com/facebookresearch/segment-anything 

Usage:

    class MyReplayStrategy(SAMReplayStrategyMixin):
        ...
"""
import io
from pprint import pformat
from mss import mss
import mss.base
import numpy as np
from segment_anything import SamPredictor, sam_model_registry,SamAutomaticMaskGenerator
import time
from PIL import Image
from loguru import logger
from openadapt.events import get_events
from openadapt.utils import display_event, rows2dicts
from openadapt.models import Recording, Screenshot
from pathlib import Path
import urllib
import numpy as np
import torch
import matplotlib.pyplot as plt
import cv2
import json
import requests

from openadapt.strategies.base import BaseReplayStrategy

CHECKPOINT_URL_BASE = "https://dl.fbaipublicfiles.com/segment_anything/"
CHECKPOINT_URL_BY_NAME = {
    "default": f"{CHECKPOINT_URL_BASE}sam_vit_h_4b8939.pth",
    "vit_l": f"{CHECKPOINT_URL_BASE}sam_vit_l_0b3195.pth",
    "vit_b": f"{CHECKPOINT_URL_BASE}sam_vit_b_01ec64.pth",
}
MODEL_NAME = "default"
CHECKPOINT_DIR_PATH = "./checkpoints"

class SAMReplayStrategyMixin(BaseReplayStrategy):
    def __init__(
            self,
            recording: Recording,
            model_name=MODEL_NAME,
            checkpoint_dir_path=CHECKPOINT_DIR_PATH,
    ):
        super().__init__(recording)
        self.sam_model = self._initialize_model(model_name, checkpoint_dir_path)
        self.sam_predictor = SamPredictor(self.sam_model)
        self.sam_mask_generator = SamAutomaticMaskGenerator(self.sam_model)

    def _initialize_model(self, model_name, checkpoint_dir_path):
        checkpoint_url = CHECKPOINT_URL_BY_NAME[model_name]
        checkpoint_file_name = checkpoint_url.split("/")[-1]
        checkpoint_file_path = Path(checkpoint_dir_path, checkpoint_file_name)
        if not Path.exists(checkpoint_file_path):
            Path(checkpoint_dir_path).mkdir(parents=True, exist_ok=True)
            logger.info(
                f"downloading {checkpoint_url=} to {checkpoint_file_path=}")
            urllib.request.urlretrieve(checkpoint_url, checkpoint_file_path)
        return sam_model_registry[model_name](checkpoint=checkpoint_file_path)
    
    
    def get_screenshot_bbox(self, screenshot: Screenshot) -> str:
        """
        Get the bounding boxes of objects in a screenshot image.

        Args:
            screenshot (Screenshot): The screenshot object containing the image.

        Returns:
            str: A string representation of a list containing the bounding boxes of objects.

        """
        image_resized = resize_image(screenshot.image)
        array_resized = np.array(image_resized)
        masks = self.sam_mask_generator.generate(array_resized)
        logger.info(f"{masks=}")
        bbox_list = []
        for mask in masks :
            bbox_list.append(mask['bbox'])
        return str(bbox_list)
    

    def get_click_event_bbox(self, screenshot: Screenshot) -> str:
        """
        Get the bounding box of the clicked object in a screenshot image.

        Args:
            screenshot (Screenshot): The screenshot object containing the image.

        Returns:
            str: A string representation of a list containing the bounding box of the clicked object.
            None: If the screenshot does not represent a click event with the mouse pressed.

        """
        if screenshot.action_event.name == "click" and screenshot.action_event.mouse_pressed == True :
            image_resized = resize_image(screenshot.image)
            array_resized = np.array(image_resized)
            self.sam_predictor.set_image(array_resized)
            input_point = np.array([[screenshot.action_event.mouse_x, screenshot.action_event.mouse_y]])
            input_label = np.array([1])
            masks, scores, logits = self.sam_predictor.predict(
            point_coords=input_point,
            point_labels=input_label,
            multimask_output=True,
            )
            best_mask = masks[np.argmax(scores), :, :] # Get the best mask with highest score
            # Find foreground pixel coordinates
            rows, cols = np.where(best_mask)
            # Calculate bounding box coordinates
            x0 = np.min(cols)
            y0 = np.min(rows)
            w = np.max(cols) - x0
            h = np.max(rows) - y0
            return str([x0,y0,w,h])
        else :
            return None

def resize_image(image : Image) -> Image:
    """
    Resize the given image.

    Args:
        image (PIL.Image.Image): The image to be resized.

    Returns:
        PIL.Image.Image: The resized image.

    """
    resize_ratio = 0.1
    new_size = [ int(dim * resize_ratio) for dim in image.size]
    image_resized = image.resize(new_size)
    return image_resized
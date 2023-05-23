"""
Implements a ReplayStrategy mixin for getting segmenting images via SAM model.

Uses SAM model:https://github.com/facebookresearch/segment-anything 

Usage:

    class MyReplayStrategy(SAMReplayStrategyMixin):
        ...
"""
import io
from pprint import pformat
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
    
    def get_autosegmented_screenshot(self, screenshot: Screenshot) -> Screenshot:
        masks = self.sam_mask_generator.generate(screenshot.array)
        segmented_image = apply_masks(screenshot.image, masks)
        
        # Create a new Screenshot object with the segmented image
        segmented_screenshot = Screenshot()
        segmented_screenshot.sct_img = pil_to_sct(segmented_image)
        
        return segmented_screenshot
    
def apply_masks(self, image, masks):
    mask_img = np.zeros_like(image)
    
    for ann in masks:
        m = ann['segmentation']
        color_mask = np.random.random(3)
        mask_img[m] = color_mask
    
    segmented_image = Image.fromarray(mask_img)
    return segmented_image

def pil_to_sct(self, image):
    img_byte_arr = io.BytesIO()
    image.save(img_byte_arr, format='PNG')
    img_byte_arr.seek(0)
    
    return img_byte_arr.getvalue()
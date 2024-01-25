from ultralytics import SAM
from ultralytics.models.sam import Predictor as SAMPredictor
from ultralytics.utils.plotting import Annotator, colors
from PIL import Image
from loguru import logger
import numpy as np
import cv2

def fetch_segmented_image(input_image: Image) -> Image:
    # Convert PIL Image to a format compatible with SAM (numpy array)
    image_np = np.array(input_image)
    image_cv2 = cv2.cvtColor(image_np, cv2.COLOR_RGB2BGR)  # Convert to BGR format expected by cv2

    # Create SAMPredictor with specified configurations
    overrides = dict(conf=0.25, task='segment', mode='predict', imgsz=1024, model="sam_l.pt")
    predictor = SAMPredictor(overrides=overrides)
    
    # Set image for the predictor
    predictor.set_image(image_cv2)
    
    # Run inference to segment the entire image
    try:
        results = predictor()  # No specific prompts, segment everything
    except Exception as e:
        logger.error(f"Inference failed: {e}")
        return None

    # Check if 'results' contains expected data
    try:
        result = results[0] if isinstance(results, list) and len(results) > 0 else None
        if result and hasattr(result, 'masks') and hasattr(result, 'orig_img'):
            orig_img_np = result.orig_img
            masks = result.masks.xy if result.masks is not None else None
            
            # Prepare an annotator for drawing
            annotator = Annotator(orig_img_np, line_width=2)
            
            # If there are masks, draw them on the image
            if masks is not None:
                clss = result.boxes.cls.cpu().tolist()  # Get the class IDs
                for mask, cls in zip(masks, clss):
                    annotator.seg_bbox(mask=mask, mask_color=colors(int(cls), True), det_label=result.names[int(cls)])
            
            # Convert numpy array back to PIL Image
            segmented_image = Image.fromarray(cv2.cvtColor(annotator.im, cv2.COLOR_BGR2RGB))  # Convert back to RGB
            return segmented_image
        else:
            logger.error("Results do not contain the expected data")
            from pprint import pformat
            logger.info(f"results=\n{pformat(results)}")
            return None
    except Exception as e:
        logger.error(f"Failed to process segmentation masks: {e}")
        return None

# Example usage:
# input_image = Image.open('path/to/image.jpg')
# segmented_image = fetch_segmented_image(input_image)
# segmented_image.show()


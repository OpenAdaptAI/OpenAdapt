from ultralytics import FastSAM
from ultralytics.models.fastsam import FastSAMPrompt
from ultralytics.utils.plotting import Annotator, colors
from PIL import Image
from loguru import logger
import numpy as np

def fetch_segmented_image(input_image: Image) -> Image:
    # Convert PIL Image to a format compatible with FastSAM (numpy array)
    image_np = np.array(input_image)
    
    # Define the model and load weights
    model = FastSAM('FastSAM-x.pt')  # Ensure you're loading the correct model
    
    # Run inference on the numpy image array
    try:
        everything_results = model(image_np, device='cpu', retina_masks=True, imgsz=1024, conf=0.4, iou=0.9)
    except Exception as e:
        logger.error(f"Inference failed: {e}")
        return None
    
    # Prepare a Prompt Process object
    try:
        prompt_process = FastSAMPrompt(image_np, everything_results, device='cpu')

        # Choose the type of prompt you want to use
        # Here using everything prompt as an example, you can replace it with box_prompt, text_prompt, or point_prompt
        ann = prompt_process.everything_prompt()

        # Check if 'ann' is a list containing Results objects
        if isinstance(ann, list) and len(ann) > 0 and hasattr(ann[0], 'masks') and hasattr(ann[0], 'orig_img'):
            result = ann[0]  # Get the first (and presumably only) Results object
            orig_img_np = result.orig_img
            masks = result.masks.xy if result.masks is not None else None
            
            # Prepare an annotator for drawing
            annotator = Annotator(orig_img_np, line_width=2)
            
            # If there are masks, draw them on the image
            if masks is not None:
                clss = result.boxes.cls.cpu().tolist()  # Get the class IDs
                for mask, cls in zip(masks, clss):
                    annotator.seg_bbox(mask=mask, mask_color=colors(int(cls), True), det_label=model.model.names[int(cls)])
            
            # Convert numpy array back to PIL Image
            segmented_image = Image.fromarray(annotator.im)
            return segmented_image
        else:
            logger.error("ann does not contain the expected data")
            return None
    except Exception as e:
        logger.error(f"Failed to process segmentation masks: {e}")
        return None

# Example usage:
# input_image = Image.open('path/to/image.jpg')
# segmented_image = fetch_segmented_image(input_image)
# segmented_image.show()


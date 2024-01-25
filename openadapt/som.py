"""Set-of-Marks prompting, as per https://github.com/microsoft/SoM"""

import numpy as np
from loguru import logger
from PIL import Image, ImageDraw, ImageFont
from skimage import measure

from openadapt import replicate, ultralytics, utils


def get_contiguous_masks(image: Image) -> dict:
    """Convert an image with contiguous color masks into individual masks.

    This function processes the segmented image and extracts individual
    masks for each unique color (representing different objects).

    Args:
        image: An instance of PIL.Image with contiguous color masks.

    Returns:
        A dictionary where keys are color tuples and values are binary
        masks (numpy arrays) corresponding to each color/object.
    """
    logger.debug("Starting to get contiguous masks.")
    masks = {}
    image_np = np.array(image)
    unique_colors = np.unique(image_np.reshape(-1, image_np.shape[2]), axis=0)
    logger.debug(f"Unique colors identified: {len(unique_colors)}")
    
    for color in unique_colors:
        # Skip black color / background
        if (color == [0, 0, 0]).all():
            continue
        
        # Create a mask for the current color
        mask = (image_np == color).all(axis=2)
        masks[tuple(color)] = mask
        logger.debug(f"Processed mask for color: {color}")
    
    logger.debug("Finished processing all masks.")
    return masks

def draw_masks_and_labels_on_image(image: Image, masks: dict) -> Image:
    """Draws masks and labels on the image.

    For each mask in the masks dictionary, this function draws the mask's
    outline and a label on the original image.

    Args:
        image: An instance of PIL.Image representing the original image.
        masks: A dictionary with color tuples as keys and corresponding
               binary masks (numpy arrays) as values.

    Returns:
        An instance of PIL.Image with masks and labels drawn.
    """
    logger.debug("Starting to draw masks and labels on image.")
    image_draw = ImageDraw.Draw(image)
    #font = ImageFont.truetype("arial.ttf", 15)  # Specify the font and size
    font = utils.get_font("Arial.ttf", 15)
    
    for color, mask in masks.items():
        logger.debug(f"Drawing mask and label for color: {color}")
        # Find contours of the mask
        contours = measure.find_contours(mask, 0.5)
        
        for contour in contours:
            contour = np.flip(contour, axis=1)
            contour = contour.ravel().tolist()
            image_draw.line(contour, fill=tuple(color), width=5)
        
        # Draw label (you might want to calculate a better position)
        label_pos = (np.array(np.where(mask)).mean(axis=1) * [1, -1] + [10, -10]).astype(int)
        image_draw.text(label_pos, f"Object {tuple(color)}", fill="white", font=font)
    
    logger.debug("Finished drawing masks and labels on image.")
    return image

def resize_mask_to_match_screenshot(mask_image: Image, screenshot_image: Image) -> Image:
    """Resize the mask image to match the dimensions of the screenshot image.

    Args:
        mask_image: An instance of PIL.Image representing the mask image.
        screenshot_image: An instance of PIL.Image representing the screenshot (original) image.

    Returns:
        An instance of PIL.Image representing the resized mask image.
    """
    logger.debug("Starting to resize mask image to match screenshot.")
    logger.info(f"{screenshot_image.size=}")
    logger.info(f"{mask_image.size=}")
    # Get dimensions of the screenshot image
    screenshot_width, screenshot_height = screenshot_image.size
    # Resize the mask image to match the dimensions of the screenshot
    resized_mask_image = mask_image.resize(
        (screenshot_width, screenshot_height),
        Image.NEAREST
    )
    logger.info(f"{screenshot_image.size=}")
    logger.info(f"{resized_mask_image.size=}")
    logger.debug("Resizing completed.")

    return resized_mask_image

def get_marked_image(image: Image, display=True) -> Image:
    """Fetches, processes, and overlays masks and labels on the original image.

    Args:
        image: An instance of PIL.Image representing the original image.

    Returns:
        An instance of PIL.Image with masks and labels overlaid.
    """
    logger.debug("Starting to get marked image.")

    SEGMENTATION_PROVIDER = [
        #"REPLICATE",
        "ULTRALYTICS",
    ][0]

    # Fetch the segmented image from the API
    logger.debug("fetching segmented image...")
    if SEGMENTATION_PROVIDER == "REPLICATE":
        segmented_image = replicate.fetch_segmented_image(image)
    elif SEGMENTATION_PROVIDER == "ULTRALYTICS":
        segmented_image = ultralytics.fetch_segmented_image(image)
    #if display:
    #    utils.display_two_images(image, segmented_image)

    # Resize the segmented mask image to match the original image
    logger.info(f"resizing...")
    resized_segmented_image = resize_mask_to_match_screenshot(segmented_image, image)
    if display:
        utils.display_two_images(image, resized_segmented_image)

    import ipdb; ipdb.set_trace()

    # Get the contiguous masks from the resized segmented image
    logger.info(f"getting contiguous masks...")
    masks = get_contiguous_masks(resized_segmented_image)

    # Draw the masks and labels on the original image
    logger.info("drawing masks and labels...")
    marked_image = draw_masks_and_labels_on_image(image.copy(), masks)

    logger.debug("Completed getting marked image.")

    return marked_image

# Usage Example
# original_image = Image.open("path_to_your_image.jpg")
# marked_image = get_marked_image(original_image)
# marked_image.show()

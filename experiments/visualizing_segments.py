from loguru import logger
from PIL import Image
import numpy as np
from openadapt import vision
import cv2
from skimage.metrics import structural_similarity as ssim


def extract_difference_image(
    new_image: Image.Image,
    old_image: Image.Image,
    tolerance: float = 0.05,
) -> Image.Image:
    """Extract the portion of the new image that is different from the old image.

    Args:
        new_image: The new image as a PIL Image object.
        old_image: The old image as a PIL Image object.
        tolerance: Tolerance level to consider a pixel as different (default is 0.05).

    Returns:
        A PIL Image object representing the difference image.
    """
    new_image_np = np.array(new_image)
    old_image_np = np.array(old_image)

    # Compute the absolute difference between the two images in each color channel
    diff = np.abs(new_image_np - old_image_np)

    # Create a mask for the regions where the difference is above the tolerance
    mask = np.any(diff > (255 * tolerance), axis=-1)

    # Initialize an array for the segmented image
    segmented_image_np = np.zeros_like(new_image_np)

    # Set the pixels that are different in the new image
    segmented_image_np[mask] = new_image_np[mask]

    # Convert the numpy array back to an image
    return Image.fromarray(segmented_image_np)


def combine_images_with_masks(
    image_1: Image.Image,
    difference_image: Image.Image,
    old_masks: list[np.ndarray],
    new_masks: list[np.ndarray],
) -> Image.Image:
    """Combine image_1 and difference_image using the masks.

    Args:
        image_1: The original image as a PIL Image object.
        difference_image: The difference image as a PIL Image object.
        old_masks: List of numpy arrays representing the masks from the original image.
        new_masks: List of numpy arrays representing the masks from the difference image.

    Returns:
        A PIL Image object representing the combined image.
    """

    image_1_np = np.array(image_1)
    difference_image_np = np.array(difference_image)

    # Create an empty canvas with the same dimensions and mode as image_1
    combined_image_np = np.zeros_like(image_1_np)

    def masks_overlap(mask1, mask2):
        """Check if two masks overlap."""
        return np.any(np.logical_and(mask1, mask2))

    # Apply old masks to the combined image where there is no overlap with new masks
    for old_mask in old_masks:
        if not any(masks_overlap(old_mask, new_mask) for new_mask in new_masks):
            combined_image_np[old_mask] = image_1_np[old_mask]

    # Apply new masks to the combined image
    for new_mask in new_masks:
        combined_image_np[new_mask] = difference_image_np[new_mask]

    # Fill in remaining pixels from image_1 where there are no masks
    combined_image_np[(combined_image_np == 0).all(axis=-1)] = image_1_np[
        (combined_image_np == 0).all(axis=-1)
    ]

    # Convert the numpy array back to an image
    return Image.fromarray(combined_image_np)


def find_matching_sections_ssim(
    image_1: Image.Image,
    image_2: Image.Image,
    block_size: int = 50,
    threshold: float = 0.9,
):
    """Find and visualize matching sections between two images using SSIM.

    Args:
        image_1: The first image as a PIL Image object.
        image_2: The second image as a PIL Image object.
        block_size: The size of the blocks to compare in the SSIM calculation. Default is 50.
        threshold: The SSIM score threshold to consider blocks as matching. Default is 0.9.

    Returns:
        A PIL Image object with matching sections highlighted.
    """

    # Convert images to grayscale
    image_1_gray = np.array(image_1.convert("L"))
    image_2_gray = np.array(image_2.convert("L"))

    # Dimensions of the images
    height, width = image_1_gray.shape

    # Create an empty image to visualize matches
    matching_image = np.zeros_like(image_1_gray)

    # Iterate over the image in blocks
    for y in range(0, height, block_size):
        for x in range(0, width, block_size):
            # Define the block region
            block_1 = image_1_gray[y : y + block_size, x : x + block_size]
            block_2 = image_2_gray[y : y + block_size, x : x + block_size]

            # Check if blocks have the same shape
            if block_1.shape == block_2.shape:
                # Compute SSIM for the current block
                score, _ = ssim(block_1, block_2, full=True)

                # Highlight matching sections
                if score >= threshold:
                    matching_image[y : y + block_size, x : x + block_size] = 255

    # Convert matching regions back to RGB or RGBA for visualization
    if image_1.mode == "RGBA":
        matching_image_rgb = cv2.cvtColor(matching_image, cv2.COLOR_GRAY2RGBA)
        highlight_color = [255, 0, 0, 255]  # Red with full opacity for RGBA
    else:
        matching_image_rgb = cv2.cvtColor(matching_image, cv2.COLOR_GRAY2RGB)
        highlight_color = [255, 0, 0]  # Red for RGB

    # Create an overlay to highlight matching regions on the original image
    overlay = np.array(image_1)
    overlay[matching_image == 255] = highlight_color

    # Convert back to PIL Image
    matching_image_pil = Image.fromarray(overlay)

    return matching_image_pil


def visualize(image_1: Image, image_2: Image):
    """Visualize matching sections, difference sections, and combined images between two images.

    Args:
        image_1: The first image as a PIL Image object.
        image_2: The second image as a PIL Image object.

    Returns:
        None
    """

    try:
        images = []

        images.append(image_1)

        matching_image = find_matching_sections_ssim(image_1, image_2)
        images.append(matching_image)

        difference_image = extract_difference_image(image_2, image_1, tolerance=0.05)
        images.append(difference_image)

        old_masks = vision.get_masks_from_segmented_image(image_1)
        new_masks = vision.get_masks_from_segmented_image(difference_image)

        combined_image = combine_images_with_masks(
            image_1, difference_image, old_masks, new_masks
        )
        images.append(combined_image)

        for image in images:
            image.show()

    except Exception as e:
        logger.error(f"An error occurred: {e}")


# Example usage
img_1 = Image.open("./winCalOld.png")
img_2 = Image.open("./winCalNew.png")
visualize(img_1, img_2)

from PIL import Image, ImageEnhance, ImageOps, ImageFilter
import cv2
import numpy as np
from openadapt import adapters, config, utils

def apply_augmentations(image: Image.Image) -> list[Image.Image]:
    """
    Applies a series of augmentations to the image and returns a list of augmented images.
    
    Args:
        image (Image.Image): The original image.
        
    Returns:
        list[Image.Image]: List of augmented images.
    """
    augmented_images = []
    
    # Original image
    augmented_images.append(image)

    """
    # increase contrast
    enhancer = ImageEnhance.Contrast(image)
    enhanced_image = enhancer.enhance(1000)
    
    # Increase sharpness
    sharpness_enhancer = ImageEnhance.Sharpness(image)
    augmented_images.append(sharpness_enhancer.enhance(100))  # Sharper image
    
    # Adaptive Histogram Equalization (CLAHE)
    clahe_image = apply_clahe(image)
    augmented_images.append(clahe_image)
    
    # Edge Enhancement
    edge_enhanced_image = image.filter(ImageFilter.EDGE_ENHANCE)
    augmented_images.append(edge_enhanced_image)
    """

    # invert
    inverted_image = ImageOps.invert(image.convert("RGB"))
    augmented_images.append(inverted_image)

    enhancer = ImageEnhance.Contrast(inverted_image)
    enhanced_image = enhancer.enhance(1000)
    augmented_images.append(enhanced_image)
    
    return augmented_images

def apply_clahe(image: Image.Image) -> Image.Image:
    """
    Applies CLAHE (adaptive histogram equalization) to the image.
    
    Args:
        image (Image.Image): The original image.
        
    Returns:
        Image.Image: Image after applying CLAHE.
    """
    # Convert to OpenCV format (BGR)
    image_cv = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
    
    # Convert to grayscale
    gray_image = cv2.cvtColor(image_cv, cv2.COLOR_BGR2GRAY)
    
    # Apply CLAHE
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    equalized_image = clahe.apply(gray_image)
    
    # Convert back to RGB and PIL format
    equalized_image = cv2.cvtColor(equalized_image, cv2.COLOR_GRAY2RGB)
    return Image.fromarray(equalized_image)

def _main():
    image_file_path = config.ROOT_DIR_PATH / "../tests/assets/excel.png"
    image = Image.open(image_file_path)

    segmentation_adapter = adapters.get_default_segmentation_adapter()
    all_annotations = []

    # Apply augmentations to original and contrasted images
    images_to_segment = apply_augmentations(image)

    for _image in images_to_segment:
        _image.show()
        segmented_image = segmentation_adapter.fetch_segmented_image(
            _image,
            min_confidence_threshold=0,
            max_iou_threshold=0.05,
        )
        segmented_image.show()

from PIL import Image

from openadapt import adapters, config, plotting, vision, strategies, utils

def main():
    image_file_path = config.ROOT_DIR_PATH / "../tests/assets/excel.png"
    image = Image.open(image_file_path)
    image.show()
    CONTRAST_FACTOR = 1000
    image_contrasted = utils.increase_contrast(image, CONTRAST_FACTOR)

    segmentation_adapter = adapters.get_default_segmentation_adapter()
    all_annotations = []
    for _image in (image, image_contrasted):
        if 1:
            segmented_image = segmentation_adapter.fetch_segmented_image(
                _image,
                # threshold below which boxes will be filtered out
                min_confidence_threshold=0,
                # discards all overlapping boxes with IoU > iou_threshold
                max_iou_threshold=0.05,
            )
            segmented_image.show()
        else:
            annotations = segmentation_adapter.get_annotations(
                _image,
                # threshold below which boxes will be filtered out
                min_confidence_threshold=0,
                # discards all overlapping boxes with IoU > iou_threshold
                max_iou_threshold=0.1,
            )
            all_annotations += annotations
    import ipdb; ipdb.set_trace()
    masks = [annotation.masks.numpy() for annotation in all_annotations]
    import ipdb; ipdb.set_trace()

    masks = vision.get_masks_from_segmented_image(segmented_image)
    plotting.display_binary_images_grid(masks)

    refined_masks = vision.refine_masks(masks)
    plotting.display_binary_images_grid(refined_masks)

    masked_images = vision.extract_masked_images(image, refined_masks)

    similar_idx_groups, ungrouped_idxs, _, _ = vision.get_similar_image_idxs(
        masked_images,
        MIN_SEGMENT_SSIM,
        MIN_SEGMENT_SIZE_SIM,
    )
    ungrouped_masked_images = [
        masked_images[idx]
        for idx in ungrouped_idxs
    ]
    ungrouped_descriptions = strategies.visual.prompt_for_descriptions(
        image,
        ungrouped_masked_images,
        None,
    )

if __name__ == "__main__":
    main()

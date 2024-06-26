"""Computer vision module."""

import math

from loguru import logger
from PIL import Image
from scipy.ndimage import binary_fill_holes
from skimage.metrics import structural_similarity as ssim
import cv2
import numpy as np

from openadapt import cache


@cache.cache()
def get_masks_from_segmented_image(
    segmented_image: Image.Image, sort_by_area: bool = False
) -> list[np.ndarray]:
    """Get masks from a segmented image.

    Process the image to find unique masks based on color channels and optionally sort
    them by area.

    Args:
        segmented_image: A PIL.Image object of the segmented image.
        sort_by_area: A boolean flag to sort masks by their area in descending order.

    Returns:
        A list of numpy.ndarrays, each representing a unique mask. Sorted by area if
            specified.
    """
    logger.info("starting...")
    segmented_image_np = np.array(segmented_image)

    # Assume the last channel is the alpha channel if the image has 4 channels
    if segmented_image_np.shape[2] == 4:
        segmented_image_np = segmented_image_np[:, :, :3]

    # Find unique colors in the image, each unique color corresponds to a unique mask
    unique_colors = np.unique(
        segmented_image_np.reshape(-1, segmented_image_np.shape[2]), axis=0
    )
    logger.info(f"{len(unique_colors)=}")

    masks = []
    for color in unique_colors:
        # Create a mask for each unique color
        mask = np.all(segmented_image_np == color, axis=-1)
        masks.append(mask)

    if sort_by_area:
        # Calculate the area of each mask and sort the masks by area in descending order
        masks.sort(key=np.sum, reverse=True)

    logger.info(f"{len(masks)=}")
    return masks


def extract_masked_image(image: Image.Image, mask: np.ndarray) -> Image.Image:
    # Ensure the mask is in the correct format
    mask = mask.astype(np.uint8) * 255

    # Create a blank (transparent) image
    masked_image = Image.new("RGBA", image.size)

    # Convert the mask to an image
    mask_image = Image.fromarray(mask, mode="L")

    # Composite the original image and the blank image using the mask
    masked_image = Image.composite(image.convert("RGBA"), masked_image, mask_image)

    return masked_image


@cache.cache()
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


@cache.cache()
def filter_masks_by_size(
    masks: list[np.ndarray],
    min_mask_size: tuple[int, int] = (15, 15),
) -> list[np.ndarray]:
    """Filter masks based on minimum size using the bounding box of "on" pixels.

    Args:
        masks: A list of numpy.ndarrays, each representing a mask.
        min_mask_size: A tuple specifying the minimum dimensions (height, width) that
            the bounding box of the "on" pixels must have to be retained.

    Returns:
        A list of numpy.ndarrays, each representing a mask that meets the size criteria.
    """
    size_filtered_masks = []
    for mask in masks:
        coords = np.argwhere(mask)  # Get coordinates of all "on" pixels
        if coords.size > 0:
            y_min, x_min = coords.min(axis=0)
            y_max, x_max = coords.max(axis=0)
            height = y_max - y_min + 1
            width = x_max - x_min + 1
            if height >= min_mask_size[0] and width >= min_mask_size[1]:
                size_filtered_masks.append(mask)
    return size_filtered_masks


@cache.cache()
def refine_masks(masks: list[np.ndarray]) -> list[np.ndarray]:
    """Refine the list of masks.

    - Fill holes of any size.
    - Remove masks completely contained within other masks.
    - Exclude masks where the convex hull does not meet a specified minimum
      size in any dimension.

    Args:
        masks: A list of numpy.ndarrays, each representing a mask.
        min_mask_size: A tuple specifying the minimum dimensions (height,
            width) that the convex hull of a mask must have to be retained.

    Returns:
        A list of numpy.ndarrays, each representing a refined mask.
    """
    logger.info(f"{len(masks)=}")

    masks = remove_border_masks(masks)
    masks = filter_thin_ragged_masks(masks)

    # Fill holes in each mask
    filled_masks = [binary_fill_holes(mask).astype(np.uint8) for mask in masks]

    size_filtered_masks = filter_masks_by_size(filled_masks)

    # Remove masks completely contained within other masks
    refined_masks = []
    for i, mask_i in enumerate(size_filtered_masks):
        contained = False
        for j, mask_j in enumerate(size_filtered_masks):
            if i != j:
                # Check if mask_i is completely contained in mask_j
                if np.array_equal(mask_i & mask_j, mask_i):
                    contained = True
                    break
        if not contained:
            refined_masks.append(mask_i)

    logger.info(f"{len(refined_masks)=}")
    return refined_masks


@cache.cache()
def filter_thin_ragged_masks(
    masks: list[np.ndarray],
    kernel_size: int = 3,
    iterations: int = 5,
) -> list[np.ndarray]:
    """Applies morphological operations to filter out thin and ragged masks.

    Args:
        masks: A list of ndarrays, where each ndarray is a binary mask.
        kernel_size: Size of the structuring element.
        iterations: Number of times the operation is applied.

    Returns:
        A list of ndarrays with thin and ragged masks filtered out.
    """
    logger.info(f"{len(masks)=}")
    kernel = np.ones((kernel_size, kernel_size), np.uint8)
    filtered_masks = []

    for mask in masks:
        # Convert boolean mask to uint8
        mask_uint8 = mask.astype(np.uint8) * 255
        # Perform erosion
        eroded_mask = cv2.erode(mask_uint8, kernel, iterations=iterations)
        # Perform dilation
        dilated_mask = cv2.dilate(eroded_mask, kernel, iterations=iterations)

        # Convert back to boolean mask and add to the filtered list
        filtered_masks.append(dilated_mask > 0)

    logger.info(f"{len(filtered_masks)=}")
    return filtered_masks


@cache.cache()
def remove_border_masks(
    masks: list[np.ndarray],
    threshold_percent: float = 5.0,
) -> list[np.ndarray]:
    """Removes masks whose "on" pixels are close to the mask borders on all four sides.

    Args:
        masks: A list of ndarrays, where each ndarray is a binary mask.
        threshold_percent: A float indicating how close the "on" pixels can be to
              the border, represented as a percentage of the mask's dimensions.

    Returns:
    - A list of ndarrays with the border masks removed.
    """

    def is_close_to_all_borders(mask: np.ndarray, threshold: float) -> bool:
        # Determine actual threshold in pixels based on the percentage
        threshold_rows = int(mask.shape[0] * (threshold_percent / 100))
        threshold_cols = int(mask.shape[1] * (threshold_percent / 100))

        # Check for "on" pixels close to each border
        top = np.any(mask[:threshold_rows, :])
        bottom = np.any(mask[-threshold_rows:, :])
        left = np.any(mask[:, :threshold_cols])
        right = np.any(mask[:, -threshold_cols:])

        # If "on" pixels are close to all borders, return True
        return top and bottom and left and right

    logger.info(f"{len(masks)=}")

    filtered_masks = []
    for mask in masks:
        # Only add mask if it is not close to all borders
        if not is_close_to_all_borders(mask, threshold_percent):
            filtered_masks.append(mask)

    logger.info(f"{len(filtered_masks)=}")
    return filtered_masks


@cache.cache()
def extract_masked_images(
    original_image: Image.Image,
    masks: list[np.ndarray],
) -> list[Image.Image]:
    """Apply each mask to the original image.

    Resize the image to fit the mask's bounding box, discarding pixels outside the mask.

    Args:
        original_image: A PIL.Image object of the original image.
        masks: A list of numpy.ndarrays, each representing a refined mask.

    Returns:
        A list of PIL.Image objects, each cropped to the mask's bounding box and
        containing the content of the original image within that mask.
    """
    logger.info(f"{len(masks)=}")
    original_image_np = np.array(original_image)
    masked_images = []

    for mask in masks:
        # Find the bounding box of the mask
        rows = np.any(mask, axis=1)
        cols = np.any(mask, axis=0)
        rmin, rmax = np.where(rows)[0][[0, -1]]
        cmin, cmax = np.where(cols)[0][[0, -1]]

        # Crop the mask and the image to the bounding box
        cropped_mask = mask[rmin : rmax + 1, cmin : cmax + 1]
        cropped_image = original_image_np[rmin : rmax + 1, cmin : cmax + 1]

        # Apply the mask
        masked_image = np.where(cropped_mask[:, :, None], cropped_image, 0).astype(
            np.uint8
        )
        masked_images.append(Image.fromarray(masked_image))

    logger.info(f"{len(masked_images)=}")
    return masked_images


@cache.cache()
def calculate_bounding_boxes(
    masks: list[np.ndarray],
) -> tuple[list[dict[str, float]], list[tuple[float, float]]]:
    """Calculate bounding boxes and centers for each mask in the list separately.

    Args:
        masks: A list of numpy.ndarrays, each representing a mask.

    Returns:
        A tuple containing two lists:
        - The first list contains dictionaries, each containing the "top",
          "left", "height", "width" of the bounding box for each mask.
        - The second list contains tuples, each representing the "center" as a
          tuple of (x, y) for each mask.
    """
    bounding_boxes = []
    centroids = []
    for mask in masks:
        # Find all indices where mask is True
        rows, cols = np.where(mask)
        if len(rows) == 0 or len(cols) == 0:  # In case of an empty mask
            bounding_boxes.append({})
            centroids.append((float("nan"), float("nan")))
            continue

        # Calculate bounding box
        top, left = rows.min(), cols.min()
        height, width = rows.max() - top, cols.max() - left

        # Calculate center
        center_x, center_y = left + width / 2, top + height / 2

        # Append data to the lists
        bounding_boxes.append(
            {
                "top": float(top),
                "left": float(left),
                "height": float(height),
                "width": float(width),
            }
        )
        centroids.append((float(center_x), float(center_y)))

    return bounding_boxes, centroids


def get_image_similarity(
    im1: Image.Image,
    im2: Image.Image,
    grayscale: bool = False,
    win_size: int = 7,  # Default window size for SSIM
) -> tuple[float, np.array]:
    """Calculate the structural similarity index (SSIM) between two images.

    This function resizes the images to a common size maintaining their aspect ratios,
    and computes the SSIM either in grayscale or across each color channel separately.

    Args:
        im1 (Image.Image): The first image to compare.
        im2 (Image.Image): The second image to compare.
        grayscale (bool): If True, convert images to grayscale. Otherwise, compute
            SSIM on color channels.
        win_size (int): Window size for SSIM calculation. Must be odd and less than or
            equal to the smaller side of the images.

    Returns:
        tuple[float, np.array]: A tuple containing the SSIM and the difference image.
    """
    # Determine the minimum dimension size based on win_size, ensuring minimum size to
    # avoid win_size error
    min_dim_size = max(2 * win_size + 1, 7)

    # Calculate scale factors to ensure both dimensions are at least min_dim_size
    scale_factor1 = max(min_dim_size / im1.width, min_dim_size / im1.height, 1)
    scale_factor2 = max(min_dim_size / im2.width, min_dim_size / im2.height, 1)

    # Calculate common dimensions that accommodate both images
    target_width = max(int(im1.width * scale_factor1), int(im2.width * scale_factor2))
    target_height = max(
        int(im1.height * scale_factor1), int(im2.height * scale_factor2)
    )

    # Resize images to these new common dimensions
    im1 = im1.resize((target_width, target_height), Image.LANCZOS)
    im2 = im2.resize((target_width, target_height), Image.LANCZOS)

    if grayscale:
        # Convert images to grayscale
        im1 = np.array(im1.convert("L"))
        im2 = np.array(im2.convert("L"))
        data_range = max(im1.max(), im2.max()) - min(im1.min(), im2.min())
        mssim, diff_image = ssim(
            im1, im2, win_size=win_size, data_range=data_range, full=True
        )
    else:
        # Compute SSIM on each channel separately and then average the results
        mssims = []
        diff_images = []
        for c in range(3):  # Assuming RGB images
            im1_c = np.array(im1)[:, :, c]
            im2_c = np.array(im2)[:, :, c]
            data_range = max(im1_c.max(), im2_c.max()) - min(im1_c.min(), im2_c.min())
            ssim_c, diff_c = ssim(
                im1_c, im2_c, win_size=win_size, data_range=data_range, full=True
            )
            mssims.append(ssim_c)
            diff_images.append(diff_c)

        # Average the SSIM and create a mean difference image
        mssim = np.mean(mssims)
        diff_image = np.mean(diff_images, axis=0)

    return mssim, diff_image


@cache.cache()
def get_similar_image_idxs(
    images: list[Image.Image],
    min_ssim: float,
    min_size_sim: float,
    short_circuit_ssim: bool = True,
) -> tuple[list[list[int]], list[int], list[list[float]], list[list[float]]]:
    """Get images having Structural Similarity Index Measure (SSIM) above a threshold.

    Return the SSIM and size similarity matrices. Also returns indices of images not
    in any group. Optionally skips SSIM computation if the size difference exceeds the
    threshold.

    Args:
        images: A list of PIL.Image objects to compare.
        min_ssim: The minimum threshold for the SSIM for images to be considered
            similar.
        min_size_sim: Minimum required similarity in size as a fraction
            (e.g., 0.9 for 90% similarity required).
        short_circuit_ssim: If True, skips SSIM calculation when size similarity is
            below the threshold.

    Returns:
        A tuple containing four elements:
        - A list of lists, where each sublist contains indices of images in the input
          list that are similar to each other above the given SSIM and size thresholds.
        - A list of indices of images not part of any group.
        - A matrix of SSIM values between each pair of images.
        - A matrix of size similarity values between each pair of images.
    """
    num_images = len(images)
    already_compared = set()
    similar_groups = []
    ssim_matrix = [[0.0] * num_images for _ in range(num_images)]
    size_similarity_matrix = [[0.0] * num_images for _ in range(num_images)]
    all_indices = set(range(num_images))

    for i in range(num_images):
        ssim_matrix[i][i] = 1.0
        size_similarity_matrix[i][i] = 1.0
        for j in range(i + 1, num_images):
            size_sim = get_size_similarity(images[i], images[j])
            size_similarity_matrix[i][j] = size_similarity_matrix[j][i] = size_sim

            if not short_circuit_ssim or size_sim >= min_size_sim:
                s_ssim, _ = get_image_similarity(images[i], images[j])
                ssim_matrix[i][j] = ssim_matrix[j][i] = s_ssim
            else:
                ssim_matrix[i][j] = ssim_matrix[j][i] = math.nan

    for i in range(num_images):
        if i in already_compared:
            continue
        current_group = [i]
        for j in range(i + 1, num_images):
            if j in already_compared:
                continue
            if (
                ssim_matrix[i][j] >= min_ssim
                and size_similarity_matrix[i][j] >= min_size_sim
            ):
                current_group.append(j)
                already_compared.add(j)

        if len(current_group) > 1:
            similar_groups.append(current_group)
        already_compared.add(i)

    ungrouped_indices = list(all_indices - already_compared)

    return similar_groups, ungrouped_indices, ssim_matrix, size_similarity_matrix


def get_size_similarity(
    img1: Image.Image,
    img2: Image.Image,
) -> float:
    """Calculate size similarity between two images, returning a score between 0 and 1.

    1.0 indicates identical dimensions, values closer to 0 indicate greater disparity.

    Args:
        img1: First image to compare.
        img2: Second image to compare.

    Returns:
        A float indicating the similarity in size between the two images.
    """
    width1, height1 = img1.size
    width2, height2 = img2.size
    width_ratio = min(width1 / width2, width2 / width1)
    height_ratio = min(height1 / height2, height2 / height1)

    return (width_ratio + height_ratio) / 2


"""
# XXX TODO broken, unused
def filter_ui_components(
    masks: list[np.ndarray],
    area_threshold: tuple[float, float] | None = None,
    aspect_ratio_threshold: tuple[float, float] | None = None,
    extent_threshold: float | None = None,
    solidity_threshold: float | None = None,
) -> list[np.ndarray]:
    filtered_masks = []

    for mask in masks:
        mask_uint8 = mask.astype(np.uint8) * 255
        contours, _ = cv2.findContours(
            mask_uint8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )

        for contour in contours:
            area = cv2.contourArea(contour)

            if area_threshold is not None and (
                area < area_threshold[0] or area > area_threshold[1]
            ):
                continue

            x, y, w, h = cv2.boundingRect(contour)
            aspect_ratio = float(w) / h

            if aspect_ratio_threshold is not None and (
                aspect_ratio < aspect_ratio_threshold[0]
                or aspect_ratio > aspect_ratio_threshold[1]
            ):
                continue

            bounding_rect_area = w * h
            extent = area / bounding_rect_area if bounding_rect_area > 0 else 0

            if extent_threshold is not None and extent < extent_threshold:
                continue

            hull = cv2.convexHull(contour)
            hull_area = cv2.contourArea(hull)
            solidity = area / hull_area if hull_area > 0 else 0

            if solidity_threshold is not None and solidity < solidity_threshold:
                continue

            contour_mask = np.zeros(mask.shape, dtype=np.uint8)
            cv2.drawContours(
                contour_mask, [contour], -1, color=255, thickness=cv2.FILLED
            )
            filtered_masks.append(contour_mask)
    return filtered_masks
"""

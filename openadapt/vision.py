"""Computer vision module."""

from loguru import logger
from PIL import Image, ImageDraw
from scipy.ndimage import binary_fill_holes
import cv2
import numpy as np

from openadapt import utils


def process_image_for_masks(segmented_image: Image.Image) -> list[np.ndarray]:
    """Process the image to find unique masks based on color channels.

    Args:
        segmented_image: A PIL.Image object of the segmented image.

    Returns:
        A list of numpy.ndarrays, each representing a unique mask.
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

    logger.info(f"{len(masks)=}")
    return masks


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


def display_binary_images_grid(
    images: list[np.ndarray],
    grid_size: tuple[int, int] | None = None,
    margin: int = 10,
) -> None:
    """Display binary arrays as images on a grid with separation between grid cells.

    Args:
        images: A list of binary numpy.ndarrays.
        grid_size: Optional tuple (rows, cols) indicating the grid size.
            If not provided, a square grid size will be calculated.
        margin: The margin size between images in the grid.
    """
    if grid_size is None:
        grid_size = (int(np.ceil(np.sqrt(len(images)))),) * 2

    # Determine max dimensions of images in the list
    max_width = max(image.shape[1] for image in images) + margin
    max_height = max(image.shape[0] for image in images) + margin

    # Create a new image with a white background
    total_width = max_width * grid_size[1] + margin
    total_height = max_height * grid_size[0] + margin
    grid_image = Image.new("1", (total_width, total_height), 1)

    for index, binary_image in enumerate(images):
        # Convert ndarray to PIL Image
        img = Image.fromarray(binary_image.astype(np.uint8) * 255, "L").convert("1")
        img_with_margin = Image.new("1", (img.width + margin, img.height + margin), 1)
        img_with_margin.paste(img, (margin // 2, margin // 2))

        # Calculate the position on the grid
        row, col = divmod(index, grid_size[1])
        x = col * max_width + margin // 2
        y = row * max_height + margin // 2

        # Paste the image into the grid
        grid_image.paste(img_with_margin, (x, y))

    # Display the grid image
    grid_image.show()


def display_images_table_with_titles(
    images: list[Image.Image],
    titles: list[str] | None = None,
    margin: int = 10,
    fontsize: int = 20,
) -> None:
    """Display RGB PIL.Images in a table layout with titles to the right of each image.

    Args:
        images: A list of RGB PIL.Images.
        titles: An optional list of strings containing titles for each image.
        margin: The margin size in pixels between images and their titles.
        fontsize: The size of the title font.
    """
    if titles is None:
        titles = [""] * len(images)
    elif len(titles) != len(images):
        raise ValueError("The length of titles must match the length of images.")

    font = utils.get_font("Arial.ttf", fontsize)

    # Calculate the width and height required for the composite image
    max_image_width = max(image.width for image in images)
    total_height = sum(image.height for image in images) + margin * (len(images) - 1)
    max_title_height = fontsize + margin  # simple approach to calculating title height
    max_title_width = max(font.getsize(title)[0] for title in titles) + margin

    composite_image_width = max_image_width + max_title_width + margin * 3
    composite_image_height = max(
        total_height, max_title_height * len(images) + margin * (len(images) - 1)
    )

    # Create a new image to composite everything onto
    composite_image = Image.new(
        "RGB", (composite_image_width, composite_image_height), "white"
    )
    draw = ImageDraw.Draw(composite_image)

    current_y = 0
    for image, title in zip(images, titles):
        # Paste the image
        composite_image.paste(image, (margin, current_y))
        # Draw the title
        draw.text(
            (
                max_image_width + 2 * margin,
                current_y + image.height // 2 - fontsize // 2,
            ),
            title,
            fill="black",
            font=font,
        )
        current_y += image.height + margin

    composite_image.show()


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

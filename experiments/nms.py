from PIL import Image, ImageDraw
import numpy as np
import cv2
from openadapt import adapters, vision

DEBUG = True


def sliding_window(
    image: Image.Image, step_ratio: float, window_ratio: tuple[float, float]
) -> list[tuple[Image.Image, int, int]]:
    """Generate image patches using a sliding window, ensuring patches at the boundaries are complete and not duplicated."""
    step_size = (int(image.width * step_ratio), int(image.height * step_ratio))
    window_size = (
        int(image.width * window_ratio[0]),
        int(image.height * window_ratio[1]),
    )
    patches = []

    # Iterate over the image to extract patches
    for y in range(0, image.height, step_size[1]):
        for x in range(0, image.width, step_size[0]):
            # Adjust the patch location only if it extends beyond the image boundary
            if x + window_size[0] > image.width:
                x = (
                    image.width - window_size[0]
                )  # Adjust x to start earlier so the patch fits
            if y + window_size[1] > image.height:
                y = (
                    image.height - window_size[1]
                )  # Adjust y to start earlier so the patch fits

            patch = image.crop((x, y, x + window_size[0], y + window_size[1]))
            patches.append((patch, x, y))

            # Break the loop if the patch was adjusted to fit at the edge
            if x == image.width - window_size[0]:
                break
        # Break the outer loop if the patch was adjusted to fit at the bottom edge
        if y == image.height - window_size[1]:
            break

    return patches


def combine_and_nms(
    image: Image.Image, patches_boxes: list[tuple[list[dict[str, float]], int, int]]
) -> list[dict[str, float]]:
    """Combine patches and apply non-maximum suppression (NMS)."""
    bounding_boxes = []
    for boxes, x_offset, y_offset in patches_boxes:
        for box in boxes:
            box["top"] += y_offset
            box["left"] += x_offset
            bounding_boxes.append(box)

    boxes = np.array(
        [
            [
                box["left"],
                box["top"],
                box["left"] + box["width"],
                box["top"] + box["height"],
            ]
            for box in bounding_boxes
        ]
    )
    scores = np.ones(len(boxes))

    indices = cv2.dnn.NMSBoxes(
        boxes.tolist(), scores.tolist(), score_threshold=0.1, nms_threshold=0.9
    )
    print(f"{indices=}")

    if len(indices) > 0:
        indices = indices.flatten()

    nms_boxes = [bounding_boxes[i] for i in indices]

    return nms_boxes


def overlay_boxes(image: Image.Image, boxes: list[dict[str, float]]) -> Image.Image:
    """Overlay bounding boxes on the image."""
    draw = ImageDraw.Draw(image)
    for box in boxes:
        left = box["left"]
        top = box["top"]
        right = left + box["width"]
        bottom = top + box["height"]
        draw.rectangle([left, top, right, bottom], outline="red", width=2)
    return image


def main():
    image_path = "tests/assets/excel.png"
    image = Image.open(image_path)

    step_ratio = 1
    window_ratio = (1, 1)

    segmentation_adapter = adapters.get_default_segmentation_adapter()
    image_patches = sliding_window(image, step_ratio, window_ratio)
    patches_boxes = []

    for patch, x, y in image_patches:
        segmented_patch = segmentation_adapter.fetch_segmented_image(patch)
        if DEBUG:
            segmented_patch.show()
        patch_masks = vision.get_masks_from_segmented_image(segmented_patch)
        patch_boxes, _ = vision.calculate_bounding_boxes(patch_masks)
        patches_boxes.append((patch_boxes, x, y))

    nms_boxes = combine_and_nms(image, patches_boxes)
    image_with_boxes = overlay_boxes(image, nms_boxes)
    image_with_boxes.show()


if __name__ == "__main__":
    main()

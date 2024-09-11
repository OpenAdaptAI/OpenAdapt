"""Segmentation adapter using Ultralytics Fast Segment Anything.

See https://docs.ultralytics.com/models/fast-sam/#predict-usage for details.
"""

# flake8: noqa: E402

from pathlib import Path
from tempfile import TemporaryDirectory
import errno
import os
import time

from PIL import Image, ImageColor

# importing is required for use() to work
from PySide6.QtCore import Qt  # noqa

# use() required when invoked from tray
import matplotlib
import numpy as np

from openadapt.custom_logger import logger

matplotlib.use("Qt5Agg")


from ultralytics import FastSAM
from ultralytics.models.fastsam import FastSAMPredictor
from ultralytics.models.sam import Predictor as SAMPredictor
import fire
import numpy as np
import ultralytics

from openadapt import cache

FASTSAM_MODEL_NAMES = (
    "FastSAM-x.pt",
    "FastSAM-s.pt",
)
SAM_MODEL_NAMES = (
    "sam_b.pt",  # base
    "sam_l.pt",  # large
    # "mobile_sam.pt",
)
MODEL_NAMES = FASTSAM_MODEL_NAMES + SAM_MODEL_NAMES
DEFAULT_MODEL_NAME = MODEL_NAMES[0]


# TODO: rename
def fetch_segmented_image(
    image: Image.Image,
    model_name: str = DEFAULT_MODEL_NAME,
    **kwargs,
) -> Image.Image:
    """Segment a PIL.Image using ultralytics.

    Args:
        image: The input image to be segmented.
        model_name: The name of the model to use.
        kwargs: Arguments to pass to segmentation function.

    Returns:
        The segmented image as a PIL Image.
    """
    assert model_name in MODEL_NAMES, "{model_name=} must be in {MODEL_NAMES=}"
    if model_name in FASTSAM_MODEL_NAMES:
        return do_fastsam(image, model_name, **kwargs)
    else:
        return do_sam(image, model_name, **kwargs)


# TODO: support SAM models
# TODO: consolidate with do_fastsam
@cache.cache()
def get_annotations(
    image: Image,
    model_name: str = FASTSAM_MODEL_NAMES[0],
    # TODO: inject from config
    device: str = "cpu",
    retina_masks: bool = True,
    imgsz: int | tuple[int, int] | None = 1024,
    # threshold below which boxes will be filtered out
    min_confidence_threshold: float = 0.4,
    # discards all overlapping boxes with IoU > iou_threshold
    max_iou_threshold: float = 0.9,
    # The maximum number of boxes to keep after NMS.
    max_det: int = 1000,
    max_retries: int = 5,
    retry_delay_seconds: float = 0.1,
) -> Image:
    """Get mask segments via FastSAM.

    For usage of thresholds see:
    github.com/ultralytics/ultralytics/blob/dacbd48fcf8407098166c6812eeb751deaac0faf
        /ultralytics/utils/ops.py#L164

    Args:
        TODO
        min_confidence_threshold (float, optional): The minimum confidence score
            that a detection must meet or exceed to be considered valid. Detections
            below this threshold will not be marked. Defaults to 0.00.
        max_iou_threshold (float, optional): The maximum allowed Intersection over
            Union (IoU) value for overlapping detections. Detections that exceed this
            IoU threshold are considered for suppression, keeping only the
            detection with the highest confidence. Defaults to 0.05.
    """
    model = FastSAM(model_name)

    imgsz = imgsz or image.size

    everything_results = model(
        image,
        device=device,
        retina_masks=retina_masks,
        imgsz=imgsz,
        conf=min_confidence_threshold,
        iou=max_iou_threshold,
        max_det=max_det,
    )
    assert len(everything_results) == 1, len(everything_results)
    return everything_results


@cache.cache()
def do_fastsam(
    image: Image,
    model_name: str,
    # TODO: inject from config
    device: str = "cpu",
    retina_masks: bool = True,
    imgsz: int | tuple[int, int] | None = 1024,
    # threshold below which boxes will be filtered out
    min_confidence_threshold: float = 0.4,
    # discards all overlapping boxes with IoU > iou_threshold
    max_iou_threshold: float = 0.9,
    # The maximum number of boxes to keep after NMS.
    max_det: int = 1000,
    max_retries: int = 5,
    retry_delay_seconds: float = 0.1,
) -> Image:
    """Get segmented image via FastSAM.

    For usage of thresholds see:
    github.com/ultralytics/ultralytics/blob/dacbd48fcf8407098166c6812eeb751deaac0faf
        /ultralytics/utils/ops.py#L164

    Args:
        TODO
        min_confidence_threshold (float, optional): The minimum confidence score
            that a detection must meet or exceed to be considered valid. Detections
            below this threshold will not be marked. Defaults to 0.00.
        max_iou_threshold (float, optional): The maximum allowed Intersection over
            Union (IoU) value for overlapping detections. Detections that exceed this
            IoU threshold are considered for suppression, keeping only the
            detection with the highest confidence. Defaults to 0.05.
    """
    model = FastSAM(model_name)

    imgsz = imgsz or image.size

    everything_results = model(
        image,
        device=device,
        retina_masks=retina_masks,
        imgsz=imgsz,
        conf=min_confidence_threshold,
        iou=max_iou_threshold,
        max_det=max_det,
    )
    assert len(everything_results) == 1, len(everything_results)
    annotation = everything_results[0]

    segmented_image = Image.fromarray(
        annotation.plot(
            img=np.ones(annotation.orig_img.shape, dtype=annotation.orig_img.dtype),
            kpt_line=False,
            labels=False,
            boxes=False,
            probs=False,
            color_mode='instance',
        )
    )

    # Check if the dimensions of the original and segmented images differ
    # XXX TODO this is a hack, this plotting code should be refactored, but the
    # bug may exist in ultralytics, since they seem to resize as well; see:
    # https://github.com/ultralytics/ultralytics/blob/main/ultralytics/utils/plotting.py#L238
    # https://github.com/ultralytics/ultralytics/issues/561#issuecomment-1403079910
    if image.size != segmented_image.size:
        logger.warning(f"{image.size=} != {segmented_image.size=}, resizing...")
        # Resize segmented_image to match original using nearest neighbor interpolation
        segmented_image = segmented_image.resize(image.size, Image.NEAREST)

    assert image.size == segmented_image.size, (image.size, segmented_image.size)
    return segmented_image


@cache.cache()
def do_sam(
    image: Image.Image,
    model_name: str,
    # TODO: add params
) -> Image.Image:
    # Create SAMPredictor
    overrides = dict(
        conf=0.25,
        task="segment",
        mode="predict",
        imgsz=1024,
        model=model_name,
    )
    predictor = SAMPredictor(overrides=overrides)

    # Segment with additional args
    # results = predictor(source=image, crop_n_layers=1, points_stride=64)
    results = predictor(
        source=image,
        # crop_n_layers=3,
        # crop_overlap_ratio=0.5,
        # crop_downscale_factor=1,
        # point_grids=None,
        # points_stride=12,
        # points_batch_size=128,
        # conf_thres=0.8,
        # stability_score_thresh=0.95,
        # stability_score_offset=0.95,
        # crop_nms_thresh=0.8,
    )
    mask_ims = results_to_mask_images(results)
    segmented_image = colorize_masks(mask_ims)
    return segmented_image


def results_to_mask_images(
    results: list[ultralytics.engine.results.Results],
) -> list[Image.Image]:
    logger.info(f"{len(results)=}")
    masks = results[0].masks
    mask_arrs = [mask.data.cpu().detach().numpy() for mask in masks]
    assert all([mask_arr.shape[0] == 1 for mask_arr in mask_arrs])
    mask_ims = [Image.fromarray(mask_arr[0, :]) for mask_arr in mask_arrs]
    return mask_ims


def colorize_masks(masks: list[Image.Image]) -> Image.Image:
    """
    Takes a list of PIL images containing binary masks and returns a new PIL.Image
    where each mask is colored differently using a unique color for each mask.

    Args:
        masks (list[PIL.Image]): List of PIL images where each contains a binary mask.

    Returns:
        PIL.Image: A new image with each mask in a different color.
    """
    if not masks:
        return None  # Return None if the list is empty

    # Assuming all masks are the same size, get dimensions
    width, height = masks[0].size

    # Create an empty array with 3 color channels (RGB)
    result_image = np.zeros((height, width, 3), dtype=np.uint8)

    # Generate unique colors using HSV color space
    num_masks = len(masks)
    colors = [
        tuple(
            int(c * 255)
            for c in ImageColor.getcolor(
                f"hsv({int(i / num_masks * 360)}, 100%, 100%)",
                "RGB",
            )
        )
        for i in range(num_masks)
    ]

    for idx, mask in enumerate(masks):
        # Convert PIL Image to numpy array
        mask_array = np.array(mask)

        # Apply the color to the mask
        for c in range(3):
            # Only colorize where the mask is True (assuming mask is binary: 0 or 255)
            result_image[:, :, c] += (mask_array / 255 * colors[idx][c]).astype(
                np.uint8
            )

    # Convert the result back to a PIL image
    return Image.fromarray(result_image)


def fetch_segmented_image_from_path(image_path: str) -> None:
    """Gets a segmented image and displays it.

    Args:
        image_path: path to image to segment
    """
    with Image.open(image_path) as image:
        segmented_image = fetch_segmented_image(image)
        segmented_image.show()


if __name__ == "__main__":
    fire.Fire(fetch_segmented_image_from_path)

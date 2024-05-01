"""Adapter for getting segmentation via Replicate API."""

import io
import os

from loguru import logger
from PIL import Image
import fire
import replicate
import requests

from openadapt import cache, utils
from openadapt.config import config


@cache.cache()
def _fetch_segmented_image(image_uri: str) -> Image.Image:
    """Fetch segmented image.

    For details, see:
    https://replicate.com/pablodawson/segment-anything-automatic/api?tab=python#run

    Args:
        image_uri: URI of image to segment

    Returns:
        PIL Image containing segmented image
    """
    os.environ["REPLICATE_API_TOKEN"] = config.REPLICATE_API_TOKEN
    logger.info("segmenting image...")
    segmented_image_url = replicate.run(
        "pablodawson/segment-anything-automatic:"
        "14fbb04535964b3d0c7fad03bb4ed272130f15b956cbedb7b2f20b5b8a2dbaa0",
        input={
            "image": image_uri,
            # "resize_width": 1080,
            # "min_mask_region_area": 30
        },
    )
    logger.info(f"{segmented_image_url=}")
    image_data = requests.get(segmented_image_url).content
    image = Image.open(io.BytesIO(image_data))
    return image


def fetch_segmented_image(image: Image, n: int | None = 100) -> Image:
    """Fetch a segmented image from Segment Anything on Replicate.

    The image resolution is modified by a percent ratio before the segmentation
    request, and the segmented image is resized back to the original
    resolution.

    Args:
        image: The input image to be segmented.
        n: Optional; The percent ratio to adjust the resolution of the input
            image before segmentation. Default is 100 (no adjustment).

    Returns:
        The segmented image, resized back to the original resolution if it was adjusted.
    """
    # Store the original dimensions
    original_width, original_height = image.size

    # Adjust resolution if necessary
    if n is not None and n != 100:
        new_width = int(original_width * n / 100)
        new_height = int(original_height * n / 100)
        image = image.resize((new_width, new_height), Image.ANTIALIAS)

    # Convert adjusted image to a format suitable for segmentation
    image_uri = utils.image2utf8(image)

    # Fetch the segmented image
    segmented_image = _fetch_segmented_image(image_uri)

    # Resize the segmented image back to the original dimensions
    segmented_image = segmented_image.resize(
        (original_width, original_height), Image.NEAREST
    )

    return segmented_image


def fetch_segmented_image_from_path(image_path: str, n: int | None = 100) -> None:
    """Fetch a segmented image and save it to the same directory.

    '-segmented' is added before the file extension.

    Args:
        image_path: string containing path to image
        n: Optional; The percent ratio to adjust the resolution of the input
            image before segmentation. Default is 100 (no adjustment).
    """
    with Image.open(image_path) as image:
        segmented_image = fetch_segmented_image(image, n)
        # Construct new file name with '-segmented' before the extension
        base, ext = os.path.splitext(image_path)
        segmented_image_path = f"{base}-segmented{ext}"
        segmented_image.save(segmented_image_path)
        logger.info(f"Segmented image saved to: {segmented_image_path}")


if __name__ == "__main__":
    fire.Fire(fetch_segmented_image_from_path)

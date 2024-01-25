import os

from loguru import logger
from PIL import Image
import replicate
import requests

from openadapt import cache, config, utils

@cache.cache()
def _fetch_segmented_image(image_uri: str):
    """
    https://replicate.com/pablodawson/segment-anything-automatic/api?tab=python#run
    """
    os.environ["REPLICATE_API_TOKEN"] = config.REPLICATE_API_TOKEN
    output = replicate.run(
        "pablodawson/segment-anything-automatic:14fbb04535964b3d0c7fad03bb4ed272130f15b956cbedb7b2f20b5b8a2dbaa0",
        input={
            "image": image_uri,
        },
    )
    logger.info(f"output=\n{output}")    
    segmented_image_url = output

    image_data = requests.get(segmented_image_url).content

    # TODO: move to config
    OUT_DIR_PATH = "~/.openadapt/cache"
    os.makedirs(OUT_DIR_PATH, exist_ok=True)
    image_uri_hash = hash(image_uri)
    image_file_name = f"{image_uri_hash}.jpg"
    image_file_path = os.path.join(OUT_DIR_PATH, image_file_name)
    logger.info(f"{image_file_path=}")
    with open(image_file_path, 'wb') as handler:
        handler.write(image_data)

    return Image.open(image_file_path)

def fetch_segmented_image(image: Image) -> Image:
    """Fetch a segmented image from Segment Anything on Replicate"""
    image_uri = utils.image2utf8(image)
    return _fetch_segmented_image(image_uri)

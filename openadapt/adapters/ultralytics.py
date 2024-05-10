"""Segmentation adapter using Ultralytics Fast Segment Anything.

See https://docs.ultralytics.com/models/fast-sam/#predict-usage for details.
"""
# flake8: noqa: E402

from pathlib import Path
from tempfile import TemporaryDirectory
import os

from loguru import logger
from PIL import Image


# use() required when invoked from tray
import matplotlib

# importing is required for use() to work
from PySide6.QtCore import Qt  # noqa

matplotlib.use("Qt5Agg")


from ultralytics import FastSAM
from ultralytics.models.fastsam import FastSAMPrompt
import fire
import numpy as np

from openadapt import cache


MODEL_NAMES = (
    "FastSAM-x.pt",
    "FastSAM-s.pt",
)
MODEL_NAME = MODEL_NAMES[0]


# TODO: rename
@cache.cache()
def fetch_segmented_image(
    image: Image,
    model_name: str = MODEL_NAME,
    # TODO: inject from config
    device: str = "cpu",
    retina_masks: bool = True,
    imgsz: int = 1024,
    conf: float = 0.4,
    iou: float = 0.9,
) -> Image:
    """Segment a PIL.Image using ultralytics Fast Segment Anything.

    Args:
        image: The input image to be segmented.
        model_name: The name of the model to use. Must be FastSAM-s.pt or FastSAM-x.pt.
        TODO: document remaining params

    Returns:
        The segmented image as a PIL Image.
    """
    assert model_name in MODEL_NAMES, (model_name, MODEL_NAMES)

    model = FastSAM(model_name)

    # Run inference on image
    everything_results = model(
        image,
        device=device,
        retina_masks=retina_masks,
        imgsz=imgsz,
        conf=conf,
        iou=iou,
    )

    # Prepare a Prompt Process object
    prompt_process = FastSAMPrompt(image, everything_results, device="cpu")

    # Everything prompt
    annotations = prompt_process.everything_prompt()

    # TODO: support other modes:

    # Bbox default shape [0,0,0,0] -> [x1,y1,x2,y2]
    # annotations = prompt_process.box_prompt(bbox=[200, 200, 300, 300])

    # Text prompt
    # annotations = prompt_process.text_prompt(text='a photo of a dog')

    # Point prompt
    # points default [[0,0]] [[x1,y1],[x2,y2]]
    # point_label default [0] [1,0] 0:background, 1:foreground
    # annotations = prompt_process.point_prompt(points=[[200, 200]], pointlabel=[1])

    assert len(annotations) == 1, len(annotations)
    annotation = annotations[0]

    # hide original image
    annotation.orig_img = np.ones(annotation.orig_img.shape)

    # TODO: in memory, e.g. with prompt_process.fast_show_mask()
    with TemporaryDirectory() as tmp_dir:
        # Force the output format to PNG to prevent JPEG compression artefacts
        annotation.path = annotation.path.replace(".jpg", ".png")
        prompt_process.plot(
            [annotation],
            tmp_dir,
            with_contours=False,
            retina=False,
        )
        result_name = os.path.basename(annotation.path)
        logger.info(f"{annotation.path=}")
        image_path = Path(tmp_dir) / result_name
        image = Image.open(image_path)
        os.remove(image_path)
    return image


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

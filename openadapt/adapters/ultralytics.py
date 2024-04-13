"""Segmentation adapter using Ultralytics Fast Segment Anything.

See https://docs.ultralytics.com/models/fast-sam/#predict-usage for details.
"""

from tempfile import TemporaryDirectory
from pathlib import Path
import os

from loguru import logger
from PIL import Image
from ultralytics import FastSAM
from ultralytics.models.fastsam import FastSAMPrompt
import fire

from openadapt import cache


MODEL_NAMES = (
    "FastSAM-x.pt",
    "FastSAM-s.pt",
)
MODEL_NAME = MODEL_NAMES[-1]


# TODO: rename
@cache.cache()
def fetch_segmented_image(
    image: Image,
    model_name: str = MODEL_NAME,
    # TODO: inject from config
    device='cpu',
    retina_masks=True,
    imgsz=1024,
    conf=0.4,
    iou=0.9,
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
    prompt_process = FastSAMPrompt(image, everything_results, device='cpu')

    # Everything prompt
    ann = prompt_process.everything_prompt()

    # TODO: support other modes:

    # Bbox default shape [0,0,0,0] -> [x1,y1,x2,y2]
    #ann = prompt_process.box_prompt(bbox=[200, 200, 300, 300])

    # Text prompt
    #ann = prompt_process.text_prompt(text='a photo of a dog')

    # Point prompt
    # points default [[0,0]] [[x1,y1],[x2,y2]]
    # point_label default [0] [1,0] 0:background, 1:foreground
    #ann = prompt_process.point_prompt(points=[[200, 200]], pointlabel=[1])

    """
    fig, ax = plt.subplots()
    prompt_process.fast_show_mask(
        annotation=ann[0].masks,
        ax=ax,
        random_color=random_color,
        bbox=bbox,
        points=points,
        pointlabel=pointlabel,
        retinamask=retinamask
    )
    """
    assert len(ann) == 1, len(ann)
    ann_item = ann[0]

    import numpy as np
    ann_item.orig_img = np.ones(ann_item.orig_img.shape)

    with TemporaryDirectory() as tmp_dir:
        ann_item.path = ann_item.path.replace('.jpg', '.png')  # Force the output format to PNG
        prompt_process.plot(
            [ann_item],
            tmp_dir,
            with_contours=False,
            retina=False,
        )
        result_name = os.path.basename(ann_item.path)
        result_name = os.path.basename(ann_item.path).replace('.jpg', '.png')
        logger.info(f"{ann_item.path=}")
        image_path = Path(tmp_dir) / result_name
        image = Image.open(image_path)
        os.remove(image_path)
    return image


def fetch_segmented_image_from_path(image_path: str):
    """
    Gets a segmented image and displays it.
    """
    with Image.open(image_path) as image:
        segmented_image = fetch_segmented_image(image)
        segmented_image.show()


if __name__ == "__main__":
	fire.Fire(fetch_segmented_image_from_path)

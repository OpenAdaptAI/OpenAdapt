"""SoM with Ultralytics FastSam

python -m pip install 'git+https://github.com/facebookresearch/detectron2.git' --no-build-isolation
"""

from pprint import pformat

from loguru import logger
from PIL import Image
import numpy as np

from openadapt import adapters, cache, config, plotting, som, utils, vision


CONTRAST_FACTOR = 10000
DEBUG = False


def main():
    image_file_path = config.ROOT_DIR_PATH / "../tests/assets/excel.png"
    image = Image.open(image_file_path)
    if DEBUG:
        image.show()

    image_contrasted = utils.increase_contrast(image, CONTRAST_FACTOR)
    if DEBUG:
        image_contrasted.show()

    segmentation_adapter = adapters.get_default_segmentation_adapter()
    segmented_image = segmentation_adapter.fetch_segmented_image(image)
    if DEBUG:
        segmented_image.show()

    masks = vision.get_masks_from_segmented_image(segmented_image, sort_by_area=True)
    #refined_masks = vision.refine_masks(masks)

    image_arr = np.asarray(image)

    # https://github.com/microsoft/SoM/blob/main/task_adapter/sam/tasks/inference_sam_m2m_auto.py
    #metadata = MetadataCatalog.get('coco_2017_train_panoptic')
    metadata = None
    visual = som.visualizer.Visualizer(image_arr, metadata=metadata)
    mask_map = np.zeros(image_arr.shape, dtype=np.uint8)
    label_mode = '1'
    alpha = 0.1
    anno_mode = [
        'Mask',
        #'Mark',
    ]
    for i, mask in enumerate(masks):
        label = i + 1
        color_mask = np.random.random((1, 3)).tolist()[0]
        demo = visual.draw_binary_mask_with_number(
            mask,
            text=str(label),
            label_mode=label_mode,
            alpha=alpha,
            anno_mode=anno_mode,
        )
        mask_map[mask == 1] = label

    im = demo.get_image()
    image_som = Image.fromarray(im)
    image_som.show()

    results = []

    prompt_adapter = adapters.get_default_prompt_adapter()
    text = "What are the values of the dates in the leftmost column? What about the horizontal column headings?"
    output = prompt_adapter.prompt(
        text,
        images=[
            # no marks seem to perform just as well as with marks on spreadsheets
            #image_som,
            image,
        ])
    logger.info(output)
    results.append((text, output))

    text = "\n".join([
        f"Consider the dates along the leftmost column and the horizontal column headings:",
        output,
        "What are the values in the corresponding cells?"
    ])
    output = prompt_adapter.prompt(text, images=[image_som])
    logger.info(output)
    results.append((text, output))

    text = "What are the contents of cells A2, B2, and C2?"
    output = prompt_adapter.prompt(text, images=[image_som])
    logger.info(output)
    results.append((text, output))

    logger.info(f"results=\n{pformat(results)}")


if __name__ == "__main__":
    main()

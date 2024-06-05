"""SoM with Ultralytics FastSam"""

from PIL import Image
import numpy as np

from openadapt import adapters, cache, config, plotting, vision, visualizer


CONTRAST_FACTOR = 10000
DEBUG = True


def main():
    image_file_path = config.ROOT_DIR_PATH / "../tests/assets/excel.png"
    image = Image.open(image_file_path)
    if DEBUG:
        image.show()

    image_contrasted = increase_contrast(image, contrast_factor)
    if DEBUG:
        image_contrasted.show()

    segmentation_adapter = adapters.get_default_segmentation_adapter()
    segmented_image = segmentation_adapter.fetch_segmented_image(image)
    if show_images:
        segmented_image.show()

    masks = vision.get_masks_from_segmented_image(segmented_image, sort_by_area=True)
    #refined_masks = vision.refine_masks(masks)

    image_arr = np.asarray(image)

    # https://github.com/microsoft/SoM/blob/main/task_adapter/sam/tasks/inference_sam_m2m_auto.py
    #metadata = MetadataCatalog.get('coco_2017_train_panoptic')
    metadata = None
    visual = Visualizer(image_ori, metadata=metadata)
    mask_map = np.zeros(image_ori.shape, dtype=np.uint8)    
    label_mode = '1'
    alpha = 0.1
    anno_mode = ['Mask']
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
    im.show()


if __name__ == "__main__":
    main()

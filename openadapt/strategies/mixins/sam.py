"""Implements a ReplayStrategy mixin for segmenting images via Segment Anything model.

For more about SAM model, see: https://github.com/facebookresearch/segment-anything

Usage:

    class MyReplayStrategy(SAMReplayStrategyMixin):
        ...

TODO: replace with EfficientSAM for labels and performance:
    https://github.com/IDEA-Research/Grounded-Segment-Anything/tree/main/EfficientSAM
    https://github.com/SysCV/sam-hq
"""

from pathlib import Path
from pprint import pformat
import os
import urllib
import sys

from loguru import logger
from PIL import Image
from segment_anything import (
    SamAutomaticMaskGenerator,
    SamPredictor,
    modeling,
    sam_model_registry,
)
import fire
import matplotlib.axes as axes
import matplotlib.pyplot as plt
import numpy as np

from openadapt.models import Recording, Screenshot
from openadapt.strategies.base import BaseReplayStrategy

CHECKPOINT_URL_BY_NAME = {
    # 42.5 MB
    "vit_tiny": "https://huggingface.co/lkeab/hq-sam/resolve/main/sam_hq_vit_tiny.pth",
    # 379 MB; ~0.01/image
    "vit_b": "https://huggingface.co/lkeab/hq-sam/resolve/main/sam_hq_vit_b.pth?download=true",
    # 1.25 GB; ~1:51/image
    "vit_l": "https://huggingface.co/lkeab/hq-sam/resolve/main/sam_hq_vit_l.pth?download=true",
    # 2.57 GB; ~2min/image
    "vit_h": "https://huggingface.co/lkeab/hq-sam/resolve/main/sam_hq_vit_h.pth?download=true",
}
MODEL_NAME = "vit_b"
CHECKPOINT_DIR_PATH = "./checkpoints"
RESIZE_RATIO = 1
SHOW_PLOTS = True


class SAMReplayStrategyMixin(BaseReplayStrategy):
    """ReplayStrategy mixin for segmenting images via the SAM model."""

    def __init__(
        self,
        recording: Recording,
        model_name: str = MODEL_NAME,
        checkpoint_dir_path: str = CHECKPOINT_DIR_PATH,
    ) -> None:
        """Initialize the SAMReplayStrategyMixin.

        Args:
            recording (Recording): The recording object.
            model_name (str): The name of the SAM model to use. Defaults to MODEL_NAME.
            checkpoint_dir_path (str): The directory path to store SAM model checkpoint.
        """
        super().__init__(recording)
        self.sam_model = initialize_sam_model(model_name, checkpoint_dir_path)
        self.sam_predictor = SamPredictor(self.sam_model)
        self.sam_mask_generator = SamAutomaticMaskGenerator(self.sam_model)

    def get_image_bboxes(
        self,
        image: Image.Image,
        resize_ratio: float = RESIZE_RATIO,
        show_plots: bool = SHOW_PLOTS,
    ) -> str:
        """Retrieve object bounding boxes of screenshot image(XYWH) with resize_ratio.

        Args:
            image (Image.Image): The image to segment.
            resize_ratio (float): The ratio by which to resize the image.
            show_plots (bool): Flag indicating whether to display the plots or not.

        Returns:
            list: list containing the bounding boxes of objects, in XYWH format
        """
        image_resized = resize_image(image, resize_ratio)
        array_resized = np.array(image_resized)
        logger.info("generating masks...")
        masks = self.sam_mask_generator.generate(array_resized)
        logger.info(f"masks=\n{pformat(masks)}")
        if show_plots:
            show_anns(array_resized, masks)
        bboxes = [
            mask["bbox"]
            for mask in masks
        ]
        return bboxes

    def get_click_event_bbox(
        self,
        screenshot: Screenshot,
        resize_ratio: float = RESIZE_RATIO,
        show_plots: bool = SHOW_PLOTS,
    ) -> str:
        """Get bounding box of a clicked object in resized image w/ RESIZE_RATIO(XYWH).

        Args:
            screenshot (models.Screenshot): The screenshot object containing the image.
            resize_ratio (float): The ratio by which to resize the image.
            show_plots: (bool) Flag indicating whether to display the plots or not.

        Returns:
            str: A string representation of a list containing the bounding box
              of clicked object.
            None: If the screenshot does not represent a click event with
              the mouse pressed.

        """
        for action_event in screenshot.action_event:
            if action_event.name in "click" and action_event.mouse_pressed is True:
                logger.info(f"click_action_event=\n{action_event}")
                image_resized = resize_image(screenshot.image, resize_ratio)
                array_resized = np.array(image_resized)

                # Resize mouse coordinates
                resized_mouse_x = int(action_event.mouse_x * RESIZE_RATIO)
                resized_mouse_y = int(action_event.mouse_y * RESIZE_RATIO)
                # Add additional points around the clicked point
                additional_points = [
                    [
                        resized_mouse_x - 1,
                        resized_mouse_y - 1,
                    ],  # Top-left
                    [resized_mouse_x - 1, resized_mouse_y],  # Left
                    [
                        resized_mouse_x - 1,
                        resized_mouse_y + 1,
                    ],  # Bottom-left
                    [resized_mouse_x, resized_mouse_y - 1],  # Top
                    [
                        resized_mouse_x,
                        resized_mouse_y,
                    ],  # Center (clicked point)
                    [resized_mouse_x, resized_mouse_y + 1],  # Bottom
                    [
                        resized_mouse_x + 1,
                        resized_mouse_y - 1,
                    ],  # Top-right
                    [resized_mouse_x + 1, resized_mouse_y],  # Right
                    [
                        resized_mouse_x + 1,
                        resized_mouse_y + 1,
                    ],  # Bottom-right
                ]
                input_point = np.array(additional_points)
                self.sam_predictor.set_image(array_resized)
                input_labels = np.ones(
                    input_point.shape[0]
                )  # Set labels for additional points
                masks, scores, _ = self.sam_predictor.predict(
                    point_coords=input_point,
                    point_labels=input_labels,
                    multimask_output=True,
                )
                best_mask_index = np.argmax(scores)
                best_mask = masks[best_mask_index]
                rows, cols = np.where(best_mask)
                # Calculate bounding box coordinates
                x0 = np.min(cols)
                y0 = np.min(rows)
                x1 = np.max(cols)
                y1 = np.max(rows)
                w = x1 - x0
                h = y1 - y0
                input_box = [x0, y0, w, h]
                if show_plots:
                    plt.figure(figsize=(10, 10))
                    plt.imshow(array_resized)
                    show_mask(best_mask, plt.gca())
                    show_box(input_box, plt.gca())
                    # for point in additional_points :
                    #     show_points(np.array([point]),input_labels,plt.gca())
                    show_points(input_point, input_labels, plt.gca())
                    plt.axis("on")
                    plt.show()
                return input_box
        return []


def resize_image(image: Image, resize_ratio: float) -> Image:
    """Resize the given image.

    Args:
        image (PIL.Image.Image): The image to be resized.
        resize_ratio (float): The ratio by which to resize.

    Returns:
        PIL.Image.Image: The resized image.

    """
    if resize_ratio == 1:
        return image
    logger.info(f"{resize_ratio=} {image.size=}")
    new_size = [int(dim * resize_ratio) for dim in image.size]
    image_resized = image.resize(new_size)
    logger.info(f"{image_resized.size=}")
    return image_resized


def show_mask(mask: np.ndarray, ax: axes.Axes, random_color: bool = False) -> None:
    """Display the mask on the plot.

    Args:
        mask (np.ndarray): The mask array.
        ax: The plot axes.
        random_color (bool): Flag indicating whether to use a random color.
          Defaults to False.
    """
    if random_color:
        color = np.concatenate([np.random.random(3), np.array([0.6])], axis=0)
    else:
        color = np.array([30 / 255, 144 / 255, 255 / 255, 0.6])
    h, w = mask.shape[-2:]
    mask_image = mask.reshape(h, w, 1) * color.reshape(1, 1, -1)
    ax.imshow(mask_image)


def show_points(
    coords: np.ndarray,
    labels: np.ndarray,
    ax: axes.Axes,
    marker_size: int = 120,
) -> None:
    """Display the points on the plot.

    Args:
        coords (np.ndarray): The coordinates of the points.
        labels (np.ndarray): The labels of the points.
        ax: The plot axes.
        marker_size (int): The marker size. Defaults to 120.
    """
    pos_points = coords[labels == 1]
    neg_points = coords[labels == 0]
    ax.scatter(
        pos_points[:, 0],
        pos_points[:, 1],
        color="green",
        marker="*",
        s=marker_size,
        edgecolor="white",
        linewidth=1.25,
    )
    ax.scatter(
        neg_points[:, 0],
        neg_points[:, 1],
        color="red",
        marker="*",
        s=marker_size,
        edgecolor="white",
        linewidth=1.25,
    )


def show_box(box: list[int], ax: axes.Axes) -> None:
    """Display the bounding box on the plot.

    Args:
        box (list[int]): The bounding box coordinates in XYWH format.
        ax: The plot axes.
    """
    x0, y0 = box[0], box[1]
    w, h = box[2], box[3]
    ax.add_patch(
        plt.Rectangle(
            (x0, y0),
            w,
            h,
            edgecolor="green",
            facecolor=(0, 0, 0, 0),
            lw=2,
        )
    )


def show_anns(image_array: np.ndarray, anns: list[dict]) -> None:
    """Display the annotations on the plot.

    Args:
        image_array (np.ndarray): The image to display
        anns (list[dict]): The annotations returned by
            SamAutomaticMaskGenerator.generate()
    """
    if len(anns) == 0:
        return
    logger.info("start")

    plt.figure(figsize=(20, 20))
    plt.imshow(image_array)

    sorted_anns = sorted(anns, key=(lambda x: x["area"]), reverse=True)
    ax = plt.gca()
    ax.set_autoscale_on(False)

    img = np.ones(
        (
            sorted_anns[0]["segmentation"].shape[0],
            sorted_anns[0]["segmentation"].shape[1],
            4,
        )
    )
    img[:, :, 3] = 0
    for ann in sorted_anns:
        m = ann["segmentation"]
        color_mask = np.concatenate([np.random.random(3), [0.35]])
        img[m] = color_mask
    ax.imshow(img)

    plt.axis("off")
    logger.info("show")
    plt.show()


def initialize_sam_model(
    model_name: str,
    checkpoint_dir_path: str,
) -> modeling.Sam:
    """Initialize the SAM model.

    Args:
        model_name (str): The name of the SAM model.
        checkpoint_dir_path (str): The directory path to store SAM model checkpoint.

    Returns:
        segment_anything.modeling.Sam: The initialized SAM model.
    """
    checkpoint_url = CHECKPOINT_URL_BY_NAME[model_name]
    url_parsed = urllib.parse.urlparse(checkpoint_url)
    checkpoint_file_name = os.path.basename(url_parsed.path)
    checkpoint_file_path = Path(checkpoint_dir_path, checkpoint_file_name)
    if not Path.exists(checkpoint_file_path):
        Path(checkpoint_dir_path).mkdir(parents=True, exist_ok=True)
        msg = f"downloading {checkpoint_url} to {checkpoint_file_path}"
        logger.info(msg)
        progress_logger = create_progress_logger(msg)
        urllib.request.urlretrieve(
            checkpoint_url,
            checkpoint_file_path,
            reporthook=progress_logger,
        )
    try:
        return sam_model_registry[model_name](checkpoint=checkpoint_file_path)
    except RuntimeError as exc:
        logger.exception(exc)
        logger.warning(f"{exc=}, retrying...")
        # TODO: sleep with backoff
        logger.info(f"unlinking {checkpoint_file_path=}")
        checkpoint_file_path.unlink()
        return initialize_sam_model(model_name, checkpoint_dir_path)

def create_progress_logger(msg, interval=.1):
    """
    Creates a progress logger function with a specified reporting interval.

    Args:
        interval (float): Every nth % at which to update progress

    Returns:
        callable: function to pass into urllib.request.urlretrieve as reporthook
    """
    last_reported_percent = 0

    def download_progress(block_num, block_size, total_size):
        nonlocal last_reported_percent
        downloaded = block_num * block_size
        if total_size > 0:
            percent = (downloaded / total_size) * 100
            if percent - last_reported_percent >= interval:
                sys.stdout.write(f"\r{percent:.1f}% {msg}")
                sys.stdout.flush()
                last_reported_percent = percent
        else:
            sys.stdout.write(f"\rDownloaded {downloaded} bytes")
            sys.stdout.flush()

    return download_progress


def run_on_image(image_path: str):
    image = Image.open(image_path)
    sam = SAMReplayStrategyMixin(None)
    sam.get_image_bboxes(image)


if __name__ == "__main__":
    fire.Fire(run_on_image)

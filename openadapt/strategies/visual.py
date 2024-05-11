"""Large Multimodal Model with replay instructions.

1. Get active element descriptions

For each processed event, if it is a mouse event:
    - segment the event's active window
    - get a natural language description of each element

2. Modify actions according to replay instructions

Get a natural language description of what the active element should be given the
replay instructions.

3. Replay modified events

For each modified event:

    a. Convert descriptions to coordinates

        If it is a click, scroll, or the last in a sequence of isolated <tab> events:
            - segment the current active window
            - determine the coordinates of the modified active element

    b. Replay modified event

See prompts for details:
- openadapt/prompts/system.j2
- openadapt/prompts/description.j2
- openadapt/prompts/apply_replay_instructions.j2

Todo:
- handle tab sequences
- re-use previous segmentations / descriptions
- handle separate UI elements which look identical (e.g. spreadsheet cells)
    - e.g. include segment mask in prompt
    - e.g. annotate grid positions


Usage:

    $ python -m openadapt.replay VisualReplayStrategy --instructions "<instructions>"
"""

from copy import deepcopy
from dataclasses import dataclass
import time

from loguru import logger
from PIL import Image, ImageDraw
from skimage.metrics import structural_similarity as ssim
import numpy as np

from openadapt import adapters, common, models, strategies, utils, vision

DEBUG = False
DEBUG_REPLAY = False
SEGMENTATIONS = []  # TODO: store to db
MAX_SSIM = 0.9  # threshold for considering an image as similar


@dataclass
class Segmentation:
    """A data class to encapsulate segmentation data of images.

    Attributes:
        image: The original image used to generate segments.
        masked_images: A list of PIL Image objects that have been masked based on
            segmentation.
        descriptions: Descriptions of each segmented region, correlating with each
            image in `masked_images`.
        bounding_boxes: A list of dictionaries containing bounding box
            coordinates for each segmented region.  Each dictionary should have the
            keys "top", "left", "height", and "width" with float values indicating
            the position and size of the box.
        centroids: A list of tuples, each containing the x and y coordinates of the
            centroid of each segmented region.
    """

    image: Image.Image
    masked_images: list[Image.Image]
    descriptions: list[str]
    bounding_boxes: list[dict[str, float]]  # "top", "left", "height", "width"
    centroids: list[tuple[float, float]]


def add_active_segment_descriptions(action_events: list[models.ActionEvent]) -> None:
    """Set the ActionEvent.active_segment_description where appropriate.

    Args:
        action_events: list of ActionEvents to modify in-place.
    """
    for action in action_events:
        # TODO: handle terminal <tab> event
        # if action.name in ("click", "doubleclick", "singleclick", "scroll"):
        if action.name in common.MOUSE_EVENTS:
            window_segmentation = get_window_segmentation(action)
            active_segment_idx = get_active_segment(action, window_segmentation)
            if not active_segment_idx:
                logger.warning(f"{active_segment_idx=}")
                active_segment_description = "(None)"
            else:
                active_segment_description = window_segmentation.descriptions[
                    active_segment_idx
                ]
            action.active_segment_description = active_segment_description
            action.available_segment_descriptions = window_segmentation.descriptions


def apply_replay_instructions(
    action_events: list[models.ActionEvent],
    replay_instructions: str,
) -> None:
    """Modify the given ActionEvents according to the given replay instructions.

    Args:
        action_events: list of action events to be modified in place.
        replay_instructions: instructions for how action events should be modified.
    """
    action_dicts = [get_action_prompt_dict(action) for action in action_events]
    actions_dict = {"actions": action_dicts}
    system_prompt = utils.render_template_from_file(
        "prompts/system.j2",
    )
    prompt = utils.render_template_from_file(
        "prompts/apply_replay_instructions.j2",
        actions=actions_dict,
        replay_instructions=replay_instructions,
    )
    prompt_adapter = adapters.get_default_prompt_adapter()
    content = prompt_adapter.prompt(
        prompt,
        system_prompt,
    )
    content_dict = utils.parse_code_snippet(content)
    try:
        action_dicts = content_dict["actions"]
    except TypeError as exc:
        logger.warning(exc)
        # sometimes OpenAI returns a list of dicts directly, so let it slide
        action_dicts = content_dict
    modified_actions = []
    for action_dict in action_dicts:
        action = models.ActionEvent.from_dict(action_dict)
        modified_actions.append(action)
    return modified_actions


def get_window_prompt_dict(active_window: models.WindowEvent) -> dict:
    """Convert a WindowEvent into a dict, excluding unnecessary properties.

    Args:
        active_window: the WindowEvent to convert

    Returns:
        dictionary containing relevant properties from the given WindowEvent.
    """
    active_window_dict = deepcopy(
        {
            key: val
            for key, val in utils.row2dict(active_window, follow=False).items()
            if val is not None
            and not key.endswith("timestamp")
            and not key.endswith("id")
            # and not isinstance(getattr(models.WindowEvent, key), property)
        }
    )
    active_window_dict["state"].pop("data")
    active_window_dict["state"].pop("meta")
    return active_window_dict


def get_action_prompt_dict(action: models.ActionEvent) -> dict:
    """Convert an ActionEvent into a dict, excluding unnecessary properties.

    Args:
        action: the ActionEvent to convert

    Returns:
        dictionary containing relevant properties from the given ActionEvent.
    """
    action_dict = deepcopy(
        {
            key: val
            for key, val in utils.row2dict(action, follow=False).items()
            if val is not None
            and not key.endswith("timestamp")
            and not key.endswith("id")
            and key not in ["reducer_names"]
            # and not isinstance(getattr(models.ActionEvent, key), property)
        }
    )
    if action.active_segment_description:
        for key in ("mouse_x", "mouse_y", "mouse_dx", "mouse_dy"):
            if key in action_dict:
                del action_dict[key]
    if action.available_segment_descriptions:
        action_dict["available_segment_descriptions"] = (
            action.available_segment_descriptions
        )
    return action_dict


class VisualReplayStrategy(
    strategies.base.BaseReplayStrategy,
):
    """ReplayStrategy using Large Multimodal Model and replay instructions."""

    def __init__(
        self,
        recording: models.Recording,
        instructions: str,
    ) -> None:
        """Initialize the VisualReplayStrategy.

        Args:
            recording (models.Recording): The recording object.
            instructions (str): Natural language instructions for how recording
                should be replayed.
        """
        super().__init__(recording)
        self.recording_action_idx = 0
        add_active_segment_descriptions(recording.processed_action_events)
        self.modified_actions = apply_replay_instructions(
            recording.processed_action_events,
            instructions,
        )
        global DEBUG
        DEBUG = DEBUG_REPLAY

    def get_next_action_event(
        self,
        active_screenshot: models.Screenshot,
        active_window: models.WindowEvent,
    ) -> models.ActionEvent:
        """Get the next ActionEvent for replay.

        Since we have already modified the actions, this function just determines
        the appropriate coordinates for the modified actions (where appropriate).

        Args:
            active_screenshot (models.Screenshot): The active screenshot object.
            active_window (models.WindowEvent): The active window event object.

        Returns:
            models.ActionEvent: The next ActionEvent for replay.
        """
        logger.debug(f"{self.recording_action_idx=}")
        if self.recording_action_idx >= len(self.modified_actions):
            raise StopIteration()

        # TODO: hack
        time.sleep(1)
        active_window = models.WindowEvent.get_active_window_event()

        active_screenshot = models.Screenshot.take_screenshot()
        logger.info(f"{active_window=}")

        modified_reference_action = self.modified_actions[self.recording_action_idx]
        self.recording_action_idx += 1

        if modified_reference_action.name in common.MOUSE_EVENTS:
            modified_reference_action.screenshot = active_screenshot
            modified_reference_action.window_event = active_window
            modified_reference_action.recording = self.recording
            exceptions = []
            while True:
                active_window_segmentation = get_window_segmentation(
                    modified_reference_action,
                    exceptions=exceptions,
                )
                try:
                    target_segment_idx = active_window_segmentation.descriptions.index(
                        modified_reference_action.active_segment_description
                    )
                except ValueError as exc:
                    logger.warning(f"{exc=}")
                    exceptions.append(exc)
                else:
                    break
            target_centroid = active_window_segmentation.centroids[target_segment_idx]
            # <image space position> = scale_ratio * <window/action space position>
            width_ratio, height_ratio = utils.get_scale_ratios(
                modified_reference_action
            )
            target_mouse_x = target_centroid[0] / width_ratio + active_window.left
            target_mouse_y = target_centroid[1] / height_ratio + active_window.top
            modified_reference_action.mouse_x = target_mouse_x
            modified_reference_action.mouse_y = target_mouse_y
        return modified_reference_action


def get_active_segment(
    action: models.ActionEvent,
    window_segmentation: Segmentation,
    debug: bool = DEBUG,
) -> int:
    """Get the index of the bounding box within the action's mouse coordinates.

    Adjust for the scaling of the cropped window and the action coordinates.
    Additionally, visualizes the segments and the mouse position.

    Args:
        action: the ActionEvent
        window_segmentation: the Segmentation
        debug: whether to display images useful in debugging

    Returns:
        index of active segment in Segmentation
    """
    # Obtain the scale ratios
    width_ratio, height_ratio = utils.get_scale_ratios(action)
    logger.info(f"{width_ratio=} {height_ratio=}")

    # Adjust action coordinates to be relative to the cropped window's top-left corner
    adjusted_mouse_x = (action.mouse_x - action.window_event.left) * width_ratio
    adjusted_mouse_y = (action.mouse_y - action.window_event.top) * height_ratio

    active_index = None

    if debug:
        # Create an empty image with enough space to display all bounding boxes
        width = int(
            max(
                box["left"] + box["width"] for box in window_segmentation.bounding_boxes
            )
        )
        height = int(
            max(
                box["top"] + box["height"] for box in window_segmentation.bounding_boxes
            )
        )
        image = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(image)

    for index, box in enumerate(window_segmentation.bounding_boxes):
        box_left = box["left"]
        box_top = box["top"]
        box_right = box["left"] + box["width"]
        box_bottom = box["top"] + box["height"]

        if debug:
            # Draw each bounding box as a rectangle
            draw.rectangle(
                [box_left, box_top, box_right, box_bottom], outline="red", width=1
            )

        # Check if the adjusted action's coordinates are within the bounding box
        if (
            box_left <= adjusted_mouse_x < box_right
            and box_top <= adjusted_mouse_y < box_bottom
        ):
            active_index = index

    if debug:
        # Draw the adjusted mouse position
        draw.ellipse(
            [
                adjusted_mouse_x - 5,
                adjusted_mouse_y - 5,
                adjusted_mouse_x + 5,
                adjusted_mouse_y + 5,
            ],
            fill="blue",
        )
        # Display the image without blocking
        image.show()

    return active_index


def get_image_similarity(im1: Image.Image, im2: Image.Image) -> tuple[float, np.array]:
    """Calculate the structural similarity index (SSIM) between two images.

    This function first resizes the images to a common size maintaining their aspect
    ratios. It then converts the resized images to grayscale and computes the SSIM.

    Args:
        im1 (Image.Image): The first image to compare.
        im2 (Image.Image): The second image to compare.

    Returns:
        tuple[float, np.array]: A tuple containing the SSIM and the difference image.
    """
    # Calculate aspect ratios
    aspect_ratio1 = im1.width / im1.height
    aspect_ratio2 = im2.width / im2.height
    # Use the smaller image as the base for resizing to maintain the aspect ratio
    if aspect_ratio1 < aspect_ratio2:
        base_width = min(im1.width, im2.width)
        base_height = int(base_width / aspect_ratio1)
    else:
        base_height = min(im1.height, im2.height)
        base_width = int(base_height * aspect_ratio2)

    # Resize images to a common base while maintaining aspect ratio
    im1 = im1.resize((base_width, base_height), Image.LANCZOS)
    im2 = im2.resize((base_width, base_height), Image.LANCZOS)

    # Convert images to grayscale
    im1_gray = np.array(im1.convert("L"))
    im2_gray = np.array(im2.convert("L"))

    data_range = im2_gray.max() - im2_gray.min()
    mssim, diff_image = ssim(im1_gray, im2_gray, data_range=data_range, full=True)

    return mssim, diff_image


def find_similar_image_segmentation(
    image: Image.Image,
    max_ssim: float = MAX_SSIM,
) -> tuple[Segmentation, np.ndarray] | tuple[None, None]:
    """Identify a similar image in the cache based on the SSIM comparison.

    This function iterates through a global list of image segmentations,
    comparing each against a given image using the SSIM index calculated by
    get_image_similarity.
    It logs and updates the best match found above a specified SSIM threshold.

    Args:
        image (Image.Image): The image to compare against the cache.
        max_ssim (float): The minimum SSIM threshold for considering a match.

    Returns:
        tuple[Segmentation, np.ndarray] | tuple[None, None]: The best matching
        segmentation and its difference image if a match is found;
        otherwise, None for both.
    """
    similar_segmentation = None
    similar_segmentation_diff = None

    for segmentation in SEGMENTATIONS:
        similarity_index, ssim_image = get_image_similarity(image, segmentation.image)
        if similarity_index > max_ssim:
            logger.info(f"{similarity_index=}")
            max_ssim = similarity_index
            similar_segmentation = segmentation
            similar_segmentation_diff = ssim_image

    return similar_segmentation, similar_segmentation_diff


def get_window_segmentation(
    action_event: models.ActionEvent,
    exceptions: list[Exception] | None = None,
) -> Segmentation:
    """Segments the active window from the action event's screenshot.

    Args:
        action_event: action event containing the screenshot data.
        exceptions: list of exceptions previously raised, added to prompt.

    Returns:
        Segmentation object containing detailed segmentation information.
    """
    screenshot = action_event.screenshot
    original_image = screenshot.cropped_image
    if DEBUG:
        original_image.show()

    similar_segmentation, similar_segmentation_diff = find_similar_image_segmentation(
        original_image,
    )
    if similar_segmentation:
        # TODO XXX: create copy of similar_segmentation, but overwrite with segments of
        # regions of new image where segments of similar_segmentation overlap non-zero
        # regions of similar_segmentation_diff
        return similar_segmentation

    segmentation_adapter = adapters.get_default_segmentation_adapter()
    segmented_image = segmentation_adapter.fetch_segmented_image(original_image)
    if DEBUG:
        segmented_image.show()

    masks = vision.process_image_for_masks(segmented_image)
    if DEBUG:
        vision.display_binary_images_grid(masks)

    refined_masks = vision.refine_masks(masks)
    if DEBUG:
        vision.display_binary_images_grid(refined_masks)

    masked_images = vision.extract_masked_images(original_image, refined_masks)

    original_image_base64 = screenshot.base64
    masked_images_base64 = [
        utils.image2utf8(masked_image) for masked_image in masked_images
    ]
    descriptions = prompt_for_descriptions(
        original_image_base64,
        masked_images_base64,
        action_event.active_segment_description,
        exceptions,
    )
    bounding_boxes, centroids = vision.calculate_bounding_boxes(refined_masks)
    assert len(bounding_boxes) == len(descriptions) == len(centroids), (
        len(bounding_boxes),
        len(descriptions),
        len(centroids),
    )
    segmentation = Segmentation(
        original_image,
        masked_images,
        descriptions,
        bounding_boxes,
        centroids,
    )
    if DEBUG:
        vision.display_images_table_with_titles(masked_images, descriptions)

    SEGMENTATIONS.append(segmentation)
    return segmentation


def prompt_for_descriptions(
    original_image_base64: str,
    masked_images_base64: list[str],
    active_segment_description: str | None,
    exceptions: list[Exception] | None = None,
) -> list[str]:
    """Generates descriptions for given image segments using a prompt adapter.

    Args:
        original_image_base64: Base64 encoding of the original image.
        masked_images_base64: List of base64 encoded masked images.
        active_segment_description: Description of the active segment.
        exceptions: List of exceptions previously raised, added to prompts.

    Returns:
        list of descriptions for each masked image.
    """
    prompt_adapter = adapters.get_default_prompt_adapter()

    # TODO: move inside adapters
    # off by one to account for original image
    if prompt_adapter.MAX_IMAGES and (
        len(masked_images_base64) + 1 > prompt_adapter.MAX_IMAGES
    ):
        masked_images_base64_batches = utils.split_list(
            masked_images_base64,
            prompt_adapter.MAX_IMAGES - 1,
        )
        descriptions = []
        for masked_images_base64_batch in masked_images_base64_batches:
            descriptions_batch = prompt_for_descriptions(
                original_image_base64,
                masked_images_base64_batch,
                active_segment_description,
                exceptions,
            )
            descriptions += descriptions_batch
        return descriptions

    images = [original_image_base64] + masked_images_base64
    system_prompt = utils.render_template_from_file(
        "prompts/system.j2",
    )
    logger.info(f"system_prompt=\n{system_prompt}")
    num_segments = len(masked_images_base64)
    prompt = utils.render_template_from_file(
        "prompts/description.j2",
        active_segment_description=active_segment_description,
        num_segments=num_segments,
        exceptions=exceptions,
    )
    logger.info(f"prompt=\n{prompt}")
    descriptions_json = prompt_adapter.prompt(
        prompt,
        system_prompt,
        images,
    )
    descriptions = utils.parse_code_snippet(descriptions_json)["descriptions"]
    logger.info(f"{descriptions=}")
    try:
        assert len(descriptions) == len(masked_images_base64), (
            len(descriptions),
            len(masked_images_base64),
        )
    except Exception as exc:
        # TODO XXX
        raise exc
    # remove indexes
    descriptions = [desc for idx, desc in descriptions]
    return descriptions

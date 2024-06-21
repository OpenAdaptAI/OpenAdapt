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
- re-use individual segments of previous segmentations
- handle distinct segments which look identical (e.g. spreadsheet cells)
    - e.g. include segment mask in prompt
    - e.g. annotate grid positions
    - e.g. describe relative positions
- update actions during replay
- handle API failures


Usage:

    $ python -m openadapt.replay VisualReplayStrategy --instructions "<instructions>"
"""

from dataclasses import dataclass
from pprint import pformat
import time
from turtle import title
from loguru import logger
from PIL import Image, ImageDraw
import numpy as np
from openadapt import adapters, common, models, plotting, strategies, utils, vision

DEBUG = False
DEBUG_REPLAY = False
SEGMENTATIONS = []  # TODO: store to db
MIN_SCREENSHOT_SSIM = 0.9  # threshold for considering screenshots structurally similar
MIN_SEGMENT_SSIM = 0.95  # threshold for considering segments structurally similar
MIN_SEGMENT_SIZE_SIM = 0  # threshold for considering segment sizes similar


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
    action_dicts = [action.to_prompt_dict() for action in action_events]
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
        # TODO: make this less of a hack
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
    """Get the index of the bounding box containing the action's mouse coordinates.

    Adjust for the scaling of the cropped window and the action coordinates.
    Optionally visualize segments and mouse position.

    Args:
        action: the ActionEvent
        window_segmentation: the Segmentation
        debug: whether to display images for debugging

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


def find_similar_image_segmentation(
    image: Image.Image,
    min_ssim: float = MIN_SCREENSHOT_SSIM,
) -> tuple[Segmentation, np.ndarray] | tuple[None, None]:
    """Identify a similar image in the cache based on the SSIM comparison.

    This function iterates through a global list of image segmentations,
    comparing each against a given image using the SSIM index calculated by
    get_image_similarity.
    It logs and updates the best match found above a specified SSIM threshold.

    Args:
        image (Image.Image): The image to compare against the cache.
        min_ssim (float): The minimum SSIM threshold for considering a match.

    Returns:
        tuple[Segmentation, np.ndarray] | tuple[None, None]: The best matching
        segmentation and its difference image if a match is found;
        otherwise, None for both.
    """
    similar_segmentation = None
    similar_segmentation_diff = None

    for segmentation in SEGMENTATIONS:
        similarity_index, ssim_image = vision.get_image_similarity(
            image,
            segmentation.image,
        )
        if similarity_index > min_ssim:
            logger.info(f"{similarity_index=}")
            min_ssim = similarity_index
            similar_segmentation = segmentation
            similar_segmentation_diff = ssim_image

    return similar_segmentation, similar_segmentation_diff

 
def combine_segmentations(
    new_image: Image.Image,
    previous_segmentation: Segmentation,
    new_descriptions: list[str],
    new_masked_images: list[Image.Image],
    new_masks: list[np.ndarray]
) -> Segmentation:
    """Combine the previous segmentation with the new segmentation of the differences.

    Args:
        new_image: The new image which includes the changes.
        previous_segmentation: The previous segmentation containing unchanged segments.
        new_descriptions: Descriptions of the new segments from the difference image.
        new_masked_images: Masked images of the new segments from the difference image.
        new_masks: masks of the new segments.

    Returns:
        Segmentation: A new segmentation combining both previous and new segments.
    """
    def masks_overlap(mask1, mask2):
        """Check if two masks overlap."""
        return np.any(np.logical_and(mask1, mask2))

    # Calculate the bounding boxes and centroids for the new segments
    new_bounding_boxes, new_centroids = vision.calculate_bounding_boxes(new_masks)

    # Filter out overlapping previous segments
    filtered_previous_masked_images = []
    filtered_previous_descriptions = []
    filtered_previous_bounding_boxes = []
    filtered_previous_centroids = []
    for idx, prev_mask in enumerate(previous_segmentation.masks):
        if not any(masks_overlap(prev_mask, new_mask) for new_mask in new_masks):
            filtered_previous_masked_images.append(previous_segmentation.masked_images[idx])
            filtered_previous_descriptions.append(previous_segmentation.descriptions[idx])
            filtered_previous_bounding_boxes.append(previous_segmentation.bounding_boxes[idx])
            filtered_previous_centroids.append(previous_segmentation.centroids[idx])

    # Combine filtered previous segments with new segments
    combined_masked_images = filtered_previous_masked_images + new_masked_images
    combined_descriptions = filtered_previous_descriptions + new_descriptions
    combined_bounding_boxes = filtered_previous_bounding_boxes + new_bounding_boxes
    combined_centroids = filtered_previous_centroids + new_centroids

    return Segmentation(
        image=new_image,
        masked_images=combined_masked_images,
        descriptions=combined_descriptions,
        bounding_boxes=combined_bounding_boxes,
        centroids=combined_centroids
    )


def get_window_segmentation(
    action_event: models.ActionEvent,
    exceptions: list[Exception] | None = None,
    handle_similar_image_groups: bool = False,
) -> Segmentation:
    """Segments the active window from the action event's screenshot.

    Args:
        action_event: action event containing the screenshot data.
        exceptions: list of exceptions previously raised, added to prompt.
        handle_similar_image_groups (bool): Whether to distinguish between similar
            image groups. Work-in-progress.

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
        difference_image = vision.extract_difference_image(
            original_image,
            similar_segmentation.image,
            tolerance=0.05,
        )
        new_masks = vision.get_masks_from_segmented_image(difference_image)
        new_masked_images = vision.extract_masked_images(difference_image, new_masks)
        new_descriptions = prompt_for_descriptions(
            difference_image,
            new_masked_images,
            action_event.active_segment_description,
            exceptions,
        )
        updated_segmentation = combine_segmentations(
            difference_image,
            similar_segmentation,
            new_descriptions,
            new_masked_images,
            new_masks,
        )
        similar_segmentation = updated_segmentation
        return similar_segmentation

    segmentation_adapter = adapters.get_default_segmentation_adapter()
    segmented_image = segmentation_adapter.fetch_segmented_image(original_image)
    if DEBUG:
        segmented_image.show()

    masks = vision.get_masks_from_segmented_image(segmented_image)
    if DEBUG:
        plotting.display_binary_images_grid(masks)

    refined_masks = vision.refine_masks(masks)
    if DEBUG:
        plotting.display_binary_images_grid(refined_masks)

    masked_images = vision.extract_masked_images(original_image, refined_masks)

    if handle_similar_image_groups:
        similar_idx_groups, ungrouped_idxs, _, _ = vision.get_similar_image_idxs(
            masked_images,
            MIN_SEGMENT_SSIM,
            MIN_SEGMENT_SIZE_SIM,
        )
        # TODO XXX: handle similar image groups
        raise ValueError("Currently unsupported.")

    descriptions = prompt_for_descriptions(
        original_image,
        masked_images,
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
        plotting.display_images_table_with_titles(masked_images, descriptions)

    SEGMENTATIONS.append(segmentation)
    return segmentation


def prompt_for_descriptions(
    original_image: Image.Image,
    masked_images: list[Image.Image],
    active_segment_description: str | None,
    exceptions: list[Exception] | None = None,
) -> list[str]:
    """Generates descriptions for given image segments using a prompt adapter.

    Args:
        original_image: The original image.
        masked_images: List of masked images.
        active_segment_description: Description of the active segment.
        exceptions: List of exceptions previously raised, added to prompts.

    Returns:
        list of descriptions for each masked image.
    """
    prompt_adapter = adapters.get_default_prompt_adapter()

    # TODO: move inside adapters
    # off by one to account for original image
    if prompt_adapter.MAX_IMAGES and (
        len(masked_images) + 1 > prompt_adapter.MAX_IMAGES
    ):
        masked_images_batches = utils.split_list(
            masked_images,
            prompt_adapter.MAX_IMAGES - 1,
        )
        descriptions = []
        for masked_images_batch in masked_images_batches:
            descriptions_batch = prompt_for_descriptions(
                original_image,
                masked_images_batch,
                active_segment_description,
                exceptions,
            )
            descriptions += descriptions_batch
        return descriptions

    images = [original_image] + masked_images
    system_prompt = utils.render_template_from_file(
        "prompts/system.j2",
    )
    logger.info(f"system_prompt=\n{system_prompt}")
    num_segments = len(masked_images)
    prompt = utils.render_template_from_file(
        "prompts/description.j2",
        active_segment_description=active_segment_description,
        num_segments=num_segments,
        exceptions=exceptions,
    )
    logger.info(f"prompt=\n{prompt}")
    logger.info(f"{len(images)=}")
    descriptions_json = prompt_adapter.prompt(
        prompt,
        system_prompt,
        images,
    )
    descriptions = utils.parse_code_snippet(descriptions_json)["descriptions"]
    logger.info(f"{descriptions=}")
    try:
        assert len(descriptions) == len(masked_images), (
            len(descriptions),
            len(masked_images),
        )
    except Exception as exc:
        exceptions = exceptions or []
        exceptions.append(exc)
        logger.info(f"exceptions=\n{pformat(exceptions)}")
        return prompt_for_descriptions(
            original_image,
            masked_images,
            active_segment_description,
            exceptions,
        )

    # remove indexes
    descriptions = [desc for idx, desc in descriptions]
    return descriptions

#Example usage for visualizing
image_1 = Image.open("./winCalOld.png")
image_2 = Image.open("./winCalNew.png")

difference_image = vision.extract_difference_image(image_1, image_2, tolerance=0.05)
difference_image.show(title="difference Image")
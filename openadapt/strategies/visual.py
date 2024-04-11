"""Large Multimodal Model with replay instructions.

TODO:
- handle tab sequences
- re-use previous segmentations / descriptions
- handle separate UI elements which look identical (e.g. spreadsheet cells)
    - e.g. include segment mask in prompt
    - e.g. annotate grid positions

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

Usage:

    $ python -m openadapt.replay VisualReplayStrategy --instructions "<instructions>"
"""

from copy import deepcopy
from dataclasses import dataclass
from pprint import pformat

from loguru import logger
from PIL import Image
import deepdiff
import json

from openadapt import common, config, models, strategies, utils, vision
from openadapt import adapters


DEBUG = True
DEBUG_REPLAY = False


@dataclass
class Segmentation:
    masked_images: list[Image.Image]
    descriptions: list[str]
    bounding_boxes: list[dict[str, float]]  # "top", "left", "height", "width"
    centroids: list[tuple[float, float]]


def add_active_segment_descriptions(
    action_events: list[models.ActionEvent]
) -> None:
    """Set the ActionEvent.active_segment_description where appropriate"""

    logical_events = []
    for action in action_events:
        # TODO: handle terminal <tab> event
        #if action.name in ("click", "doubleclick", "singleclick", "scroll"):
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
    """TODO"""

    action_dicts = [
        get_action_prompt_dict(action)
        for action in action_events
    ]
    actions_dict = {
        "actions": action_dicts
    }
    system_prompt = utils.render_template_from_file(
        "openadapt/prompts/system.j2",
    )
    prompt = utils.render_template_from_file(
        "openadapt/prompts/apply_replay_instructions.j2",
        actions=actions_dict,
        replay_instructions=replay_instructions,
    )
    prompt_adapter = get_default_prompt_adapter()
    content = prompt_adapter.prompt(
        prompt,
        system_prompt,
    )
    content_dict = utils.parse_code_snippet(content)
    action_dicts = content_dict["actions"]
    modified_actions = []
    for action_dict in action_dicts:
        action = models.ActionEvent.from_dict(action_dict)
        modified_actions.append(action)
    return modified_actions


def get_window_prompt_dict(active_window: models.WindowEvent) -> dict:
    """TODO"""
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
    """TODO"""
    action_dict = deepcopy(
        {
            key: val
            for key, val in utils.row2dict(
                action, follow=False
            ).items()
            if val is not None
            and not key.endswith("timestamp")
            and not key.endswith("id")
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
    """ReplayStrategy using Large Multimodal Model"""

    def __init__(
        self,
        recording: models.Recording,
        replay_instructions: str,
    ) -> None:
        """Initialize the VisualReplayStrategy.

        Args:
            recording (models.Recording): The recording object.
            replay_instructions (str): Natural language instructions for how recording
                should be replayed.
        """
        super().__init__(recording)
        self.recording_action_idx = 0
        add_active_segment_descriptions(recording.processed_action_events)
        self.modified_actions = apply_replay_instructions(
            recording.processed_action_events, replay_instructions,
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
        if self.recording_action_idx == len(self.recording.processed_action_events):
            raise StopIteration()

        try:
            reference_action = self.recording.processed_action_events[
                self.recording_action_idx
            ]
        except Exception as exc:
            logger.warning(exc)
            raise StopIteration()
        reference_window = reference_action.window_event

        # TODO XXX HACK
        while (
            ref_win_title_prefix := reference_window.title.split(" ")[0]
        ) != (
            active_win_title_prefix := active_window.title.split(" ")[0]
        ):
            logger.warning(f"{ref_win_title_prefix=} != {active_win_title_prefix=}")
            import time
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
                    logger.warning("{exc=}")
                    excs.append(exc)
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


def get_active_segment(action, window_segmentation, debug=DEBUG):
    """
    Returns the index of the bounding box within which the action's mouse coordinates
    fall, adjusted for the scaling of the cropped window and the action coordinates.
    Additionally, visualizes the segments and the mouse position.
    """
    # Obtain the scale ratios
    width_ratio, height_ratio = utils.get_scale_ratios(action)
    logger.info(f"{width_ratio=} {height_ratio=}")

    # Adjust action coordinates to be relative to the cropped window's top-left corner
    adjusted_mouse_x = (action.mouse_x - action.window_event.left) * width_ratio
    adjusted_mouse_y = (action.mouse_y - action.window_event.top) * height_ratio

    # Setup plot
    if debug:
        import matplotlib.patches as patches
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots()

    active_index = None

    for index, box in enumerate(window_segmentation.bounding_boxes):
        box_left = box['left']
        box_top = box['top']
        box_right = (box['left'] + box['width'])
        box_bottom = (box['top'] + box['height'])

        if debug:
            # Plot each bounding box as a rectangle
            rect = patches.Rectangle(
                (box_left, box_top),
                box_right - box_left,
                box_bottom - box_top,
                linewidth=1,
                edgecolor='r',
                facecolor='none',
            )
            ax.add_patch(rect)

        # Check if the adjusted action's coordinates are within the bounding box
        if (
            box_left <= adjusted_mouse_x < box_right
        ) and (
            box_top <= adjusted_mouse_y < box_bottom
        ):
            active_index = index

    if debug:
        # Plot the adjusted mouse position
        plt.plot(adjusted_mouse_x, adjusted_mouse_y, 'bo')  # 'bo' creates a blue dot

        # Set plot limits and labels for clarity
        plt.xlim(0, max([
            box['left'] + box['width'] for box in window_segmentation.bounding_boxes
        ]))
        plt.ylim(0, max([
            box['top'] + box['height'] for box in window_segmentation.bounding_boxes
        ]))
        plt.gca().invert_yaxis()  # Invert y-axis to match coordinate system
        plt.xlabel('X coordinate')
        plt.ylabel('Y coordinate')
        plt.title('Bounding Boxes and Mouse Position')

        plt.show()

    return active_index


def get_window_segmentation(
    action_event: models.ActionEvent,
    exceptions: list[Exception] | None = None,
) -> Segmentation:
    screenshot = action_event.screenshot
    screenshot.crop_active_window(action_event)
    original_image = screenshot.image
    if DEBUG:
        original_image.show()
    segmentation_adapter = get_default_segmentation_adapter()
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
        utils.image2utf8(masked_image)
        for masked_image in masked_images
    ]
    descriptions = prompt_for_descriptions(
        original_image_base64,
        masked_images_base64,
        action_event.active_segment_description,
        exceptions,
    )
    bounding_boxes, centroids = vision.calculate_bounding_boxes(refined_masks)
    assert len(bounding_boxes) == len(descriptions) == len(centroids), (
        len(bounding_boxes), len(descriptions), len(centroids),
    )
    segmentation = Segmentation(masked_images, descriptions, bounding_boxes, centroids)
    if DEBUG:
        vision.display_images_table_with_titles(masked_images, descriptions)
        import ipdb; ipdb.set_trace()
    return segmentation


def get_default_prompt_adapter():
    return {
        "openai": adapters.openai,
        "anthropic": adapters.anthropic,
        "google": adapters.google,
    }[config.DEFAULT_ADAPTER]


def get_default_segmentation_adapter():
    return {
        "som": adapters.som,
        "replicate": adapters.replicate,
        "ultralytics": adapters.ultralytics,
    }[config.DEFAULT_SEGMENTATION_ADAPTER]


def prompt_for_descriptions(
    original_image_base64: str,
    masked_images_base64: list[str],
    active_segment_description: str | None,
    exceptions: list[Exception] | None = None,
) -> list[str]:
    prompt_adapter = get_default_prompt_adapter()

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
        "openadapt/prompts/system.j2",
    )
    logger.info(f"system_prompt=\n{system_prompt}")
    num_segments = len(masked_images_base64)
    prompt = utils.render_template_from_file(
        "openadapt/prompts/description.j2",
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
            len(descriptions), len(masked_images_base64)
        )
    except Exception as exc:
        # TODO XXX
        logger.error(exc)
        import ipdb; ipdb.set_trace()
        foo = 1
    # remove indexes
    descriptions = [desc for idx, desc in descriptions]
    return descriptions

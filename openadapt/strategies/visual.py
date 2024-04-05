"""Large Multimodal Model with replay instructions.

1. Get active element descriptions

For each processed event, if it is a click, scroll, or the last in a sequence of
isolated <tab> events:
    - segment the event's active window
    - get a natural language description of the active element

2. Modify actions according to replay instructions

Get a natural language description of what the active element should be given the
replay instructions.

2. Replay modified events

For each modified event:

    a. Convert descriptions to coordinates

        If it is a click, scroll, or the last in a sequence of isolated <tab> events:
            - segment the current active window
            - determine the coordinates of the modified active element

    b. Replay modified event

See openadapt/prompts/system.j2 and openadapt/prompts/action.j2 for details.

Usage:

    $ python -m openadapt.replay VisualReplayStrategy --instructions "<replay instructions>"
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


DEBUG = False
MAX_TOKENS = 2**14  # 16384


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
        if action.name in ("click", "doubleclick", "singleclick", "scroll"):
            window_segmentation = get_window_segmentation(action)
            active_segment_idx = get_active_segment(action, window_segmentation)
            if not active_segment_idx:
                logger.warning(f"{active_segment_idx=}")
                active_segment_description = "(None)"
            else:
                active_segment_description = window_segmentation.descriptions[active_segment_idx]
            action.active_segment_description = active_segment_description
            action.available_segment_descriptions = window_segmentation.descriptions


def apply_replay_instructions(
    action_events: list[models.ActionEvent],
    replay_instructions: str,
    max_tokens: int | None = MAX_TOKENS,
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
    import ipdb; ipdb.set_trace()
    prompt_adapter = get_default_prompt_adapter()
    content = prompt_adapter.prompt(
        prompt,
        system_prompt,
        max_tokens=max_tokens,
    )
    import ipdb; ipdb.set_trace()
    content = utils.parse_code_snippet(content)
    actions = models.ActionEvent.from_dict(content)


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
        action_dict["available_segment_descriptions"] = action.available_segment_descriptions
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
        apply_replay_instructions(
            recording.processed_action_events, replay_instructions,
        )

    def get_next_action_event(
        self,
        active_screenshot: models.Screenshot,
        active_window: models.WindowEvent,
    ) -> models.ActionEvent:
        """Get the next ActionEvent for replay.

        Args:
            active_screenshot (models.Screenshot): The active screenshot object.
            active_window (models.WindowEvent): The active window event object.

        Returns:
            models.ActionEvent: The next ActionEvent for replay.
        """
        logger.debug(f"{self.recording_action_idx=}")
        if self.recording_action_idx == len(self.recording.processed_action_events):
            raise StopIteration()

        active_window_dict = get_window_prompt_dict(active_window)

        actions = self.recording.processed_action_events
        prev_window_title = None
        prompt_frames = []
        active_action = None
        active_action_dict = None
        num_images = 0
        screenshots = []
        #screenshots_by_window_title = get_screenshots_by_window_title(actions)
        for action_idx, action in enumerate(actions):
            window_dict = get_window_prompt_dict(action.window_event)
            window_title = window_dict["title"]
            if prev_window_title is None or prev_window_title != window_title:
                screenshot = action.screenshot
                num_images += 1
                screenshots.append(screenshot)
            prev_window_title = window_title

            action_dict = get_action_prompt_dict(action)
            is_reference_action = action_idx == self.recording_action_idx
            logger.info(f"{is_reference_action=}")
            if is_reference_action:
                reference_action = action
                reference_action_dict = action_dict

            active_segment_description = None
            active_segment_bounding_box = None
            if action.name in ("click", "doubleclick", "singleclick"):
                if SEGMENT_ONCE_PER_WINDOW_TITLE and window_title in self.window_segmentation_by_title:
                    window_segmentation = self.window_segmentation_by_title[window_title]
                else:
                    window_segmentation = get_window_segmentation(action)
                masked_images = window_segmentation.masked_images
                descriptions = window_segmentation.descriptions
                bounding_boxes = window_segmentation.bounding_boxes
                active_segment_idx = get_active_segment(action, window_segmentation)
                if active_segment_idx:
                    active_segment_description = window_segmentation.descriptions[active_segment_idx]
                    active_segment_bounding_box = window_segmentation.bounding_boxes[active_segment_idx]
                else:
                    logger.warning(f"{active_segment_idx=} {action=}")

            prompt_frame = {
                "screenshot_number": num_images,
                "window": window_dict,
                "action": action_dict,
                "action_idx": action_idx,
                "action_number": action_idx + 1,
                "is_reference_action": is_reference_action,
                "active_segment_description": active_segment_description,
                "active_segment_bounding_box": active_segment_bounding_box,
            }
            prompt_frames.append(prompt_frame)
        logger.info(f"prompt_frames=\n{pformat(prompt_frames)}")

        active_segmentation = None
        if reference_action.name in common.MOUSE_EVENTS:
            active_segmentation = get_window_segmentation(
                screenshot=active_screenshot,
                window_event=actions[0].window_event,
            )

        # TODO: replace screenshots with window segmentations?

        screenshots.append(active_screenshot)
        screenshots_base64 = [
            screenshot.base64
            for screenshot in screenshots
        ]
        prompt_data = {
            "prompt_frames": prompt_frames,
            "active_window": active_window_dict,
            "reference_action": reference_action_dict,
            "replay_instructions": replay_instructions,
            "task_description": self.recording.task_description,
            "screenshots_base64": screenshots_base64,
            "active_segmentation": active_segmentation,
        }
        completion = prompt_for_action(prompt_data)

        logger.info(f"{completion=}")
        #import ipdb; ipdb.set_trace()

        #active_action_dicts = utils.get_action_dict_from_json(completion)
        active_action_dicts = completion
        logger.debug(f"active_action_dicts=\n{pformat(active_action_dicts)}")
        active_action = models.ActionEvent.from_dict(active_action_dicts)
        self.recording_action_idx += 1
        return active_action


def get_active_segment(action, window_segmentation, debug=DEBUG):
    """
    Returns the index of the bounding box within which the action's mouse coordinates fall,
    adjusted for the scaling of the cropped window and the action coordinates.
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
            rect = patches.Rectangle((box_left, box_top), box_right - box_left, box_bottom - box_top, linewidth=1, edgecolor='r', facecolor='none')
            ax.add_patch(rect)

        # Check if the adjusted action's coordinates are within the bounding box
        if box_left <= adjusted_mouse_x < box_right and box_top <= adjusted_mouse_y < box_bottom:
            active_index = index

    if debug:
        # Plot the adjusted mouse position
        plt.plot(adjusted_mouse_x, adjusted_mouse_y, 'bo')  # 'bo' creates a blue dot

        # Set plot limits and labels for clarity
        plt.xlim(0, max([box['left'] + box['width'] for box in window_segmentation.bounding_boxes]))
        plt.ylim(0, max([box['top'] + box['height'] for box in window_segmentation.bounding_boxes]))
        plt.gca().invert_yaxis()  # Invert y-axis to match coordinate system
        plt.xlabel('X coordinate')
        plt.ylabel('Y coordinate')
        plt.title('Bounding Boxes and Mouse Position')

        plt.show()

    return active_index

def _get_active_segment(action: models.ActionEvent, window_segmentation: Segmentation) -> int | None:
    """
    Returns the index of the bounding box within which the action's mouse coordinates fall,
    adjusted for the scaling of the cropped window and the action coordinates.
    """
    # Obtain the scale ratios
    width_ratio, height_ratio = utils.get_scale_ratios(action)

    # Adjust action coordinates to be relative to the cropped window's top-left corner
    adjusted_mouse_x = (action.mouse_x - action.window_event.left) * width_ratio
    adjusted_mouse_y = (action.mouse_y - action.window_event.top) * height_ratio

    for index, box in enumerate(window_segmentation.bounding_boxes):
        box_left = box['left']# * width_ratio
        box_top = box['top']# * height_ratio
        box_right = (box['left'] + box['width'])# * width_ratio
        box_bottom = (box['top'] + box['height'])# * height_ratio

        # Check if the adjusted action's coordinates are within the bounding box
        if box_left <= adjusted_mouse_x < box_right and box_top <= adjusted_mouse_y < box_bottom:
            return index

    return None

def get_window_segmentation(
    action_event: models.ActionEvent | None = None,
    screenshot: models.Screenshot | None = None,
    window_event: models.WindowEvent | None = None,
    # TODO
    #active_element_only: bool = True,
) -> Segmentation:
    assert action_event or (screenshot and window_event)
    if action_event:
        screenshot = action_event.screenshot
        screenshot.crop_active_window(action_event)
    else:
        width_ratio, height_ratio = utils.get_scale_ratios(window_event.action_events[0])
        screenshot.crop_active_window(
            window_event=window_event, 
            width_ratio=width_ratio,
            height_ratio=height_ratio,
        )
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
        original_image_base64, masked_images_base64,
    )
    assert len(descriptions) == len(masked_images), (
        len(descriptions), len(masked_images)
    )
    bounding_boxes, centroids = vision.calculate_bounding_boxes(refined_masks)
    assert len(bounding_boxes) == len(descriptions) == len(centroids), (
        len(bounding_boxes), len(descriptions), len(centroids),
    )
    segmentation = Segmentation(masked_images, descriptions, bounding_boxes, centroids)
    if DEBUG:
        vision.display_images_table_with_titles(masked_images, descriptions)
        #import ipdb; ipdb.set_trace()
    return segmentation

#def get_screenshots_by_window_title(
#    actions: list[models.ActionEvent],
#) -> dict[str, models.Screenshot]:
#    screenshots_by_window_title = {}
#    for action_idx, action in enumerate(actions):
#        window = action.window_event
#        window_title = window.title
#        screenshot = action.screenshot
#        screenshots_by_window_title.setdefault(window_title, [])
#        screenshots_by_window_title[window_title].append(screenshot)
#    return screenshots_by_window_title


def get_default_prompt_adapter():
    return {
        "openai": adapters.openai,
        "anthropic": adapters.anthropic,
    }[config.DEFAULT_ADAPTER]


def get_default_segmentation_adapter():
    return {
        "som": adapters.som,
        "replicate": adapters.replicate,
    }[config.DEFAULT_SEGMENTATION_ADAPTER]


from typing import Callable

# modified from https://github.com/OpenAdaptAI/OpenAdapt/pull/560/files
def prompt_for_action(
    prompt_data: dict,
    max_tokens: int | None = MAX_TOKENS,
    prompt_adapter: Callable = get_default_prompt_adapter(),
) -> dict:
    prompt_frames = prompt_data["prompt_frames"]
    active_window = prompt_data["active_window"]
    reference_action = prompt_data["reference_action"]
    replay_instructions = prompt_data["replay_instructions"]
    task_description = prompt_data["task_description"]
    images = prompt_data["screenshots_base64"]

    num_actions = len(prompt_frames)

    num_images = len(images)
    system_prompt = utils.render_template_from_file(
        "openadapt/prompts/system.j2",
    )
    logger.info(f"system_prompt=\n{system_prompt}")

    prompt = utils.render_template_from_file(
        "openadapt/prompts/action.j2",
        prompt_frames=prompt_frames,
        active_window=active_window,
        reference_action=reference_action,
        replay_instructions=replay_instructions,
        task_description=task_description,
        num_actions=num_actions,
        num_images=num_images,
    )
    logger.info(f"prompt=\n{prompt}")
    if reference_action['name'] in common.MOUSE_EVENTS:
        import ipdb; ipdb.set_trace()
        foo = 1
    content = prompt_adapter.prompt(
        prompt,
        system_prompt,
        images,
        max_tokens=max_tokens,
        detail="low",
    )
    try:
        content_dict = utils.parse_code_snippet(content)
    except Exception as exc:
        logger.warning(exc)
        raise
    logger.info(f"content_dict=\n{pformat(content_dict)}")
    return content_dict

def prompt_for_descriptions(
    original_image_base64: str,
    masked_images_base64: list[str],
    max_tokens: int | None = MAX_TOKENS,
) -> list[str]:
    images = [original_image_base64] + masked_images_base64
    system_prompt = utils.render_template_from_file(
        "openadapt/prompts/system.j2",
    )
    logger.info(f"system_prompt=\n{system_prompt}")
    prompt = utils.render_template_from_file(
        "openadapt/prompts/description.j2",
    )
    logger.info(f"prompt=\n{prompt}")
    prompt_adapter = get_default_prompt_adapter()
    descriptions_json = prompt_adapter.prompt(
        prompt,
        system_prompt,
        images,
        max_tokens=max_tokens,
    )
    descriptions = utils.parse_code_snippet(descriptions_json)["descriptions"]
    logger.info(f"{descriptions=}")
    return descriptions

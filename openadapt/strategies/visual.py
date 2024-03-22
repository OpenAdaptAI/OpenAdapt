"""Large Multimodal Model with user-supplied Replay Instructions

See openadapt/prompts/system.j2 and "openadapt/prompts/action.j2
for details.

Usage:

    $ python -m openadapt.replay VisualReplayStrategy --instructions"<your instructions to the model>"
"""

from copy import deepcopy
from pprint import pformat

from loguru import logger
import deepdiff
import json

from openadapt import common, config, models, strategies, utils, vision
from openadapt import adapters

# number of actions to include in context simultaneously
ACTION_WINDOW_SIZE = 5

DEBUG = False

# throughput hack
SEGMENT_ONCE_PER_WINDOW_TITLE = True


class VisualReplayStrategy(
    strategies.base.BaseReplayStrategy,
):
    """Large Multimodal Model"""

    def __init__(
        self,
        recording: models.Recording,
    ) -> None:
        """Initialize the StatefulReplayStrategy.

        Args:
            recording (models.Recording): The recording object.
        """
        super().__init__(recording)
        self.recording_action_strs = [
            f"<{action_event}>"
            for action_event in self.recording.processed_action_events
        ][:-1]
        self.recording_action_idx = 0
        self.prepare_screenshots()

    def prepare_screenshots(self) -> None:
        # first get one screenshot per unique window title
        self.screenshots_by_window_title = get_screenshots_by_window_title(
            self.recording.processed_action_events
        )
        self.window_segmentation_by_title = {}
        for window_title, screenshots in self.screenshots_by_window_title.items():
            # just use the first screenshot for now
            screenshot = screenshots[0]

            # XXX why is screenshot.action_event a list?
            action_event = screenshot.action_event[0]
            if action_event.name in common.MOUSE_EVENTS:
                segmentation = get_window_segmentation(action_event)
                self.window_segmentation_by_title[window_title] = segmentation

    def get_next_action_event(
        self,
        active_screenshot: models.Screenshot,
        active_window: models.WindowEvent,
        replay_instructions: str,
    ) -> models.ActionEvent:
        """Get the next ActionEvent for replay.

        Args:
            active_screenshot (models.Screenshot): The active screenshot object.
            active_window (models.WindowEvent): The active window event object.
            replay_instructions (str): User-specified replay instructions.

        Returns:
            models.ActionEvent: The next ActionEvent for replay.
        """
        logger.debug(f"{self.recording_action_idx=}")
        if self.recording_action_idx == len(self.recording.processed_action_events):
            raise StopIteration()

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

        actions = self.recording.processed_action_events
        prev_window_title = None
        prompt_frames = []
        active_action = None
        active_action_dict = None
        num_images = 0
        screenshots = []
        #screenshots_by_window_title = get_screenshots_by_window_title(actions)
        for action_idx, action in enumerate(actions):
            window = action.window_event
            window_dict = deepcopy(
                {
                    key: val
                    for key, val in utils.row2dict(window, follow=False).items()
                    if val is not None
                    and not key.endswith("timestamp")
                    and not key.endswith("id")
                    # and not isinstance(getattr(models.WindowEvent, key), property)
                }
            )
            window_dict["state"].pop("data")
            window_dict["state"].pop("meta")
            window_title = window_dict["title"]
            if prev_window_title is None or prev_window_title != window_title:
                screenshot = action.screenshot
                num_images += 1
                screenshots.append(screenshot)
            prev_window_title = window_title
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
            is_reference_action = action_idx == self.recording_action_idx
            logger.info(f"{is_reference_action=}")
            if is_reference_action:
                reference_action = action
                reference_action_dict = action_dict

            active_segment_description = None
            active_segment_bounding_box = None
            if action.name in ("click", "doubleclick", "singleclick"):
                if SEGMENT_ONCE_PER_WINDOW_TITLE:
                    window_segmentation = self.window_segmentation_by_title[window_title]
                else:
                    window_segmentation = get_window_segmentation(action_event)
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


        active_window_segmentation = get_window_segmentation(
            screenshot=active_screenshot,
            window_event=active_window,
        )

        # XXX TODO: replace screenshot with active window segmentation

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


MAX_TOKENS = 2**14  # 16384

from dataclasses import dataclass

from PIL import Image

@dataclass
class Segmentation:
    masked_images: list[Image.Image]
    descriptions: list[str]
    bounding_boxes: list[dict[str, float]]  # "top", "left", "height", "width"
    centroids: list[tuple[float, float]]

from typing import Optional

def get_active_segment(action: models.ActionEvent, window_segmentation: Segmentation) -> Optional[int]:
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
    screenshot: models.ScreenShot | None = None,
    window_event: models.WindowEvent | None = None,
) -> Segmentation:
    assert action_event or (screenshot and window_event)
    if action_event:
        screenshot = action_event.screenshot
        screenshot.crop_active_window(action_event)
    else:
        width_ratio, height_ratio = utils.get_scale_ratios(action_event)
        screenshot.crop_active_window(
            window_event=window_event, 
            width_ratio=width_ratio,
            height_ratio=height_ratio,
        )
    original_image = screenshot.image
    if DEBUG:
        original_image.show()
    segmented_image = adapters.replicate.fetch_segmented_image(original_image)
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
        vision.display_images_grid_with_titles(masked_images, descriptions)
        import ipdb; ipdb.set_trace()
    return segmentation

def get_screenshots_by_window_title(
    actions: list[models.ActionEvent],
) -> dict[str, models.Screenshot]:
    screenshots_by_window_title = {}
    for action_idx, action in enumerate(actions):
        window = action.window_event
        window_title = window.title
        screenshot = action.screenshot
        screenshots_by_window_title.setdefault(window_title, [])
        screenshots_by_window_title[window_title].append(screenshot)
    return screenshots_by_window_title


def get_default_adapter():
    return {
        "openai": adapters.openai,
        "anthropic": adapters.anthropic,
    }[config.DEFAULT_ADAPTER]


from typing import Callable

# modified from https://github.com/OpenAdaptAI/OpenAdapt/pull/560/files
def prompt_for_action(
    prompt_data: dict,
    max_tokens: int | None = MAX_TOKENS,
    adapter: Callable = get_default_adapter(),
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
    #import ipdb; ipdb.set_trace()
    content = adapter.prompt(
        prompt,
        system_prompt,
        images,
        max_tokens=max_tokens,
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
    adapter: Callable = get_default_adapter(),
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
    descriptions_json = adapter.prompt(
        prompt,
        system_prompt,
        images,
        max_tokens=max_tokens,
    )
    descriptions = utils.parse_code_snippet(descriptions_json)["descriptions"]
    logger.info(f"{descriptions=}")
    return descriptions

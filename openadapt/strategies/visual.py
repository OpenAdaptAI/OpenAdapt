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
            action_event = screenshot.action_event
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
        active_action_dict = None
        num_images = 0
        screenshots_base64 = []
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
            window_title = window_dict["title"]
            if prev_window_title is None or prev_window_title != window_title:
                screenshot_base64 = action.screenshot.base64
                num_images += 1
                screenshots_base64.append(screenshot_base64)
            prev_window_title = window_title

            is_keyboard_action = action.name in common.KEY_EVENTS
            if is_keyboard_action:
                action_dict = deepcopy(
                    {
                        key: val
                        for key, val in utils.rows2dicts(
                            [action],# follow=is_keyboard_action,
                        )[0].items()
                        if val is not None
                        and not key.endswith("timestamp")
                        and not key.endswith("id")
                        # and not isinstance(getattr(models.ActionEvent, key), property)
                    }
                )
            else:
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
            is_current_action = action_idx == self.recording_action_idx
            logger.info(f"{is_current_action=}")
            if is_current_action:
                active_action_dict = action_dict

            #if SEGMENT_ONCE_PER_WINDOW_TITLE:
            #    window_segmentation = self.window_segmentation_by_title[window_title]
            #else:
            #    window_segmentation

            masked_images = window_segmentation["masked_imges"]
            descriptions = window_segmentation["descriptions"]
            bounding_boxes = window_segmentation["bounding_boxes"]
            prompt_frame = {
                "screenshot_number": num_images,
                "window": window_dict,
                "action": action_dict,
                "action_idx": action_idx,
                "action_number": action_idx + 1,
                "is_current_action": is_current_action,
            }
            prompt_frames.append(prompt_frame)
        logger.info(f"prompt_frames=\n{pformat(prompt_frames)}")

        screenshots_base64.append(active_screenshot.base64)
        screenshots_base64 = [
            screenshot.base64
            for screenshot in screenshots
        ]
        prompt_data = {
            "prompt_frames": prompt_frames,
            "active_window": active_window_dict,
            "active_action": active_action_dict,
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
        active_action = models.ActionEvent.from_children(active_action_dicts)
        self.recording_action_idx += 1
        return active_action


MAX_TOKENS = 2**14  # 16384

from dataclasses import dataclass

from PIL import Image

@dataclass
class Segmentation:
    masked_images: list[Image.Image]
    descriptions: list[str]
    bounding_boxes: dict[float, float | tuple[float, float]]


def get_window_segmentation(
    action_event: models.ActionEvent
) -> Segmentation:
        screenshot.crop_active_window(action_event)
        original_image = screenshot.image
        segmented_image = adapters.replicate.fetch_segmented_image(original_image)
        masks = vision.process_image_for_masks(segmented_image)
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
        bounding_boxes = vision.calculate_bounding_boxes(refined_masks)
        assert len(bounding_boxes) == len(descriptions), (
            len(bounding_boxes), len(descriptions)
        )
        return Segmentation(masked_images, descriptions, bounding_boxes)

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
):
    prompt_frames = prompt_data["prompt_frames"]
    active_window = prompt_data["active_window"]
    active_action = prompt_data["active_action"]
    replay_instructions = prompt_data["replay_instructions"]
    task_description = prompt_data["task_description"]
    images = prompt_data["screenshots_base64"]

    num_actions = len(prompt_frames)

    num_images = len(images)
    system_prompt = utils.render_template_from_file(
        "openadapt/prompts/system.j2",
        #recording_task_description=recording_task_description,
        #replay_instructions=replay_instructions,
        #action_number,
        #num_actions=num_actions,
    )
    logger.info(f"system_prompt=\n{system_prompt}")
    #for window_dict in (reference_window_dict, active_window_dict):
    #    for key in ("meta", "data"):
    #        if key in window_dict:
    #            del window_dict[key]

    #from openadapt.common import KEY_EVENTS, MOUSE_EVENTS
    #valid_action_names = KEY_EVENTS + MOUSE_EVENTS

    prompt = utils.render_template_from_file(
        "openadapt/prompts/action.j2",
        prompt_frames=prompt_frames,
        active_window=active_window,
        active_action=active_action,
        replay_instructions=replay_instructions,
        task_description=task_description,
        num_actions=num_actions,
        num_images=num_images,
    )
    logger.info(f"prompt=\n{prompt}")
    #payload = adapter.create_payload(
    #    prompt,
    #    system_prompt,
    #    images,
    #    max_tokens=max_tokens,
    #)
    #logger.info(f"payload=\n{pformat(payload)}")
    #result = adapter.get_completion(payload)
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
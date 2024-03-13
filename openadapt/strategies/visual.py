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

from openadapt import config, models, strategies, utils
from openadapt import adapters


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

    def get_next_action_event(
        self,
        active_screenshot: models.Screenshot,
        active_window: models.WindowEvent,
        instructions: str,
    ) -> models.ActionEvent:
        """Get the next ActionEvent for replay.

        Args:
            active_screenshot (models.Screenshot): The active screenshot object.
            active_window (models.WindowEvent): The active window event object.
            instructions (str): User-specified replay instructions.

        Returns:
            models.ActionEvent: The next ActionEvent for replay.
        """
        logger.debug(f"{self.recording_action_idx=}")
        if self.recording_action_idx == len(self.recording.processed_action_events):
            raise StopIteration()
        reference_action = self.recording.processed_action_events[
            self.recording_action_idx
        ]
        reference_window = reference_action.window_event

        reference_window_dict = deepcopy(
            {
                key: val
                for key, val in utils.row2dict(reference_window, follow=False).items()
                if val is not None
                and not key.endswith("timestamp")
                and not key.endswith("id")
                # and not isinstance(getattr(models.WindowEvent, key), property)
            }
        )
        if reference_action.children:
            reference_action_dicts = [
                deepcopy(
                    {
                        key: val
                        for key, val in utils.row2dict(child, follow=False).items()
                        if val is not None
                        and not key.endswith("timestamp")
                        and not key.endswith("id")
                        and not isinstance(getattr(models.ActionEvent, key), property)
                    }
                )
                for child in reference_action.children
            ]
        else:
            reference_action_dicts = [
                deepcopy(
                    {
                        key: val
                        for key, val in utils.row2dict(
                            reference_action, follow=False
                        ).items()
                        if val is not None
                        and not key.endswith("timestamp")
                        and not key.endswith("id")
                        # and not isinstance(getattr(models.ActionEvent, key), property)
                    }
                )
            ]
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
        reference_window_dict["state"].pop("data")
        active_window_dict["state"].pop("data")
        reference_screenshot = reference_action.screenshot

        completion = prompt_for_action(
            reference_screenshot,
            reference_window_dict,
            reference_action_dicts,
            active_screenshot,
            active_window_dict,
            self.recording.task_description,
            instructions,
        )

        #active_action_dicts = utils.get_action_dict_from_json(completion)
        active_action_dicts = completion
        logger.debug(f"active_action_dicts=\n{pformat(active_action_dicts)}")
        active_action = models.ActionEvent.from_children(active_action_dicts)
        self.recording_action_idx += 1
        return active_action


MAX_TOKENS = 2**14  # 16384


def get_default_adapter():
    return {
        "openai": adapters.openai,
        "anthropic": adapters.anthropic,
    }[config.DEFAULT_ADAPTER]


from typing import Callable

# copied from https://github.com/OpenAdaptAI/OpenAdapt/pull/560/files
def prompt_for_action(
    reference_screenshot: models.Screenshot,
    reference_window_dict: dict,
    reference_action_dicts: list[dict],
    active_screenshot: models.Screenshot,
    active_window_dict: dict,
    recording_task_description: str,
    replay_instructions: str,
    max_tokens: int | None = MAX_TOKENS,
    adapter: Callable = get_default_adapter(),
):
    reference_screenshot_base64 = reference_screenshot.base64
    active_screenshot_base64 = active_screenshot.base64
    images = [reference_screenshot_base64, active_screenshot_base64]
    system_prompt = utils.render_template_from_file(
        "openadapt/prompts/system.j2",
        recording_task_description=recording_task_description,
        replay_instructions=replay_instructions,
    )
    #logger.info(f"{action=}")
    for window_dict in (reference_window_dict, active_window_dict):
        for key in ("meta", "data"):
            if key in window_dict:
                del window_dict[key]
    prompt = utils.render_template_from_file(
        "openadapt/prompts/action.j2",
        reference_action_jsons=json.dumps(reference_action_dicts),
        reference_window_json=json.dumps(reference_window_dict),
        active_window_json=json.dumps(active_window_dict),
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

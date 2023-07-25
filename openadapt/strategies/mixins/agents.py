"""Implements a ReplayStrategy mixin for captioning with Transformers Agents.

Usage:

    class TransformersAgentsMixin(OpenAiAgent):
        ...
"""

import os

from loguru import logger
from transformers import OpenAiAgent, Tool

from openadapt import config
from openadapt.events import get_events
from openadapt.models import ActionEvent, Recording, Screenshot
from openadapt.utils import display_event

MODEL_NAME = config.OPENAI_MODEL_NAME


def get_prompt(action_event: ActionEvent, diffs: bool = False) -> str:
    """Returns a prompt for the agent.

    Args:
        diffs (bool, *optional*, defaults to `False`):
            Whether or not to mention diffs in the prompt.
        action_event (ActionEvent): The action event to prompt for.

    Returns:
        str: The prompt for the agent.
    """
    return (
        f"In the image, you are presented with a "
        f"screenshot of a user's current active window."
        f"The user's current window event is: {action_event.window_event.title}. "
        f"What is the user doing, and what text do they see? "
        f"DO NOT SEGMENT, feel free to use text_classifier and text_qa. "
        f"If you have been given another image previously, "
        f"please use that image and list the user's next possible actions."
    ) + (
        "A diff of the screenshot may be given after the prompt".join(
            " if it does not contain useful information, disregard it."
        )
        if diffs
        else ""
    )


class TransformersAgentsMixin(OpenAiAgent):
    """Wrapper for OpenAiAgent.

    Will continuously prompt the agent with screenshots and action events.
    """

    screenshots = []
    action_events = []

    def __init__(
        self,
        recording: Recording,
        api_key: str,
        model_name: str = MODEL_NAME,
        chat_prompt_template: str = None,
        run_prompt_template: str = None,
        additional_tools: Tool = None,
        screenshots: list[Screenshot] = [],
    ) -> None:
        """Initialize the TransformersAgentsMixin class.

        Args:
            recording (Recording): The recording to prompt on.
            api_key (str): The OpenAI API key.
            model_name (str, *optional*, defaults to `MODEL_NAME`):
                The name of the model to use.
            chat_prompt_template (str, *optional*, defaults to `None`):
                The template to use for chat prompts.
            run_prompt_template (str, *optional*, defaults to `None`):
                The template to use for run prompts.
            additional_tools (Tool, *optional*, defaults to `None`):
                Additional tools to use.
            screenshots (list[Screenshot], *optional*, defaults to `[]`):
                The screenshots to prompt on.
        """
        super().__init__(
            model_name,
            api_key,
            chat_prompt_template,
            run_prompt_template,
            additional_tools,
        )
        if recording is None:
            logger.warning("recording invalid")
        else:
            self.recording = recording
            self.screenshots = screenshots
            self.action_events = get_events(recording)

    def prompt(self, n: int = -1, debug: bool = False, diffs: bool = False) -> bool:
        """Wrapper for `self.chat`.

        This method will prompt the agent with screenshots and action events.

        Args:
            n (int): number of action events to prompt for. -1 for all.
            debug (bool): whether to save screenshots and diffs to disk
            diffs (bool): whether to prompt with diffs [more experimental]

        Returns:
            bool: whether the prompt was successful

        Preconditions:
            - `self.recording` is not `None`
        """
        if self.recording is None:
            raise ValueError("recording is invalid")
        else:
            debug_path = os.path.join(
                config.DEBUG_DIR_NAME,
                f"{self.recording.task_description}-{self.recording.timestamp}",
            )

        for idx, action_event in enumerate(self.action_events):
            if n != -1 and idx >= n:
                break

            screenshot = action_event.screenshot
            if screenshot is not None:
                self.screenshots.append(screenshot)
                screenshot.crop_active_window(action_event)
                diff = display_event(action_event, diff=True) if diffs else None

                if debug:
                    os.makedirs(debug_path, exist_ok=True)
                    logger.info("writing debug files")
                    screenshot._image.save(
                        os.path.join(
                            debug_path, f"recording-{self.recording.id}-{idx}.png"
                        )
                    )
                    if diff is not None:
                        diff.save(
                            os.path.join(
                                debug_path,
                                "recording-{self.recording.id}-{idx}-diff.png",
                            )
                        )
            try:
                self.chat(get_prompt(action_event, diffs), image=screenshot.image)
                if diffs:
                    self.chat(
                        f"Here is a diff of the screenshot. "
                        f"The event text is {action_event.text} What can you conclude?",
                        image=diff,
                    )
            except Exception as e:
                logger.error(f"chat failed: {e}")
                raise
            else:
                logger.warning("screenshot invalid")
        return True

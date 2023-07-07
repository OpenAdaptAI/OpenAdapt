"""
Implements a ReplayStrategy mixin for captioning with Transformers Agents.

Usage:

    class TransformersAgentsMixin(OpenAiAgent):
        ...
"""

import os

from loguru import logger
from transformers import OpenAiAgent

from openadapt import config
from openadapt.events import get_events
from openadapt.models import Recording
from openadapt.utils import display_event

MODEL_NAME = config.OPENAI_MODEL_NAME


def get_prompt(action_event, diffs=False):
    """
    Returns a prompt for the agent.

    Args:
        diffs (bool, *optional*, defaults to `False`):
            Whether or not to mention diffs in the prompt.
        action_event:
            An `ActionEvent`.
    """

    return (
        f"In the image, you are presented with a screenshot of a user's current active window."
        f"The user's current window event is: {action_event.window_event.title}."
        f"What is the user doing, and what text do they see? DO NOT SEGMENT, feel free to use text_classifier and text_qa."
        f"If you have been given another image previously, please use that image and list the user's next possible actions."
    ) + (
        " A diff of the screenshot may be given after the prompt, if it does not contain useful information, disregard it."
        if diffs
        else ""
    )


class TransformersAgentsMixin(OpenAiAgent):
    """
    Wrapper for OpenAiAgent that will continuously prompt the agent with screenshots and action events for information.

    Args:
        recording (Recording):
          Recording to use in prompts.
        model (`str`, *optional*, defaults to `MODEL_NAME`):
            The name of the OpenAI model to use.
        api_key (`str`, *optional*):
            The API key to use. If unset, will look for the environment variable `"OPENAI_API_KEY"`.
        chat_prompt_template (`str`, *optional*):
            Pass along your own prompt if you want to override the default template for the `chat` method.
        run_prompt_template (`str`, *optional*):
            Pass along your own prompt if you want to override the default template for the `run` method.
        additional_tools ([`Tool`], list of tools or dictionary with tool values, *optional*):
            Any additional tools to include on top of the default ones. If you pass along a tool with the same name as
            one of the default tools, that default tool will be overridden. See test_agents.py for an example.
    """

    screenshots = []
    action_events = []

    def __init__(
        self,
        recording: Recording,
        api_key,
        model_name=MODEL_NAME,
        chat_prompt_template=None,
        run_prompt_template=None,
        additional_tools=None,
        screenshots=[],
    ):
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

    def prompt(self, n=-1, debug=False, diffs=False):
        """
        This function prompts the agent with a message and an image, asking them to identify what the user is
        doing and what text they see in the image.

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
                        f"Here is a diff of the screenshot. The event text is {action_event.text} What can you conclude?",
                        image=diff,
                    )
            except Exception as e:
                logger.error(f"chat failed: {e}")
                raise
            else:
                logger.warning("screenshot invalid")
        return True

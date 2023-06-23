"""
Implements a ReplayStrategy mixin for captioning with Transformers Agents.

Usage:

    class TransformersAgentsMixin(OpenAiAgent):
        ...
"""

import os
from transformers import OpenAiAgent
from openadapt import config
from openadapt.events import get_events
from openadapt.models import Recording, Screenshot, ActionEvent

from openadapt.utils import display_event

MODEL_NAME = config.OPENAI_MODEL_NAME


class TransformersAgentsMixin(OpenAiAgent):
    screenshots = []
    action_events = []
    meta = {}

    def __init__(
        self,
        recording: Recording,
        api_key,
        model_name=MODEL_NAME,
        chat_prompt_template=None,
        run_prompt_template=None,
        additional_tools=None,
        screenshots=None,
    ):
        super().__init__(
            MODEL_NAME,
            api_key,
            chat_prompt_template,
            run_prompt_template,
            additional_tools,
        )
        if recording is None:
            print("warning: no recording given")
        else:
            self.recording = recording
            self.screenshots = screenshots
            self.action_events = get_events(recording, process=5, meta=self.meta)

    def prompt(self, debug=False):
        """
        This function prompts the agent with a message and an image, asking them to identify what the user is
        doing and what text they see in the image.
        """

        for idx, action_event in enumerate(self.action_events):
            screenshot = action_event.screenshot
            self.screenshots.append(screenshot)
            screenshot.crop_active_window(action_event)
            if debug:
                diff = display_event(action_event, diff=True)
                if not os.path.exists("debug"):
                    os.mkdir("debug")
                else:
                    for f in os.listdir("debug"):
                        os.remove(os.path.join("debug", f))
                screenshot._image.save(f"debug/recording-{self.recording.id}-{idx}.png")
                diff.save(f"debug/recording-{self.recording.id}-{idx}-diff.png")

            self.chat(
                f"In the image, you are presented with a screenshot of a user's current active window."
                f"The user's window event is: {action_event.window_event.title}."
                f"What is the user doing, and what text do they see? DO NOT SEGMENT, feel free to use text_classifier and text_qa. "
                f"If you have been given another image previously, please use that image and list the user's next possible actions.",
                image=screenshot.image.convert("RGB"),
            )

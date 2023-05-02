"""
Demonstration of LLM, OCR, and ASCII ReplayStrategyMixins.

Usage:

    $ python puterbot/replay.py DemoReplayStrategy
"""

from loguru import logger
from typing import List, Dict, Union
from pynput import keyboard, mouse

import numpy as np
import guardrails as gd
import re
import transformers

from puterbot.events import get_events
from puterbot.playback import play_input_event
from puterbot.models import Recording, Screenshot, InputEvent
from puterbot.strategies.base import BaseReplayStrategy
from puterbot.strategies.llm_mixin import (
    LLMReplayStrategyMixin,
    MAX_INPUT_SIZE,
)
from puterbot.strategies.ocr_mixin import OCRReplayStrategyMixin
from puterbot.strategies.ascii_mixin import ASCIIReplayStrategyMixin

RAIL_STR = """
<rail version="0.1">

<output>
    <object name="dragon">
        <string name="name" format="valid-choices: {['move', 'click', 'scroll', 'doubleclick', 'singleclick', 'press', 'release', 'type']}" on-fail-valid-choices="reask" />
        <integer name="x" description="The x coordinate of the InputEvent" />
        <integer name="y" description="The y coordinate of the InputEvent" />
        <integer name="dx" description="The change in x coordinate of the InputEvent" />
        <integer name="dy" description="The change in y coordinate of the InputEvent" />
        <string name="button_name" description="The mouse button name if it was Mouse InputEvent" />
        <bool name="pressed" description="Was the mouse pressed?" />
        <integer name="key" description="The key if it was a Key InputEvent" />
    </object>
</output>

<prompt>
Extract information from this document and return a JSON that follows the correct schema.

@xml_prefix_prompt

{output_schema}

@json_suffix_prompt_v2_wo_none

</prompt>

</rail>
"""
MAX_TOKENS = 100

guard = gd.Guard.from_rail_string(RAIL_STR)


class DemoReplayStrategy(
    LLMReplayStrategyMixin,
    OCRReplayStrategyMixin,
    ASCIIReplayStrategyMixin,
    BaseReplayStrategy,
):

    def __init__(
        self,
        recording: Recording,
    ):
        super().__init__(recording)
        self.result_history = []
        self.prompt = ""

    def my_llm_api(self, prompt, **kwargs) -> str:
        """Custom LLM API wrapper.

        Args:
            prompt (str): The prompt to be passed to the LLM API
            **kwargs: Any additional arguments to be passed to the LLM API

        Returns:
            str: The output of the LLM API
        """
        # Call your LLM API here
        completion = self.get_completion(prompt, MAX_TOKENS)
        logger.info(f"{completion=}")
        return completion
   
    def parse_input_event(self, completion):
        # Define a list of allowed input event names
        allowed_names = ['move', 'click', 'scroll', 'doubleclick', 'singleclick', 'press', 'release', 'type']
        # Split the completion string by the first occurrence of the "[" character and remove unnecessary characters
        completion = completion.split('[')
        s = completion[1].replace(", ", "").replace(",", "").replace("'", "")
        # Extract the command string from the modified completion string
        command_string = [x.strip() for x in s.split("'") if x.strip()]
        # Split the command string by the allowed input event names
        commands = re.split('(move|click|scroll|doubleclick|singleclick|press|release|type)', command_string[0])
        # Remove empty items from the commands list
        commands = list(filter(None, commands))
        # Initialize variables
        result = []
        current = ''

        # Iterate over the commands list and create input events based on the allowed input event names
        for item in commands:
            if item in allowed_names:
                if current:
                    result.append(current)
                    current = ''
                result.append(item)
            else:
                current += item
        if current:
            result.append(current)

        # Merge consecutive input events that belong to the same action
        merged_commands = [result[i] + result[i+1] for i in range(0, len(result), 2)]

        # Iterate over the merged input events and create a list of input event dictionaries
        res = []
        for command in merged_commands:
            input_dict = {
                "name": "",
                "mouse_x": 0,
                "mouse_y": 0,
                "mouse_dx": 0,
                "mouse_dy": 0,
                "mouse_button_name": "left",
                "mouse_pressed": False,
                "canonical_key_char": ""
            }
            # Split the input event string into a list of arguments
            args = command.split()
            name = args[0]
            input_dict["name"] = name
            if name == "press":
                input_dict["mouse_pressed"] = True 
            else: 
                input_dict["mouse_pressed"] = False
            if name == "move":
                input_dict["mouse_x"] = args[1]
                if len(args) > 2:
                    input_dict["mouse_y"] = args[2]
            elif name in ["click", "doubleclick", "singleclick", "press", "release"]:
                # Make sure the key is in valid Canonical form
                input_dict["canonical_key_char"] = (args[1])
            elif name == "scroll":
                input_dict["mouse_dx"] = args[1]
                if len(args) > 2:
                    input_dict["mouse_dy"] = args[2]
            elif name == "type":
                input_dict["canonical_key_char"] = (args[1])
            res.append(input_dict)

        # Return the list of input event dictionaries
        return res

    
    def get_next_input_event(
        self,
        screenshot: Screenshot,
    ):

        ascii_text = self.get_ascii_text(screenshot)
        #logger.info(f"ascii_text=\n{ascii_text}")

        ocr_text = self.get_ocr_text(screenshot)
        #logger.info(f"ocr_text=\n{ocr_text}")

        event_strs = [
            f"{event}, "
            for event in self.recording.input_events
        ]
        history_strs = [
            f"<{completion}>"
            for completion in self.result_history
        ]

        self.prompt = "Generate the next input event based on the following:\n\n" \
                 "Task goal: {}\n\n" \
                 "Previously recorded input events: {}\n\n" \
                 "Using the previously recorded input events, generate a sequence of input events to complete the task as a list: " \
            .format(self.recording.task_description,
                    event_strs)
        logger.info(f"{self.prompt=}")

        completion = self.get_completion(self.prompt, MAX_TOKENS)
        logger.info(f"{completion=}")

        parsed_events = self.parse_input_event(completion)
        logger.info(f"{parsed_events=}")


        # Wrap the get_completion() method with Guard object
        # validated_output = guard(
        #     self.my_llm_api,
        #     prompt_params={"prompt": self.prompt},
        #     engine="text-davinci-003",
        #     max_tokens=max_tokens,
        # )
        # logger.info(f"{validated_output=}")

        keyboard_controller = keyboard.Controller()
        mouse_controller = mouse.Controller()
        # play all the parsed InputEvents
        for event in parsed_events:
            try:
                input_event = InputEvent(**event)
                play_input_event(
                    input_event,
                    mouse_controller,
                    keyboard_controller)
            except Exception as e:
                # if there are errors playing input events
                print(e)

        return None

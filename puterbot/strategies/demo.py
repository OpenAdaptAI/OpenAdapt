"""
Demonstration of LLM, OCR, and ASCII ReplayStrategyMixins.

Usage:

    $ python puterbot/replay.py DemoReplayStrategy
"""

from loguru import logger
import numpy as np
import guardrails as gd
import json

from puterbot.events import get_events
from puterbot.models import Recording, Screenshot
from puterbot.strategies.base import BaseReplayStrategy
from puterbot.strategies.llm_mixin import (
    LLMReplayStrategyMixin,
    MAX_INPUT_SIZE,
)
from puterbot.strategies.ocr_mixin import OCRReplayStrategyMixin
from puterbot.strategies.ascii_mixin import ASCIIReplayStrategyMixin

rail_str = """
<rail version="0.1">

<output>
    <choice name="action" on-fail-choice="reask">
        <case name="fight">
            <string name="weapon" format="valid-choices: {['crossbow', 'machine gun']}" on-fail-valid-choices="reask" />
        </case>
        <case name="flight">
            <object name="flight">
                <string name="flight_direction" format="valid-choices: {['north','south','east','west']}" on-fail-valid-choices="exception" />
                <integer name="distance" format="valid-choices: {[1,2,3,4]}" on-fail-valid-choices="exception" />
            </object>
        </case>
    </choice>
</output>

<prompt>
You are a human in an enchanted forest. You come across opponents of different types, and you should fight smaller opponents and run away from bigger ones.

You run into a gang member. What do you do?

@complete_json_suffix_v2</prompt>

</rail>
"""

guard = gd.Guard.from_rail_string(rail_str)


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

    def my_llm_api(self, prompt: str, **kwargs) -> str:
        """Custom LLM API wrapper.

        Args:
            prompt (str): The prompt to be passed to the LLM API
            **kwargs: Any additional arguments to be passed to the LLM API

        Returns:
            str: The output of the LLM API
        """
        max_tokens = kwargs.get('max_tokens', 10)
        # Call your LLM API here
        completion = self.get_completion(prompt, max_tokens)
        logger.info(f"{completion=}")
        return completion

    def get_next_input_event(
        self,
        screenshot: Screenshot,
    ):
        print("HELLO")
        ascii_text = self.get_ascii_text(screenshot)
        #logger.info(f"ascii_text=\n{ascii_text}")

        ocr_text = self.get_ocr_text(screenshot)
        #logger.info(f"ocr_text=\n{ocr_text}")

        event_strs = [
            f"<{event}>"
            for event in self.recording.input_events
        ]
        history_strs = [
            f"<{completion}>"
            for completion in self.result_history
        ]
        prompt = " ".join(event_strs + history_strs)
        N = max(0, len(prompt) - MAX_INPUT_SIZE)
        prompt = prompt[N:]
        logger.info(f"{prompt=}")
        max_tokens = 10

        # Wrap the get_completion() method with Guard object
        validated_output = guard(
            self.my_llm_api,
            prompt_params={"prompt": prompt},
            engine="text-davinci-003",
            max_tokens=max_tokens,
        )
        logger.info(f"{validated_output=}")

        #event_dict = json.loads(validated_output)
        # input_event = InputEvent(**event_dict)
        # play_input_event(input_event)
        print(validated_output)

        result = ""
        try:
            exec(validated_output["action"])
            print("Success! Valid Data")
            result = validated_output["action"]

        except Exception as e:
            # if there are exceptions use default result from get_completion
            print(e)

        logger.info(f"{result=}")
        self.result_history.append(result)

        # TODO: parse result into InputEvent(s)

        return None

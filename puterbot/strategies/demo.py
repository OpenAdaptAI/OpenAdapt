"""
Demonstration of LLM, OCR, and ASCII ReplayStrategyMixins.

Usage:

    $ python puterbot/replay.py DemoReplayStrategy
"""

from loguru import logger
import numpy as np

from puterbot.events import get_events
from puterbot.models import Recording, Screenshot
from puterbot.strategies.base import BaseReplayStrategy
from puterbot.strategies.llm_mixin import (
    LLMReplayStrategyMixin,
    MAX_INPUT_SIZE,
)
from puterbot.strategies.ocr_mixin import OCRReplayStrategyMixin
from puterbot.strategies.ascii_mixin import ASCIIReplayStrategyMixin


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
        self.input_event_idx = -1
        self.result_history = []

    def get_next_input_event(
        self,
        screenshot: Screenshot,
    ):
        
        self.input_event_idx += 1
        print("Input Event Index: ",self.input_event_idx) #Test Print
        ascii_text = self.get_ascii_text(screenshot)
        #logger.info(f"ascii_text=\n{ascii_text}")

        ocr_text = self.get_ocr_text(screenshot)
        #logger.info(f"ocr_text=\n{ocr_text}")

        event_strs = [
            f"<{event}>"
            for event in self.recording.input_events
        ]
        print(event_strs) #Test Print
        history_strs = [
            f"<{completion}>"
            for completion in self.result_history
        ]
        print(history_strs) #Test Print

        prompt = " ".join(event_strs + history_strs)
        N = max(0, len(prompt) - MAX_INPUT_SIZE)
        prompt = prompt[N:]          #Ensure prompt is not too long
        logger.info(f"{prompt=}")
        max_tokens = 20
        action_prompt = prompt.split(">")[self.input_event_idx] + ">"
        print("Action Prompt: ",action_prompt) #Test Print
        #completion = self.get_completion(prompt, max_tokens)
        #Try using action_prompt instead of prompt to generate completions
        completion = self.get_completion(action_prompt, max_tokens)
        logger.info(f"{completion=}")

        # only take the first <...>                            <1><2><3><4>   -->  [<1>,<2>,<3>,<4>] --> <1> --> 1
        result = completion.split(">")[0].strip(" <>")
        logger.info(f"{result=}")
        self.result_history.append(result)

        # TODO: parse result into InputEvent(s)
        # In the naive implementation, we return the recorded InputEvent
        # In the demo implementation, we would like to return a more sophisticated InputEvent

        # If there is a windowEvent, find an ASCII image of the window




        return None

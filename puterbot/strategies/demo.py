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
        self.previous_screenshot_OCR = None
        self.previous_screenshot_ASCII  = None
        self.result_history = []
        self.processed_input_events = get_events(recording, process=True)

    def get_next_input_event(
        self,
        screenshot: Screenshot,
    ):
        ascii_text = self.get_ascii_text(screenshot)
        #logger.info(f"ascii_text=\n{ascii_text}")

        ocr_text = self.get_ocr_text(screenshot)
        #logger.info(f"ocr_text=\n{ocr_text}")

        if (self.input_event_idx == -1):
            print("Starting Demo Replay Strategy...")
            self.input_event_idx += 1
        elif (len(self.processed_input_events)-1 == self.input_event_idx):
            #Finished replaying all input events
            raise StopIteration()
        else:
            if (ocr_text != self.previous_screenshot_OCR) or (ascii_text != self.previous_screenshot_ASCII):
                #Screen has changed, so continue to next action
                self.input_event_idx += 1
            else:
                #Wait for the screen to change before continuing to next action
                print("Waiting for screen to change...")
        

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
        prompt = prompt[N:]          #Ensure prompt is not too long
        logger.info(f"{prompt=}")
        max_tokens = 10
        #action_prompt = prompt.split(">")[self.input_event_idx] + ">"
        #print("Action Prompt: ",action_prompt) #Test Print
        completion = self.get_completion(prompt, max_tokens)
        #Try using action_prompt instead of prompt to generate completions
        #completion = self.get_completion(action_prompt, max_tokens)
        logger.info(f"{completion=}")

        # only take the first <...>                            <1><2><3><4>   -->  [<1>,<2>,<3>,<4>] --> <1> --> 1
        result = completion.split(">")[0].strip(" <>")
        logger.info(f"{result=}")
        self.result_history.append(result)

        # TODO: parse result into InputEvent(s)

        # TODO: In the demo implementation, we would like to create the next InputEvent based on the completion
        #       as opposed to using the previous recording's Input Event. 
        self.previous_screenshot_OCR = ocr_text
        self.previous_screenshot_ASCII = ascii_text
        return self.processed_input_events[self.input_event_idx]

        # TODO: If there is a windowEvent, use the ASCII image of the window (smaller area allows for more ASCII collumns)
        #       focusing a windowEvent would prevent background changes from triggering a new input event


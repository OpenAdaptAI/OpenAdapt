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

from gensim.summarization.summarizer import summarize
from nltk.corpus import wordnet


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

    def get_next_input_event(
        self,
        screenshot: Screenshot,
    ):
        ascii_text = self.get_ascii_text(screenshot)
        #logger.info(f"ascii_text=\n{ascii_text}")

        ocr_text = self.get_ocr_text(screenshot)
        #logger.info(f"ocr_text=\n{ocr_text}")

        # identify what the window is
        summarized_ocr = summarize(ocr_text, word_count=1, split=False)

        index = self.screenshots.index(screenshot)
        if index != 0:
            last_screenshot = self.screenshots[index - 1]
            last_summarized_ocr = summarize(last_screenshot, word_count=1, split=False)

            # check if the last screenshot and current screenshot picture the same image
            synonyms_screenshot = set(wordnet.synsets(summarized_ocr))
            synonyms_last_screenshot = set(wordnet.synsets(last_summarized_ocr))

            common_synonyms = list(synonyms_screenshot & synonyms_last_screenshot)

            # may want to change the number of required common synonyms
            if len(common_synonyms) > 0:
                window_changed = "True"
            else:
                window_changed = "False"
        else:
            window_changed = "False"

        event_strs = [
            f"<{event}>"
            for event in self.recording.input_events
        ]
        history_strs = [
            f"<{completion}>"
            for completion in self.result_history
        ]
        prompt = " ".join(event_strs + history_strs + summarized_ocr)
        N = max(0, len(prompt) - MAX_INPUT_SIZE)
        prompt = prompt[N:]
        logger.info(f"{prompt=}")
        max_tokens = 10
        completion = self.get_completion(prompt, max_tokens)
        logger.info(f"{completion=}")

        # only take the first <...>
        result = completion.split(">")[0].strip(" <>")
        logger.info(f"{result=}")
        self.result_history.append(result)

        # TODO: parse result into InputEvent(s)

        return None

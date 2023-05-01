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
import re


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

        cleaned_ascii = clean_ascii(ascii_text)

        summarized_ascii = summarize(cleaned_ascii, word_count=1, split=False)
        synonyms_ascii = set(wordnet.synsets(summarized_ascii))

        ocr_text = self.get_ocr_text(screenshot)
        #logger.info(f"ocr_text=\n{ocr_text}")

        # identify what the window is
        summarized_ocr = summarize(ocr_text, word_count=1, split=False)
        synonyms_ocr = set(wordnet.synsets(summarized_ocr))

        # words that describe the screenshot display
        summarized_screenshot = set(synonyms_ascii & synonyms_ocr)

        index = self.screenshots.index(screenshot)
        if index != 0:
            last_screenshot = self.screenshots[index - 1]
            last_summarized_ascii = summarize(self.get_ascii_text(last_screenshot), word_count=1,
                                              split=False)
            last_summarized_ocr = summarize(self.get_ocr_text(last_screenshot), word_count=1,
                                            split=False)
            # summarize the last screenshot
            summarized_screenshot = set(last_summarized_ascii & last_summarized_ocr)

            # check whether the last screenshot and the current screenshot are the same
            common_synonyms = list(summarized_screenshot & summarized_screenshot)

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


def clean_ascii(
    text,
):
    # replace the irrelevant symbols in the ascii with spaces
    no_symbols = re.sub(r'[^\w\s]+', '', text)

    ascii_words = []

    for word in no_symbols:
        if wordnet.synsets(word):
            ascii_words.append(word)
    return ascii_words

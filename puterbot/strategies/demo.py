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

from sumy.summarizers.lsa import LsaSummarizer
from sumy.parsers.plaintext import PlaintextParser
from sumy.nlp.tokenizers import Tokenizer
from sumy.nlp.stemmers import Stemmer
from sumy.utils import get_stop_words
from nltk.corpus import wordnet
import re
from fuzzywuzzy import fuzz
import statistics


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

        index = self.screenshots.index(screenshot)
        if index != 0:
            last_screenshot = self.screenshots[index - 1]

            # check whether the last screenshot and the current screenshot are the same
            ascii_similarity = compare_text(ascii_text, self.get_ascii_text(last_screenshot))
            ocr_similarity = compare_text(ocr_text, self.get_ascii_text(last_screenshot))

            similarity_list = [ascii_similarity, ocr_similarity]

            # may want the required minimum  similarity
            if statistics.fmean(similarity_list) > 50:
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
        prompt = " ".join(event_strs + history_strs + list(window_changed))
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
    return str(ascii_words)

# takes 2 lists of strings and returns how similar they are as a float
def compare_text(
        text1,
        text2
):
    stemmer = Stemmer("english")
    summarizer = LsaSummarizer(stemmer)
    summarizer.stop_words = get_stop_words("english")

    print(text1)
    parser1 = PlaintextParser.from_string(text1, Tokenizer("english"))
    summarized1 = summarizer(parser1.document, 1)
    print(summarized1)

    parser2 = PlaintextParser.from_string(text2, Tokenizer("english"))
    summarized2 = summarizer(parser2.document, 1)

    # may want to change to something more complex
    return fuzz.ratio(summarized1, summarized2)

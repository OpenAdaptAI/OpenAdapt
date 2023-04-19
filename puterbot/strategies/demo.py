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
from puterbot.strategies.llm_mixin import LLMReplayStrategyMixin
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

    def get_next_input_event(
        self,
        screenshot: Screenshot,
    ):
        ascii_text = self.get_ascii_text(screenshot)
        logger.info(f"ascii_text=\n{ascii_text}")

        ocr_text = self.get_ocr_text(screenshot)
        logger.info(f"ocr_text=\n{ocr_text}")

        max_tokens = 2

        _min = 2
        _max = 10
        tokens = np.random.permutation(
            ["click"] * np.random.randint(_min, _max) +
            ["type"] * np.random.randint(_min, _max) +
            ["scroll"] * np.random.randint(_min, _max)
        )
        prompt = " ".join(tokens)
        logger.info(f"{prompt=}")
        completion = self.get_completion(prompt, max_tokens)
        logger.info(f"{completion=}")

        return None

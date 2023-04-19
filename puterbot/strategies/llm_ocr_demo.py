"""
Demonstration of LLMReplayStrategyMixin and OCRReplayStrategyMixin.

Usage:

    $ python puterbot/replay.py LLMOCRDemoReplayStrategy
"""


from loguru import logger

import mss.base

from puterbot.events import get_events
from puterbot.models import Recording
from puterbot.strategies.base import BaseReplayStrategy
from puterbot.strategies.llm_mixin import LLMReplayStrategyMixin
from puterbot.strategies.ocr_mixin import OCRReplayStrategyMixin


class LLMOCRDemoReplayStrategy(
    LLMReplayStrategyMixin,
    OCRReplayStrategyMixin,
    BaseReplayStrategy,
):

    def __init__(
        self,
        recording: Recording,
    ):
        super().__init__(recording)

    def get_next_input_event(
        self,
        screenshot: mss.base.ScreenShot,
    ):
        text = self.get_text(screenshot)

        # this doesn't make sense and is for demonstrative purposes only
        completion = self.generate_completion(text, 100)

        return None

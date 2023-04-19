"""
Demonstration of LLMReplayStrategyMixin and OCRReplayStrategyMixin.

Usage:

    $ python puterbot/replay.py LLMOCRDemoReplayStrategy
"""


from puterbot.events import get_events
from puterbot.models import Recording, Screenshot
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
        screenshot: Screenshot,
    ):
        text = self.get_text(screenshot)

        # N.B. this doesn't make sense and is for demonstration purposes only
        completion = self.generate_completion(text, 100)

        return None

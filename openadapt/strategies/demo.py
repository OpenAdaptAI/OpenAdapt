"""
Demonstration of HuggingFace, OCR, and ASCII ReplayStrategyMixins.

Usage:

    $ python -m openadapt.replay DemoReplayStrategy
"""

from loguru import logger
import numpy as np
from openadapt.crud import get_screenshots

from openadapt.events import get_events
from openadapt.models import Recording, Screenshot, WindowEvent
from openadapt.strategies.base import BaseReplayStrategy
from openadapt.strategies.mixins.huggingface import (
    HuggingFaceReplayStrategyMixin,
    MAX_INPUT_SIZE,
)

from openadapt.strategies.mixins.ocr import OCRReplayStrategyMixin
from openadapt.strategies.mixins.ascii import ASCIIReplayStrategyMixin
from openadapt.strategies.mixins.sam import SAMReplayStrategyMixin
from openadapt.strategies.mixins.summary import SummaryReplayStrategyMixin


class DemoReplayStrategy(
    HuggingFaceReplayStrategyMixin,
    OCRReplayStrategyMixin,
    ASCIIReplayStrategyMixin,
    SAMReplayStrategyMixin,
    SummaryReplayStrategyMixin,
    BaseReplayStrategy,
):
    """ """
    def __init__(
        self,
        recording: Recording,
    ):
        super().__init__(recording)
        self.result_history = []
        self.screenshots = get_screenshots(recording)
        self.screenshot_idx = 0

    def get_next_action_event(
        self,
        screenshot: Screenshot,
        window_event: WindowEvent,
    ):
        """

        Args:
          screenshot: Screenshot: 
          window_event: WindowEvent: 

        Returns:

        """
        ascii_text = self.get_ascii_text(screenshot)
        # logger.info(f"ascii_text=\n{ascii_text}")

        ocr_text = self.get_ocr_text(screenshot)
        # logger.info(f"ocr_text=\n{ocr_text}")

        screenshot_bbox = self.get_screenshot_bbox(screenshot)
        logger.info(f"screenshot_bbox=\n{screenshot_bbox}")

        screenshot_click_event_bbox = self.get_click_event_bbox(self.screenshots[self.screenshot_idx])
        logger.info(f"self.screenshots[self.screenshot_idx].action_event=\n{screenshot_click_event_bbox}")
        event_strs = [
            f"<{event}>"
            for event in self.recording.action_events
        ]
        history_strs = [
            f"<{completion}>"
            for completion in self.result_history
        ]
        prompt = " ".join(event_strs + history_strs)
        N = max(0, len(prompt) - MAX_INPUT_SIZE)
        prompt = prompt[N:]
        #logger.info(f"{prompt=}")
        max_tokens = 10
        completion = self.get_completion(prompt, max_tokens)
        #logger.info(f"{completion=}")

        # only take the first <...>
        result = completion.split(">")[0].strip(" <>")
        #logger.info(f"{result=}")
        self.result_history.append(result)

        # TODO: parse result into ActionEvent(s)
        self.screenshot_idx += 1
        return None

"""
Demonstration of HuggingFace, OCR, and ASCII ReplayStrategyMixins.

Usage:

    $ python -m openadapt.replay DemoReplayStrategy
"""

from loguru import logger
import numpy as np

from openadapt.events import get_events
from openadapt.models import Recording, Screenshot, WindowEvent
from openadapt.strategies.base import BaseReplayStrategy
from openadapt.strategies.mixins.huggingface import (
    HuggingFaceReplayStrategyMixin,
    MAX_INPUT_SIZE,
)
from openadapt.strategies.ocr_mixin import OCRReplayStrategyMixin
from openadapt.strategies.ascii_mixin import ASCIIReplayStrategyMixin
from openadapt.strategies.mixins.svg_mixin import SVGReplayStrategyMixin


class DemoReplayStrategy(
    HuggingFaceReplayStrategyMixin,
    OCRReplayStrategyMixin,
    ASCIIReplayStrategyMixin,
    SVGReplayStrategyMixin,
    BaseReplayStrategy,
):
    def __init__(
        self,
        recording: Recording,
    ):
        super().__init__(recording)
        self.result_history = []

    def get_next_action_event(
        self,
        screenshot: Screenshot,
        window_event: WindowEvent,
    ):
        ascii_text = self.get_ascii_text(screenshot)
        # logger.info(f"ascii_text=\n{ascii_text}")

        ocr_text = self.get_ocr_text(screenshot)
        # logger.info(f"ocr_text=\n{ocr_text}")

        svg_text = self.get_svg_text(screenshot)
        # logger.info(f"svg_text=\n{svg_text}")

        event_strs = [f"<{event}>" for event in self.recording.action_events]
        history_strs = [f"<{completion}>" for completion in self.result_history]
        prompt = " ".join(event_strs + history_strs)
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

        # TODO: parse result into ActionEvent(s)

        return None

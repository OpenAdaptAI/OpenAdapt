"""Demonstration of HuggingFace, OCR, and ASCII ReplayStrategyMixins.

Usage:

    $ python -m openadapt.replay DemoReplayStrategy
"""

from loguru import logger

from openadapt.db.crud import get_screenshots
from openadapt.models import Recording, Screenshot, WindowEvent
from openadapt.strategies.base import BaseReplayStrategy
from openadapt.strategies.mixins.ascii import ASCIIReplayStrategyMixin
from openadapt.strategies.mixins.huggingface import (
    MAX_INPUT_SIZE,
    HuggingFaceReplayStrategyMixin,
)
from openadapt.strategies.mixins.ocr import OCRReplayStrategyMixin
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
    """Demo replay strategy that combines HuggingFace, OCR, and ASCII mixins."""

    def __init__(
        self,
        recording: Recording,
    ) -> None:
        """Initialize the DemoReplayStrategy.

        Args:
            recording (Recording): The recording to replay.
        """
        super().__init__(recording)
        self.result_history = []
        self.screenshots = get_screenshots(recording)
        self.screenshot_idx = 0

    def get_next_action_event(
        self,
        screenshot: Screenshot,
        window_event: WindowEvent,
    ) -> None:
        """Get the next action event based on the current screenshot and window event.

        Args:
            screenshot (Screenshot): The current screenshot.
            window_event (WindowEvent): The current window event.

        Returns:
            None: No action event is returned in this demo strategy.
        """
        # ascii_text = self.get_ascii_text(screenshot)
        # logger.info(f"ascii_text=\n{ascii_text}")

        # ocr_text = self.get_ocr_text(screenshot)
        # logger.info(f"ocr_text=\n{ocr_text}")

        screenshot_bbox = self.get_screenshot_bbox(screenshot)
        logger.info(f"screenshot_bbox=\n{screenshot_bbox}")

        screenshot_click_event_bbox = self.get_click_event_bbox(
            self.screenshots[self.screenshot_idx]
        )
        logger.info(
            "self.screenshots[self.screenshot_idx].action_event=\n"
            f"{screenshot_click_event_bbox}"
        )
        event_strs = [f"<{event}>" for event in self.recording.action_events]
        history_strs = [f"<{completion}>" for completion in self.result_history]
        prompt = " ".join(event_strs + history_strs)
        N = max(0, len(prompt) - MAX_INPUT_SIZE)
        prompt = prompt[N:]
        # logger.info(f"{prompt=}")
        max_tokens = 10
        completion = self.get_completion(prompt, max_tokens)
        # logger.info(f"{completion=}")

        # only take the first <...>
        result = completion.split(">")[0].strip(" <>")
        # logger.info(f"{result=}")
        self.result_history.append(result)

        # TODO: parse result into ActionEvent(s)
        self.screenshot_idx += 1
        return None

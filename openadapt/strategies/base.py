"""Implements the base class for implementing replay strategies."""

from abc import ABC, abstractmethod
from pprint import pformat
import time

from loguru import logger
from oa_pynput import keyboard, mouse
import numpy as np

from openadapt import models, playback, utils

MAX_FRAME_TIMES = 1000


class BaseReplayStrategy(ABC):
    """Base class for implementing replay strategies."""

    def __init__(
        self,
        recording: models.Recording,
        max_frame_times: int = MAX_FRAME_TIMES,
    ) -> None:
        """Initialize the BaseReplayStrategy.

        Args:
            recording (models.Recording): The recording to replay.
            max_frame_times (int): The maximum number of frame times to track.
        """
        self.recording = recording
        self.max_frame_times = max_frame_times
        self.action_events = []
        self.screenshots = []
        self.window_events = []
        self.frame_times = []

    @abstractmethod
    def get_next_action_event(
        self,
        screenshot: models.Screenshot,
    ) -> models.ActionEvent:
        """Get the next action event based on the current screenshot.

        Args:
            screenshot (models.Screenshot): The current screenshot.

        Returns:
            models.ActionEvent: The next action event.
        """
        pass

    def run(self) -> None:
        """Run the replay strategy."""
        keyboard_controller = keyboard.Controller()
        mouse_controller = mouse.Controller()
        while True:
            screenshot = models.Screenshot.take_screenshot()
            self.screenshots.append(screenshot)
            window_event = models.WindowEvent.get_active_window_event()
            self.window_events.append(window_event)
            try:
                action_event = self.get_next_action_event(
                    screenshot,
                    window_event,
                )
            except StopIteration:
                break
            if self.action_events:
                prev_action_event = self.action_events[-1]
                if prev_action_event.timestamp and action_event.timestamp:
                    assert prev_action_event.timestamp <= action_event.timestamp, (
                        prev_action_event,
                        action_event,
                    )
                else:
                    logger.warning(
                        f"{prev_action_event.timestamp=} {action_event.timestamp=}"
                    )
            self.log_fps()
            if action_event:
                action_event_dict = utils.rows2dicts(
                    [action_event],
                    drop_constant=False,
                )[0]
                logger.debug(f"action_event=\n{pformat(action_event_dict)}")
                self.action_events.append(action_event)
                try:
                    playback.play_action_event(
                        action_event,
                        mouse_controller,
                        keyboard_controller,
                    )
                except Exception as exc:
                    logger.exception(exc)
                    import ipdb

                    ipdb.set_trace()

    def log_fps(self) -> None:
        """Log the frames per second (FPS) rate."""
        t = time.time()
        self.frame_times.append(t)
        dts = np.diff(self.frame_times)
        if len(dts) > 1:
            mean_dt = np.mean(dts)
            fps = 1 / mean_dt
            logger.info(f"{fps=:.2f}")
        if len(self.frame_times) > self.max_frame_times:
            self.frame_times.pop(0)

"""
Implements the base class for implementing replay strategies.
"""

from abc import ABC, abstractmethod
from pprint import pformat
import time

from loguru import logger
from pynput import keyboard, mouse
import mss.base
import numpy as np

from puterbot import models, playback, utils, window


MAX_FRAME_TIMES = 1000


class BaseReplayStrategy(ABC):

    def __init__(
        self,
        recording: models.Recording,
        max_frame_times: int = MAX_FRAME_TIMES,
    ):
        self.recording = recording
        self.max_frame_times = max_frame_times
        self.processed_action_events = recording.processed_action_events
        self.action_events = []
        self.screenshots = []
        self.window_events = []
        self.frame_times = []

    @abstractmethod
    def get_next_action_event(
        self,
        screenshot: models.Screenshot,
    ) -> models.ActionEvent:
        pass

    def run(self):
        keyboard_controller = keyboard.Controller()
        mouse_controller = mouse.Controller()
        while True:
            screenshot = models.Screenshot.take_screenshot()
            self.screenshots.append(screenshot)
            window_event = models.WindowEvent.get_active_window_event()
            self.window_events.append(window_event)
            try:
                action_event = self.get_next_action_event(
                    screenshot, window_event,
                )
            except StopIteration:
                break
            self.log_fps()
            self.action_events.append(action_event)
            if action_event:
                action_event_dict = utils.rows2dicts(
                    [action_event],
                    drop_constant=False,
                )[0]
                logger.info(
                    f"action_event=\n"
                    f"{pformat(action_event_dict)}"
                )
                playback.play_action_event(
                    action_event,
                    mouse_controller,
                    keyboard_controller,
                )

    def log_fps(self):
        t = time.time()
        self.frame_times.append(t)
        dts = np.diff(self.frame_times)
        if len(dts) > 1:
            mean_dt = np.mean(dts)
            fps = 1 / mean_dt
            logger.info(f"{fps=:.2f}")
        if len(self.frame_times) > self.max_frame_times:
            self.frame_times.pop(0)

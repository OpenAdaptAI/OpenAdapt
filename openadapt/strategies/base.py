"""
Implements the base class for implementing replay strategies.
"""

from abc import ABC, abstractmethod
import time

from loguru import logger
from pynput import keyboard, mouse
import mss.base
import numpy as np

from puterbot.models import ActionEvent, Recording, Screenshot
from puterbot.playback import play_action_event


MAX_FRAME_TIMES = 1000


class BaseReplayStrategy(ABC):

    def __init__(
        self,
        recording: Recording,
        max_frame_times: int = MAX_FRAME_TIMES,
    ):
        self.recording = recording
        self.max_frame_times = max_frame_times
        self.action_events = []
        self.screenshots = []
        self.frame_times = []

    @abstractmethod
    def get_next_action_event(
        self,
        screenshot: Screenshot,
    ) -> ActionEvent:
        pass

    def run(self):
        keyboard_controller = keyboard.Controller()
        mouse_controller = mouse.Controller()
        while True:
            screenshot = Screenshot.take_screenshot()
            self.screenshots.append(screenshot)
            try:
                action_event = self.get_next_action_event(screenshot)
            except StopIteration:
                break
            self.log_fps()
            self.action_events.append(action_event)
            if action_event:
                play_action_event(
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

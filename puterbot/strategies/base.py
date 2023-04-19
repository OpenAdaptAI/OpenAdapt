"""
Implements the base class for implementing replay strategies.
"""

from abc import ABC, abstractmethod
import time

from loguru import logger
from pynput import keyboard, mouse
import mss.base
import numpy as np

from puterbot.models import InputEvent, Recording, Screenshot
from puterbot.playback import play_input_event
from puterbot.utils import take_screenshot


MAX_FRAME_TIMES = 1000


class BaseReplayStrategy(ABC):

    def __init__(
        self,
        recording: Recording,
        max_frame_times: int = MAX_FRAME_TIMES,
    ):
        self.recording = recording
        self.max_frame_times = max_frame_times
        self.input_events = []
        self.screenshots = []
        self.frame_times = []

    @abstractmethod
    def get_next_input_event(
        self,
        screenshot: Screenshot,
    ) -> InputEvent:
        pass

    def run(self):
        keyboard_controller = keyboard.Controller()
        mouse_controller = mouse.Controller()
        while True:
            sct_img = take_screenshot()
            screenshot = Screenshot(sct_img=sct_img)
            self.screenshots.append(screenshot)
            try:
                input_event = self.get_next_input_event(screenshot)
            except StopIteration:
                break
            self.log_fps()
            self.input_events.append(input_event)
            if input_event:
                play_input_event(
                    input_event,
                    mouse_controller,
                    keyboard_controller,
                )

    def log_fps(self):
        t = time.time()
        self.frame_times.append(t)
        dts = np.diff(self.frame_times)
        if len(dts) > 1:
            mean_dt = np.mean(dts)
            fps = len(dts) / mean_dt
            logger.info(f"{fps=:.2f}")
        if len(self.frame_times) > self.max_frame_times:
            self.frame_times.pop(0)

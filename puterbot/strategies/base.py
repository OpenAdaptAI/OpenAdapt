"""
Implements the base class for implementing replay strategies.
"""

from abc import ABC, abstractmethod

from pynput import keyboard, mouse
import mss.base

from puterbot.models import Recording, InputEvent
from puterbot.playback import play_input_event
from puterbot.utils import get_screenshot


class BaseReplayStrategy(ABC):

    def __init__(
        self,
        recording: Recording,
    ):
        self.recording = recording
        self.input_events = []
        self.screenshots = []

    @abstractmethod
    def get_next_input_event(
        self,
        screenshot: mss.base.ScreenShot,
    ) -> InputEvent:
        pass

    def run(
        self,
    ):
        keyboard_controller = keyboard.Controller()
        mouse_controller = mouse.Controller()
        while True:
            screenshot = get_screenshot()
            self.screenshots.append(screenshot)
            try:
                input_event = self.get_next_input_event(screenshot)
            except StopIteration:
                break
            self.input_events.append(input_event)
            if input_event:
                play_input_event(
                    input_event,
                    mouse_controller,
                    keyboard_controller,
                )

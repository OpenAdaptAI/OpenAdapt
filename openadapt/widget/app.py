import kivy
from subprocess import Popen
import signal
from kivy.app import App
from kivy.uix.button import Button
from kivy.uix.floatlayout import FloatLayout
from kivy.core.window import Window
import pygetwindow as gw
from loguru import logger
import ctypes
from ctypes import wintypes
from kivy.clock import Clock
import sys
import pywinauto
import Quartz
from openadapt import window


class OpenAdaptWidget(FloatLayout):
    def __init__(self, **kwargs):
        super(OpenAdaptWidget, self).__init__(**kwargs)
        self.window = Window
        self.window.borderless = True
        self.window.size = (50, 50)
        self.window.clearcolor = (255, 255, 255)
        self.window.always_on_top = True
        self.PROC = None
        self.button = Button(background_normal="assets/logo.png")
        self.button.bind(on_press=self.callback)
        self.current_state = "default"
        self.add_widget(self.button)
        # Check for active window changes every 0.5 seconds
        self.prev_active_window_position = None
        self._active_window_checker = Clock.schedule_interval(
            self.position_above_active_window, 0.5
        )

    def position_above_active_window(self, *args):
        window_data = window.get_active_window_data()
        self.window.top = window_data["top"] + 10
        self.window.left = window_data["left"] + window_data["width"]
        if (self.window.top, self.window.right) != self.prev_active_window_position:
            self.prev_active_window_position = (self.window.top, self.window.left)
            if self.current_state == "replay_in_progress":
                self.current_state == "replay_paused"

    def callback(self, instance):
        if self.current_state == "default":
            self.button.background_normal = "assets/recording_inprogress.png"
            self.current_state = "recording_in_progress"
            self.start_recording()
        elif self.current_state == "recording_in_progress":
            self.button.background_normal = "assets/replay_available.png"
            self.current_state = "replay_available"
            self.stop_recording()
        elif self.current_state == "replay_available":
            self.button.background_normal = "assets/replay_inprogress.png"
            self.current_state = "replaying_in_progress"
            self.replay_recording()
        elif self.current_state == "replaying_in_progress":
            self.button.background_normal = "assets/replay_paused"
            self.current_state = "replaying_paused"
            self.pause_replay()
        elif self.current_state == "replaying_paused":
            self.button.background_normal = "assets/replay_inprogress.png"
            self.current_state = "replaying_in_progress"
            self.resume_replay()

    def start_recording(self):
        self.PROC = Popen(
            "python -m openadapt.record " + "test",
            shell=True,
        )

    def stop_recording(self):
        try:
            self.PROC.send_signal(signal.CTRL_C_EVENT)
            # Wait for process to terminate
            self.PROC.wait()
        except KeyboardInterrupt:
            # Catch the KeyboardInterrupt exception to prevent termination
            pass
        self.PROC = None

    def replay_recording(self):
        self.PROC = Popen(
            "python -m openadapt.replay " + "NaiveReplayStrategy",
            shell=True,
        )

    def pause_replay(self):
        self.PROC.send_signal(signal.SIGSTOP)

    def resume_replay(self):
        self.PROC.send_signal(signal.SIGCONT)


class OpenAdapt(App):
    def build(self):
        return OpenAdaptWidget()


if __name__ == "__main__":
    OpenAdapt().run()

from subprocess import Popen
import signal
import sys
from kivy.app import App
from kivy.uix.button import Button
from kivy.uix.floatlayout import FloatLayout
from kivy.core.window import Window
from kivy.config import Config
from loguru import logger
from kivy.clock import Clock
from openadapt import window
from PIL import ImageGrab


SCREEN_SCALE = Window.dpi / 96.0
WIDGET_HEIGHT = 50
WIDGET_WIDTH = 50

class OpenAdapt(FloatLayout):
    def __init__(self, **kwargs):
        """
        A Kivy widget for controlling screen recording and replaying.

        Args:
            kwargs: Additional keyword arguments to pass to the parent class.
        """
        super(OpenAdapt, self).__init__(**kwargs)
        self.window = Window
        self.window.borderless = True
        self.window.size = (WIDGET_HEIGHT, WIDGET_WIDTH)
        self.window.clearcolor = (255, 255, 255, 0.5)
        self.window.always_on_top = True
        self.record_proc = None
        self.replay_proc = None
        self.button = Button(background_normal="assets/logo.png")
        self.button.bind(on_press=self.callback)
        self.current_state = "default"
        self.add_widget(self.button)
        self.prev_active_window_position = None
        Clock.schedule_interval(self.position_above_active_window, 0.5)

    def position_above_active_window(self, *args):
        """
        Positions the OpenAdapt window above the currently active window.

        This function is called periodically to update the position of the OpenAdapt window
        to be above the currently active window on the screen.

        Args:
            args: Additional positional arguments (not used).
        """
        window_data = window.get_active_window_data(False)
        if (
            window_data
            and (window_data["top"], window_data["left"])
            != self.prev_active_window_position
            and window_data["title"] != "OpenAdaptWidget"
        ):
            if sys.platform == "darwin":
                # Adjust top and left coordinates for macOS
                top = ((window_data["top"]) // SCREEN_SCALE) - WIDGET_HEIGHT
                left = window_data["left"] // SCREEN_SCALE
            elif sys.platform in ("win32", "linux"):
                # Adjust top and left coordinates for Windows and Linux
                top = ((window_data["top"]) // SCREEN_SCALE) - WIDGET_HEIGHT
                left = (
                    (window_data["left"] + window_data["width"]) // SCREEN_SCALE
                ) - WIDGET_WIDTH
            else:
                raise Exception(f"Unsupposed {sys.platform=}")
            # Get screen resolution and adjust coordinates accordingly
            resolution = ImageGrab.grab().size
            screen_width, screen_height = (
                resolution[0] // SCREEN_SCALE,
                resolution[1] // SCREEN_SCALE,
            )
            if top >= 0 and left >= 0 and top <= screen_height and left <= screen_width:
                # Widget coordinates are within the screen boundaries
                self.window.top = top
                self.window.left = left
            else:
                # Widget coordinates are outside the screen boundaries
                self.window.top = 0
                self.window.left = screen_width - WIDGET_WIDTH
            self.prev_active_window_position = (self.window.top, self.window.left)
            if self.current_state == "replay_in_progress":
                self.current_state == "replay_paused"

    def callback(self, instance):
        # Handle button press based on the current state
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
            self.button.background_normal = "assets/replay_paused.png"
            self.current_state = "replaying_paused"
            self.pause_replay()
        elif self.current_state == "replaying_paused":
            self.button.background_normal = "assets/replay_inprogress.png"
            self.current_state = "replaying_in_progress"
            self.resume_replay()

    def start_recording(self):
        self.record_proc = Popen(
            "python -m openadapt.record " + "test",
            shell=True,
        )

    def stop_recording(self):
        self.record_proc.terminate()

    def replay_recording(self):
        self.replay_proc = Popen(
            "python -m openadapt.replay " + "NaiveReplayStrategy",
            shell=True,
        )

    def pause_replay(self):
        self.replay_proc.send_signal(signal.SIGSTOP)

    def resume_replay(self):
        self.replay_proc.send_signal(signal.SIGCONT)


class OpenAdaptWidget(App):
    def build(self):
        return OpenAdapt()


if __name__ == "__main__":
    OpenAdaptWidget().run()

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


SCREEN_SCALE = (Window.dpi / 96.0)
class OpenAdapt(FloatLayout):
    def __init__(self, **kwargs):
        super(OpenAdapt, self).__init__(**kwargs)
        self.window = Window
        self.window.borderless = True
        self.window.opacity = 0.5
        self.window.size = (50, 50)
        self.window.clearcolor = (255, 255, 255, 0.5)
        self.window.always_on_top = True
        self.record_proc = None
        self.replay_proc = None
        self.window.position = "custom"
        self.button = Button(background_normal="assets/logo.png")
        self.button.bind(on_press=self.callback)
        self.current_state = "default"
        self.add_widget(self.button)
        self.prev_active_window_position = None
        Clock.schedule_interval(self.position_above_active_window, 0.5)
    
    def position_above_active_window(self, *args):
        window_data = window.get_active_window_data(False)
        if (
          window_data
            and (window_data["top"], window_data["left"])
            != self.prev_active_window_position and window_data["title"] != "OpenAdaptWidget"
        ):
            if sys.platform == "darwin" :
                top = ((window_data["top"])// SCREEN_SCALE)-50
                left = (window_data["left"] // SCREEN_SCALE)
            elif sys.platform in ("win32", "linux") :
                top = ((window_data["top"])// SCREEN_SCALE)-50
                left = ((window_data["left"]+window_data["width"]) // SCREEN_SCALE)-50
            else :
                raise Exception(f"Unsupposed {sys.platform=}")
            resolution = ImageGrab.grab().size
            screen_width, screen_height = resolution[0] // SCREEN_SCALE, resolution[1] // SCREEN_SCALE
            logger.info(f"{screen_width=},{screen_height=}")
            if top >= 0 and left >= 0 and top <= screen_height and left <= screen_width:
                self.window.top = top
                self.window.left = left
            else :
                self.window.top = 0
                self.window.left = screen_width - 50
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

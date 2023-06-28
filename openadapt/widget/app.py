from subprocess import Popen
import signal
from kivy.app import App
from kivy.uix.button import Button
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.gridlayout import GridLayout
from kivy.core.window import Window
from loguru import logger
from kivy.clock import Clock
from openadapt import window


class OpenAdaptWidget(FloatLayout):
    def __init__(self, **kwargs):
        super(OpenAdaptWidget, self).__init__(**kwargs)
        self.window = Window
        self.window.borderless = True
        self.window.size = (50, 50)
        self.window.clearcolor = (255, 255, 255, 0.5)
        self.window.always_on_top = True
        self.record_proc = None
        self.replay_proc = None
        self.button = Button(background_normal="assets/logo.png")
        self.button.bind(on_press=self.callback)
        self.current_state = "default"
        self.add_widget(self.button)
        # add border
        # create new window, top left, top middle top right, ..., query size , verify pos window correctly,
        # Check for active window changes every 0.5 , detect when window moves instead of using clock
        self.prev_active_window_position = None
        Clock.schedule_interval(self.position_above_active_window, 0.5)

    def position_above_active_window(self, *args):
        window_data = window.get_active_window_state(False)
        if (
            window_data
            and (window_data["top"], window_data["left"])
            != self.prev_active_window_position
        ):
            self.window.top = window_data["top"] - 30
            self.window.left = window_data["left"] + window_data["width"]
            top, left, width = (
                window_data["top"],
                window_data["left"],
                window_data["width"],
            )
            logger.info(f"widget_top={self.window.top},widget_left{self.window.left}")
            logger.info(
                f"active_top={top},active_right={left+width},active_width={width}, active_left={left}"
            )
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
        self.record_proc = Popen(
            "python -m openadapt.record " + "test",
            shell=True,
        )

    def stop_recording(self):
        self.record_proc.send_signal(signal.CTRL_C_EVENT)
        self.record_proc.wait()

    def replay_recording(self):
        self.replay_proc = Popen(
            "python -m openadapt.replay " + "NaiveReplayStrategy",
            shell=True,
        )

    def pause_replay(self):
        self.replay_proc.send_signal(signal.SIGSTOP)

    def resume_replay(self):
        self.replay_proc.send_signal(signal.SIGCONT)


class OpenAdapt(App):
    def build(self):
        return OpenAdaptWidget()


if __name__ == "__main__":
    OpenAdapt().run()

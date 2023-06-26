import kivy
from subprocess import Popen
import signal
from kivy.app import App
from kivy.uix.button import Button
from kivy.uix.floatlayout import FloatLayout
from kivy.core.window import Window


class OpenAdaptWidget(FloatLayout):
    def __init__(self, **kwargs):
        super(OpenAdaptWidget, self).__init__(**kwargs)
        self.window = Window
        self.window.borderless = True
        self.window.size = (200, 200)
        self.window.clearcolor = (255, 255, 255)
        self.window.always_on_top = True
        self.PROC = None
        self.button = Button(background_normal="assets/logo.png")
        self.button.bind(on_press=self.callback)
        self.current_state = "default"
        self.add_widget(self.button)

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



class OpenAdapt(App):
    def build(self):
        return OpenAdaptWidget()


if __name__ == "__main__":
    OpenAdapt().run()

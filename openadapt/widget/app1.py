import sys
from subprocess import Popen
import signal
from PySide6.QtWidgets import QApplication, QMainWindow, QPushButton, QVBoxLayout, QWidget
from PySide6.QtCore import QTimer, Qt,QSize
from PySide6.QtGui import QIcon
from loguru import logger
from openadapt import window
#SCREEN_SCALE = 96.0 / QApplication.primaryScreen().logicalDotsPerInch()
WIDGET_HEIGHT = 50
WIDGET_WIDTH = 50

class OpenAdapt(QMainWindow):
    def __init__(self, parent=None):
        super(OpenAdapt, self).__init__(parent)
        self.setWindowTitle("OpenAdapt")
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(WIDGET_WIDTH, WIDGET_HEIGHT)
        self.setStyleSheet("background-color: rgba(255, 255, 255, 0.5);")
        self.record_proc = None
        self.replay_proc = None
        self.button = QPushButton(self)
        self.button.setIcon(QIcon("assets/logo.png"))
        self.button.setIconSize(QSize(WIDGET_WIDTH, WIDGET_HEIGHT))
        self.button.clicked.connect(self.callback)
        self.current_state = "default"
        
        central_widget = QWidget(self)
        self.setCentralWidget(central_widget)
        
        layout = QVBoxLayout(central_widget)
        layout.addWidget(self.button)
        
        self.prev_active_window_position = None
        self.timer = QTimer()
        self.timer.timeout.connect(self.position_above_active_window)
        self.timer.start(500)  # Update position every 500 milliseconds

    def position_above_active_window(self):
        active_window = window.get_active_window_data(False)
        if active_window is not None:
            if (
                active_window
                and (active_window["top"], active_window["left"])
                != self.prev_active_window_position
                and active_window["title"] != "OpenAdapt"
            ):
                top =active_window["top"]
                left = active_window["left"] + active_window["width"]
                screen_geometry = QApplication.primaryScreen().geometry()
                screen_width, screen_height = screen_geometry.width(), screen_geometry.height()
                if top >= 0 and left >= 0 and top <= screen_height and left <= screen_width:
                    self.move(left, top)
                else:
                    self.move(screen_width - WIDGET_WIDTH, 0)
                self.prev_active_window_position = (self.pos().y(), self.pos().x())
                if self.current_state == "replaying_in_progress":
                    self.current_state == "replaying_paused"

    def callback(self):
        if self.current_state == "default":
            self.button.setIcon(QIcon("assets/recording_inprogress.png"))
            self.current_state = "recording_in_progress"
            self.start_recording()
        elif self.current_state == "recording_in_progress":
            self.button.setIcon(QIcon("assets/replay_available.png"))
            self.current_state = "replay_available"
            self.stop_recording()
        elif self.current_state == "replay_available":
            self.button.setIcon(QIcon("assets/replay_inprogress.png"))
            self.current_state = "replaying_in_progress"
            self.replay_recording()
        elif self.current_state == "replaying_in_progress":
            self.button.setIcon(QIcon("assets/replay_paused.png"))
            self.current_state = "replaying_paused"
            self.pause_replay()
        elif self.current_state == "replaying_paused":
            self.button.setIcon(QIcon("assets/replay_inprogress.png"))
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
        if sys.platform == "win32":
            self.replay_proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            self.replay_proc.send_signal(signal.SIGSTOP)

    def resume_replay(self):
        if sys.platform == "win32":
            self.replay_proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            self.replay_proc.send_signal(signal.SIGCONT)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    openadapt = OpenAdapt()
    openadapt.show()
    sys.exit(app.exec())

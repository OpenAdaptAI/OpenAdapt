import sys

from loguru import logger
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QVBoxLayout, QWidget
import fire


class RecordingWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Recording In Progress")

        layout = QVBoxLayout()
        self.label = QLabel("Recording in progress...")
        layout.addWidget(self.label)

        self.stop_button = QPushButton("Stop Recording")
        self.stop_button.clicked.connect(self.stop_recording)
        layout.addWidget(self.stop_button)

        self.setLayout(layout)

    def stop_recording(self):
        self.label.setText("Recording has been finished")
        self.stop_button.setEnabled(False)


class MainApplication(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Replay Strategy")

        layout = QVBoxLayout()

        self.label = QLabel("Replay strategy has failed. Please take over.")
        layout.addWidget(self.label)

        self.yes_button = QPushButton("Yes")
        self.yes_button.clicked.connect(self.start_recording)
        layout.addWidget(self.yes_button)

        self.no_button = QPushButton("No")
        self.no_button.clicked.connect(self.skip_recording)
        layout.addWidget(self.no_button)

        self.setLayout(layout)

    def start_recording(self):
        self.recording_window = RecordingWindow()
        self.recording_window.show()
        self.hide()

    def skip_recording(self):
        logger.info("Skipped")
        self.close()


def run_replay_control():
    app = QApplication(sys.argv)
    main_app = MainApplication()
    main_app.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    fire.Fire(run_replay_control)

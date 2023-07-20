import sys
from subprocess import Popen
import signal
import threading
import time

from PySide6 import QtWidgets, QtCore, QtGui

class SystemTrayIcon(QtWidgets.QSystemTrayIcon):
    def __init__(self, icon, parent=None):
        QtWidgets.QSystemTrayIcon.__init__(self, icon, parent)
        self.setToolTip('OpenAdapt')
        self.menu = QtWidgets.QMenu()
        self.action = self.menu.addAction("Exit")
        self.action.triggered.connect(parent.quit)
        self.setContextMenu(self.menu)
        self.activated.connect(self.update_icon)

        self.icon_recording = QtGui.QIcon("assets/recording_inprogress.png")
        self.icon_replay_available = QtGui.QIcon("assets/replay_available.png")
        self.icon_replaying = QtGui.QIcon("assets/replay_inprogress.png")
        self.icon_paused = QtGui.QIcon("assets/replay_paused.png")
        self.icon_default = QtGui.QIcon("assets/logo.png")
        self.setIcon(self.icon_default)
        self.current_state = "default"

    def update_icon(self,reason):
        if reason == QtWidgets.QSystemTrayIcon.Trigger :
            if self.current_state == "default":
                self.setIcon(self.icon_recording)
                self.current_state = "recording_in_progress"
                self.start_recording()
            elif self.current_state == "recording_in_progress":
                self.setIcon(self.icon_replay_available)
                self.current_state = "replay_available"
                self.stop_recording()
                #self.action.setText("Stop Recording")
            elif self.current_state == "replay_available" and self.record_proc is None:
                self.setIcon(self.icon_replaying)
                self.current_state = "replaying_in_progress"
                self.replay_recording()
            elif self.current_state == "replaying_in_progress":
                self.setIcon(self.icon_paused)
                self.current_state = "replaying_paused"
                self.pause_replay()
            elif self.current_state == "replaying_paused":
                self.setIcon(self.icon_replaying)
                self.current_state = "replaying_in_progress"
                self.resume_replay()

    def start_recording(self):
        #poetry run?
        self.record_proc = Popen(
            "python -m openadapt.record " + "test",
            shell=True,
        )

    def stop_recording(self):
        if self.record_proc is not None :
            self.record_proc.send_signal(signal.SIGINT)
            self.record_proc.wait()
            self.record_proc = None

    def replay_recording(self):
        self.replay_proc = Popen(
            "python -m openadapt.replay " + "NaiveReplayStrategy",
            shell=True,
        )

    def pause_replay(self):
        self.replay_proc.send_signal(signal.SIGSTOP)

    def resume_replay(self):
        self.replay_proc.send_signal(signal.SIGCONT)

def main(image):
    app = QtWidgets.QApplication(sys.argv)

    #w = QtWidgets.QWidget()
    tray_icon = SystemTrayIcon(QtGui.QIcon(image), app)
    tray_icon.show()

    sys.exit(app.exec())

if __name__ == '__main__':
    main("assets/logo.png")

import os
import sys
import threading
from functools import partial

from notifypy import Notify
from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from openadapt.app.cards import quick_record, stop_record
from openadapt.app.main import FPATH, start
from openadapt.crud import get_all_recordings
from openadapt.visualize import main as visualize

# hide dock icon on macos
if sys.platform == "darwin":
    import AppKit

    info = AppKit.NSBundle.mainBundle().infoDictionary()
    info["LSBackgroundOnly"] = "1"


class SystemTrayIcon(QSystemTrayIcon):
    recording = False
    app_thread = None
    recording_actions = []

    def __init__(self):
        self.app = QApplication([])
        self.app.setQuitOnLastWindowClosed(False)

        self.icon = QIcon(f"{FPATH}{os.sep}assets{os.sep}logo_inverted.png")

        self.tray = QSystemTrayIcon()
        self.tray.setIcon(self.icon)
        self.tray.setVisible(True)

        super().__init__(self.icon, self.app)

        self.menu = QMenu()

        self.record_action = QAction("Record")
        self.record_action.triggered.connect(self._quick_record)
        self.menu.addAction(self.record_action)

        self.visualize_menu = self.menu.addMenu("Visualize")

        self.populate_visualize_menu()

        self.app_action = QAction("Open OpenAdapt")
        self.app_action.triggered.connect(self.show_app)
        self.menu.addAction(self.app_action)

        self.quit = QAction("Quit")
        self.quit.triggered.connect(self.app.quit)
        self.menu.addAction(self.quit)

        self.tray.setContextMenu(self.menu)

        self.timer = QTimer()
        self.timer.setInterval(1000)  # update every second
        self.timer.timeout.connect(self.update_tray_icon)
        self.timer.start()

        # Variables to track the current icon state
        # TODO: use this somewhere, maybe? (e.g. to show if recording is active)
        self.current_icon = f"{FPATH}{os.sep}assets{os.sep}logo.png"
        self.icon_mapping = {
            f"{FPATH}{os.sep}assets{os.sep}logo.png": f"{FPATH}{os.sep}assets{os.sep}logo_inverted.png",
            f"{FPATH}{os.sep}assets{os.sep}logo_inverted.png": f"{FPATH}{os.sep}assets{os.sep}logo.png",
        }

        Notify("Status", "OpenAdapt is running in the background.", "OpenAdapt").send()

    def cycle_icon(self):
        new_icon = self.icon_mapping[self.current_icon]
        self.tray.setIcon(QIcon(new_icon))
        self.current_icon = new_icon

    def update_tray_icon(self):
        try:
            if self.recording:
                self.record_action.setText("Stop recording")
            else:
                self.record_action.setText("Record")
            self.populate_visualize_menu()
        except KeyboardInterrupt:
            # the app is probably shutting down, so we can ignore this
            pass

    def _quick_record(self):
        if not self.recording:
            Notify("Status", "Starting recording...", "OpenAdapt").send()
            self.recording = True
            try:
                quick_record()
            except KeyboardInterrupt:
                self.recording = False
                stop_record()
        else:
            Notify("Status", "Stopping recording...", "OpenAdapt").send()
            self.recording = False
            stop_record()
            Notify("Status", "Recording stopped", "OpenAdapt").send()

    def _visualize(self, recording, *args, **kwargs):
        Notify("Status", "Starting visualization...", "OpenAdapt").send()
        visualize(recording)
        Notify("Status", "Visualization finished", "OpenAdapt").send()

    def populate_visualize_menu(self):
        self.recordings = get_all_recordings()
        for idx, recording in enumerate(self.recordings):
            self.recording_actions.append(QAction(f"{recording.task_description}"))
            self.recording_actions[idx].triggered.connect(
                partial(self._visualize, recording)
            )
            self.visualize_menu.addAction(self.recording_actions[idx])

    def show_app(self):
        if self.app_thread is None or not self.app_thread.is_alive():
            self.app_thread = threading.Thread(target=start, daemon=True)
            self.app_thread.start()

    def run(self):
        self.app.exec_()


def run():
    tray = SystemTrayIcon()
    tray.run()


if __name__ == "__main__":
    run()

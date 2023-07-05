from functools import partial
import os

from notifypy import Notify
from PySide6.QtGui import QIcon, QAction
from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
from openadapt import visualize

from openadapt.app.cards import quick_record
from openadapt.crud import get_all_recordings

FPATH = f"{os.path.dirname(os.path.abspath(__file__))}"


class SystemTrayIcon(QSystemTrayIcon):
    def __init__(self):
        self.app = QApplication([])
        self.app.setQuitOnLastWindowClosed(False)

        self.icon = QIcon(f"{FPATH}{os.sep}assets{os.sep}logo_inverted.png")

        self.tray = QSystemTrayIcon()
        self.tray.setIcon(self.icon)
        self.tray.setVisible(True)

        self.menu = QMenu()

        self.record_action = QAction("Record")
        self.record_action.triggered.connect(self._quick_record)
        self.menu.addAction(self.record_action)

        self.visualize_menu = self.menu.addMenu("Visualize")
        self.populate_visualize_menu()

        self.quit = QAction("Quit")
        self.quit.triggered.connect(self.app.quit)
        self.menu.addAction(self.quit)

        self.tray.setContextMenu(self.menu)

    def _quick_record(self):
        Notify("OpenAdapt", "Starting recording...", "OpenAdapt").send()
        quick_record()
        Notify("OpenAdapt", "Recording stopped", "OpenAdapt").send()

    def callback(self, recording, *args, **kwargs):
        visualize.main(recording)

    def populate_visualize_menu(self):
        recordings = get_all_recordings()

        latest = QAction("latest")
        latest.triggered.connect(visualize.main)
        self.visualize_menu.addAction(latest)

        for recording in recordings[1:]:
            action = QAction(f"{recording.task_description}")
            action.triggered.connect(partial(self.callback, recording))
            self.visualize_menu.addAction(action)

    def run(self):
        self.app.exec_()


if __name__ == "__main__":
    tray = SystemTrayIcon()
    tray.run()

"""Implementation of the system tray icon for OpenAdapt.

usage: `python -m openadapt.app.tray` or `poetry run app`
"""

from functools import partial
from subprocess import Popen
import os
import sys

from loguru import logger
from notifypy import Notify
from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from openadapt.app.cards import quick_record, stop_record
from openadapt.app.dashboard.run import cleanup as cleanup_dashboard
from openadapt.app.dashboard.run import run as run_dashboard
from openadapt.app.main import FPATH, start
from openadapt.config import config, print_config
from openadapt.db.crud import get_all_recordings
from openadapt.extensions.thread import Thread as oaThread
from openadapt.models import Recording
from openadapt.replay import replay

# hide dock icon on macos
if sys.platform == "darwin":
    import AppKit

    info = AppKit.NSBundle.mainBundle().infoDictionary()
    info["LSBackgroundOnly"] = "1"


class SystemTrayIcon(QSystemTrayIcon):
    """System tray icon for OpenAdapt."""

    recording = False
    app_thread = None
    dashboard_thread = None

    # the actions need to be separated by type
    # or else they will be triggered multiple times
    recording_actions = {"visualize": [], "replay": []}

    def __init__(self) -> None:
        """Initialize the system tray icon."""
        self.app = QApplication([])
        self.app.setQuitOnLastWindowClosed(False)

        self.icon = QIcon(f"{FPATH}{os.sep}assets{os.sep}logo.png")

        self.tray = QSystemTrayIcon()
        self.tray.setIcon(self.icon)
        self.tray.setVisible(True)

        super().__init__(self.icon, self.app)

        self.menu = QMenu()

        self.record_action = QAction("Record")
        self.record_action.triggered.connect(self._quick_record)
        self.menu.addAction(self.record_action)

        self.visualize_menu = self.menu.addMenu("Visualize")
        self.populate_menu(self.visualize_menu, self._visualize, "visualize")

        self.replay_menu = self.menu.addMenu("Replay")
        self.populate_menu(self.replay_menu, self._replay, "replay")

        # TODO: Remove this action once dashboard is integrated
        self.app_action = QAction("Show App")
        self.app_action.triggered.connect(self.show_app)
        self.menu.addAction(self.app_action)

        self.dashboard_action = QAction("Launch Dashboard")
        self.dashboard_action.triggered.connect(self.launch_dashboard)
        self.menu.addAction(self.dashboard_action)

        self.quit = QAction("Quit")

        def _quit() -> None:
            """Quit the application."""
            if self.dashboard_thread is not None:
                cleanup_dashboard(self.dashboard_thread._return)
            self.app.quit()

        self.quit.triggered.connect(_quit)
        self.menu.addAction(self.quit)

        self.tray.setContextMenu(self.menu)

        self.timer = QTimer()
        self.timer.setInterval(1000)  # update every second
        self.timer.timeout.connect(self.update_tray_icon)
        self.timer.start()

        self.visualize_proc = None

        Notify("Status", "OpenAdapt is running in the background.", "OpenAdapt").send()

    def update_tray_icon(self) -> None:
        """Update the tray icon."""
        try:
            if self.recording:
                self.record_action.setText("Stop recording")
            else:
                self.record_action.setText("Record")

            self.populate_menu(self.visualize_menu, self._visualize, "visualize")
            self.populate_menu(self.replay_menu, self._replay, "replay")

            if self.dashboard_thread and config.ENV != "build":
                self.dashboard_action.setText("Reload dashboard")
        except KeyboardInterrupt:
            # the app is probably shutting down, so we can ignore this
            pass

    def _quick_record(self) -> None:
        """Wrapper for the quick_record function."""
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

    def _visualize(self, recording: Recording) -> None:
        """Visualize a recording.

        Args:
            recording (Recording): The recording to visualize.
        """
        Notify("Status", "Starting visualization...", "OpenAdapt").send()
        try:
            if self.visualize_proc is not None:
                self.visualize_proc.kill()
            self.visualize_proc = Popen(
                f"python -m openadapt.visualize --timestamp {recording.timestamp}",
                shell=True,
            )

        except Exception as e:
            logger.error(e)
            Notify("Status", "Visualization failed", "OpenAdapt").send()

    def _replay(self, recording: Recording) -> None:
        """Replay a recording.

        Args:
            recording (Recording): The recording to replay.
        """
        Notify("Status", "Starting replay...", "OpenAdapt").send()
        rthread = oaThread(target=replay, args=("NaiveReplayStrategy", None, recording))
        rthread.run()
        if rthread.join():
            Notify("Status", "Replay finished", "OpenAdapt").send()
        else:
            Notify("Status", "Replay failed", "OpenAdapt").send()

    def populate_menu(self, menu: QMenu, action: QAction, action_type: str) -> None:
        """Populate a menu.

        Args:
            menu (QMenu): The menu to populate.
            action (QAction): The action to perform when the menu item is clicked.
            action_type (str): The type of action to perform ["visualize", "replay"]
        """
        recordings = get_all_recordings()
        if len(recordings) == len((self.recording_actions[action_type])):
            return
        else:
            self.recording_actions[action_type] = []
        for idx, recording in enumerate(recordings):
            self.recording_actions[action_type].append(
                QAction(f"{recording.task_description}")
            )
            self.recording_actions[action_type][idx].triggered.connect(
                partial(action, recording)
            )
            menu.addAction(self.recording_actions[action_type][idx])

    def show_app(self) -> None:
        """Show the main application window."""
        if self.app_thread is None or not self.app_thread.is_alive():
            self.app_thread = oaThread(target=start, daemon=True, args=(True,))
            self.app_thread.start()

    def launch_dashboard(self) -> None:
        """Launch the web dashboard."""
        if self.dashboard_thread:
            if config.ENV == "build":
                return
            cleanup_dashboard(self.dashboard_thread._return)
            self.dashboard_thread.join()
        self.dashboard_thread = run_dashboard()
        self.dashboard_thread.start()

    def run(self) -> None:
        """Run the system tray icon."""
        self.app.exec()


def _run() -> None:
    tray = SystemTrayIcon()
    tray.run()


if __name__ == "__main__":
    print_config()
    _run()

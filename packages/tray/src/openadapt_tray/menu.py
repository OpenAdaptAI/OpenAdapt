"""Menu construction and actions for OpenAdapt Tray."""

from typing import TYPE_CHECKING, Optional, List
from functools import partial
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import subprocess
import shutil
import webbrowser

import pystray
from pystray import MenuItem as Item, Menu

if TYPE_CHECKING:
    from openadapt_tray.app import TrayApplication

from openadapt_tray.state import TrayState


@dataclass
class CaptureInfo:
    """Information about a capture."""

    name: str
    path: str
    timestamp: str


class MenuBuilder:
    """Builds the system tray context menu."""

    def __init__(self, app: "TrayApplication"):
        """Initialize the menu builder.

        Args:
            app: The parent TrayApplication instance.
        """
        self.app = app

    def build(self) -> Menu:
        """Build the current menu based on application state.

        Returns:
            pystray Menu instance.
        """
        state = self.app.state.current

        items = [
            self._build_recording_item(state),
            Menu.SEPARATOR,
            self._build_captures_submenu(),
            self._build_training_item(state),
            Menu.SEPARATOR,
            Item("Open Dashboard", self._open_dashboard),
            Item("Settings...", self._open_settings),
            Menu.SEPARATOR,
            Item("Quit", self._quit),
        ]

        return Menu(*items)

    def _build_recording_item(self, state) -> Item:
        """Build record/stop recording menu item.

        Args:
            state: Current application state.

        Returns:
            Menu item for recording control.
        """
        if state.state == TrayState.RECORDING:
            label = f"Stop Recording ({state.current_capture})"
            return Item(label, lambda: self.app.stop_recording())
        elif state.state == TrayState.RECORDING_STARTING:
            return Item("Starting...", None, enabled=False)
        elif state.state == TrayState.RECORDING_STOPPING:
            return Item("Stopping...", None, enabled=False)
        else:
            hotkey = self.app.config.hotkeys.toggle_recording
            label = f"Start Recording ({hotkey})"
            return Item(
                label,
                lambda: self.app.start_recording(),
                enabled=state.can_start_recording(),
            )

    def _build_captures_submenu(self) -> Item:
        """Build captures submenu.

        Returns:
            Menu item with captures submenu.
        """
        captures = self._get_recent_captures()

        if not captures:
            return Item(
                "Recent Captures",
                Menu(Item("No captures", None, enabled=False)),
            )

        capture_items = []
        for c in captures[:10]:  # Limit to 10 most recent
            capture_items.append(
                Item(
                    f"{c.name} ({c.timestamp})",
                    Menu(
                        Item("View", partial(self._view_capture, c.path)),
                        Item("Delete", partial(self._delete_capture, c.path, c.name)),
                    ),
                )
            )

        capture_items.append(Menu.SEPARATOR)
        capture_items.append(Item("View All...", self._open_captures_list))

        return Item("Recent Captures", Menu(*capture_items))

    def _build_training_item(self, state) -> Item:
        """Build training status/control item.

        Args:
            state: Current application state.

        Returns:
            Menu item for training control.
        """
        if state.state == TrayState.TRAINING:
            progress = state.training_progress or 0
            return Item(
                f"Training: {progress:.0%}",
                Menu(
                    Item("View Progress", self._open_training_dashboard),
                    Item("Stop Training", self._stop_training),
                ),
            )
        else:
            return Item(
                "Training",
                Menu(
                    Item("Start Training...", self._start_training),
                    Item("View Last Results", self._view_training_results),
                ),
            )

    def _get_recent_captures(self) -> List[CaptureInfo]:
        """Get list of recent captures.

        Returns:
            List of CaptureInfo objects.
        """
        try:
            captures_dir = self.app.config.get_captures_path()
            if not captures_dir.exists():
                return []

            captures = []
            for d in sorted(
                captures_dir.iterdir(),
                key=lambda x: x.stat().st_mtime,
                reverse=True,
            ):
                if d.is_dir():
                    # Check for metadata.json (formal capture)
                    # or just any directory (simpler check)
                    mtime = datetime.fromtimestamp(d.stat().st_mtime)
                    captures.append(
                        CaptureInfo(
                            name=d.name,
                            path=str(d),
                            timestamp=mtime.strftime("%Y-%m-%d %H:%M"),
                        )
                    )

            return captures[:10]  # Limit to 10 most recent
        except Exception as e:
            print(f"Error getting captures: {e}")
            return []

    def _open_dashboard(self) -> None:
        """Open the web dashboard."""
        self.app._open_dashboard()

    def _open_settings(self) -> None:
        """Open settings dialog."""
        self.app.platform.open_settings_dialog(self.app.config)

    def _quit(self) -> None:
        """Quit the application."""
        self.app.quit()

    def _view_capture(self, path: str) -> None:
        """View a capture.

        Args:
            path: Path to capture directory.
        """
        try:
            # Try using openadapt CLI first
            subprocess.run(
                ["openadapt", "visualize", path],
                check=False,
                capture_output=True,
            )
        except FileNotFoundError:
            # Fallback: open in file browser
            self._open_in_file_browser(path)

    def _delete_capture(self, path: str, name: str) -> None:
        """Delete a capture after confirmation.

        Args:
            path: Path to capture directory.
            name: Capture name for display.
        """
        if self.app.platform.confirm_dialog(
            "Delete Capture",
            f"Are you sure you want to delete this capture?\n\n{name}",
        ):
            try:
                shutil.rmtree(path)
                self.app.notifications.show(
                    "Capture Deleted",
                    f"'{name}' has been removed.",
                )
                # Update menu
                self.app.icon.menu = self.build()
            except Exception as e:
                self.app.notifications.show(
                    "Delete Failed",
                    f"Could not delete capture: {e}",
                )

    def _open_captures_list(self) -> None:
        """Open captures list in dashboard."""
        webbrowser.open(f"http://localhost:{self.app.config.dashboard_port}/captures")

    def _open_training_dashboard(self) -> None:
        """Open training dashboard."""
        webbrowser.open(f"http://localhost:{self.app.config.dashboard_port}/training")

    def _start_training(self) -> None:
        """Open training configuration dialog."""
        self.app.platform.open_training_dialog()

    def _stop_training(self) -> None:
        """Stop current training."""
        try:
            subprocess.run(
                ["openadapt", "train", "stop"],
                capture_output=True,
                timeout=10,
            )
            self.app.state.transition(TrayState.IDLE)
        except Exception as e:
            print(f"Error stopping training: {e}")

    def _view_training_results(self) -> None:
        """View last training results."""
        try:
            result = subprocess.run(
                ["openadapt", "train", "status"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout:
                self.app.notifications.show(
                    "Training Status",
                    result.stdout.strip()[:200],  # Limit notification length
                )
            else:
                self.app.notifications.show(
                    "Training Status",
                    "No training results available.",
                )
        except FileNotFoundError:
            self.app.notifications.show(
                "Training Status",
                "openadapt CLI not found.",
            )
        except Exception as e:
            print(f"Error getting training status: {e}")

    def _open_in_file_browser(self, path: str) -> None:
        """Open a path in the system file browser.

        Args:
            path: Path to open.
        """
        import sys

        if sys.platform == "darwin":
            subprocess.run(["open", path])
        elif sys.platform == "win32":
            subprocess.run(["explorer", path])
        else:
            subprocess.run(["xdg-open", path])

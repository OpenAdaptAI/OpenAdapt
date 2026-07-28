"""Menu construction and actions for OpenAdapt Tray."""

import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from typing import TYPE_CHECKING

from pystray import Menu
from pystray import MenuItem as Item

if TYPE_CHECKING:
    from openadapt_tray.app import TrayApplication

from openadapt_tray.platform.base import DialogUnavailableError
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
        ]

        # Break/needs-attention entry (only when there are open breaks).
        break_item = self._build_break_item(state)
        if break_item is not None:
            items.append(break_item)

        items += [
            Menu.SEPARATOR,
            Item("Open Desktop App", self._open_desktop_app),
            Item("Open Cloud Dashboard", self._open_cloud_dashboard),
            self._build_sync_item(state),
            Item("Login...", self._login),
            Item("Settings...", self._open_settings),
            Menu.SEPARATOR,
            Item("Quit", self._quit),
        ]

        return Menu(*items)

    def _build_break_item(self, state) -> Item | None:
        """Build the needs-attention entry when breaks are present.

        Args:
            state: Current application state.

        Returns:
            A menu item, or ``None`` when there are no open breaks.
        """
        if not state.has_breaks():
            return None
        count = state.break_count
        noun = "automation" if count == 1 else "automations"
        return Item(
            f"⚠ {count} {noun} need attention",
            lambda: self.app.open_needs_attention(),
        )

    def _build_sync_item(self, state) -> Item:
        """Build the pause/resume-sync toggle.

        Args:
            state: Current application state.

        Returns:
            Menu item toggling the upload/sync queue.
        """
        if state.is_offline():
            return Item("Sync (offline)", None, enabled=False)
        return Item("Pause Sync", lambda: self.app.pause_sync())

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
        elif state.state == TrayState.COMPILING:
            return Item("Compiling...", None, enabled=False)
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
        try:
            captures = self._get_recent_captures()
        except OSError as e:
            # "We could not look" must not render as "there is nothing there".
            # An unreadable captures directory now says so in the menu.
            print(f"Could not read the captures directory: {e}")
            return Item(
                "Recent Captures",
                Menu(Item("Could not read captures", None, enabled=False)),
            )

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

    def _get_recent_captures(self) -> list[CaptureInfo]:
        """Get list of recent captures.

        An empty list means the directory was read and held no captures. It
        never means the directory could not be read -- that raises, so the
        caller can render the two outcomes differently.

        Returns:
            List of CaptureInfo objects, newest first (at most 10).

        Raises:
            OSError: The captures directory exists but could not be listed or
                stat'ed (permissions, a dead mount, a broken symlink).
        """
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
                # Displayed to the user, so render in LOCAL time --
                # parse the epoch as UTC and convert, rather than
                # relying on an implicit naive-local conversion.
                mtime = datetime.fromtimestamp(
                    d.stat().st_mtime, tz=timezone.utc
                ).astimezone()
                captures.append(
                    CaptureInfo(
                        name=d.name,
                        path=str(d),
                        timestamp=mtime.strftime("%Y-%m-%d %H:%M"),
                    )
                )

        return captures[:10]  # Limit to 10 most recent

    def _open_desktop_app(self) -> None:
        """Open (or focus) the local desktop app cockpit."""
        self.app.open_desktop_app()

    def _open_cloud_dashboard(self) -> None:
        """Open the hosted cloud dashboard in the browser."""
        self.app.open_cloud_dashboard()

    def _login(self) -> None:
        """Start the hosted login flow."""
        self.app.login()

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
            # Try using openadapt CLI first.
            result = subprocess.run(
                ["openadapt", "visualize", path],
                check=False,  # returncode is inspected directly below
                capture_output=True,
            )
        except FileNotFoundError:
            # No CLI at all — fall back to the file browser.
            self._open_in_file_browser(path)
            return

        # A CLI that exists but failed used to be indistinguishable from one
        # that worked: the exit status was never read, so "View" did nothing at
        # all and said nothing about it. Fall back the same way a missing CLI
        # does.
        if result.returncode != 0:
            print(
                f"openadapt visualize failed (exit {result.returncode}): "
                f"{result.stderr}"
            )
            self._open_in_file_browser(path)

    def _delete_capture(self, path: str, name: str) -> None:
        """Delete a capture after confirmation.

        Args:
            path: Path to capture directory.
            name: Capture name for display.
        """
        try:
            confirmed = self.app.platform.confirm_dialog(
                "Delete Capture",
                f"Are you sure you want to delete this capture?\n\n{name}",
            )
        except DialogUnavailableError as e:
            # Not asking is not the same as being told no. Deleting nothing is
            # still the right action here, but the user gets told why their
            # click did nothing instead of watching it vanish.
            print(f"Could not show the delete confirmation: {e}")
            self.app.notifications.show(
                "Could not confirm deletion",
                f"No confirmation dialog could be shown, so '{name}' was NOT "
                "deleted.",
            )
            return

        if confirmed:
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
        """Open the local workflow library in the desktop app."""
        self.app.open_desktop_app()

    def _open_in_file_browser(self, path: str) -> None:
        """Open a path in the system file browser.

        Args:
            path: Path to open.
        """
        import sys

        # check=False: a file browser that is missing or refuses to open is
        # not worth raising over -- this is a best-effort convenience action.
        if sys.platform == "darwin":
            subprocess.run(["open", path], check=False)
        elif sys.platform == "win32":
            subprocess.run(["explorer", path], check=False)
        else:
            subprocess.run(["xdg-open", path], check=False)

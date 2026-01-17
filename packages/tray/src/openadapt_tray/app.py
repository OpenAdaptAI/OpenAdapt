"""Main system tray application for OpenAdapt."""

import sys
import threading
import subprocess
import webbrowser
from typing import Optional

import pystray
from PIL import Image

from openadapt_tray.state import StateManager, TrayState, AppState
from openadapt_tray.menu import MenuBuilder
from openadapt_tray.icons import IconManager
from openadapt_tray.shortcuts import HotkeyManager
from openadapt_tray.notifications import NotificationManager
from openadapt_tray.ipc import IPCClient, IPCMessageType
from openadapt_tray.config import TrayConfig
from openadapt_tray.platform import get_platform_handler


class TrayApplication:
    """Main system tray application."""

    def __init__(self, config: Optional[TrayConfig] = None):
        """Initialize the tray application.

        Args:
            config: Optional configuration. If None, loads from file or defaults.
        """
        self.config = config or TrayConfig.load()
        self.state = StateManager()
        self.platform = get_platform_handler()

        # Initialize components
        self.icons = IconManager()
        self.notifications = NotificationManager()
        self.menu_builder = MenuBuilder(self)
        self.hotkeys = HotkeyManager(self.config.hotkeys)
        self.ipc = IPCClient()

        # Process handle for capture subprocess
        self._capture_process: Optional[subprocess.Popen] = None

        # Create tray icon
        self.icon = pystray.Icon(
            name="openadapt",
            icon=self.icons.get(TrayState.IDLE),
            title="OpenAdapt",
            menu=self.menu_builder.build(),
        )

        # Set tray icon reference for Windows notifications
        self.notifications.set_tray_icon(self.icon)

        # Register state change handler
        self.state.add_listener(self._on_state_change)

        # Register IPC handlers
        self._setup_ipc_handlers()

        # Register hotkey handlers
        self._setup_hotkeys()

    def _setup_hotkeys(self) -> None:
        """Configure global hotkeys."""
        self.hotkeys.register(
            self.config.hotkeys.toggle_recording,
            self._toggle_recording,
        )
        self.hotkeys.register(
            self.config.hotkeys.open_dashboard,
            self._open_dashboard,
        )

        # Register triple-ctrl for stop recording (legacy compatibility)
        if self.config.stop_on_triple_ctrl:
            self.hotkeys.register(
                "<ctrl>+<ctrl>+<ctrl>",
                self._stop_recording_if_active,
            )

    def _setup_ipc_handlers(self) -> None:
        """Configure IPC message handlers."""
        self.ipc.register_handler(
            IPCMessageType.RECORDING_STARTED,
            self._on_ipc_recording_started,
        )
        self.ipc.register_handler(
            IPCMessageType.RECORDING_STOPPED,
            self._on_ipc_recording_stopped,
        )
        self.ipc.register_handler(
            IPCMessageType.RECORDING_ERROR,
            self._on_ipc_recording_error,
        )
        self.ipc.register_handler(
            IPCMessageType.STATUS_UPDATE,
            self._on_ipc_status_update,
        )
        self.ipc.register_handler(
            IPCMessageType.TRAINING_PROGRESS,
            self._on_ipc_training_progress,
        )

    def _on_state_change(self, state: AppState) -> None:
        """Handle state changes.

        Args:
            state: New application state.
        """
        # Update icon
        self.icon.icon = self.icons.get(state.state)

        # Update menu
        self.icon.menu = self.menu_builder.build()

        # Show notification if appropriate
        if self.config.show_notifications:
            self._show_state_notification(state)

    def _show_state_notification(self, state: AppState) -> None:
        """Show notification for state transitions.

        Args:
            state: Current application state.
        """
        messages = {
            TrayState.RECORDING: (
                "Recording Started",
                f"Capturing: {state.current_capture or 'session'}",
            ),
            TrayState.IDLE: ("Recording Stopped", "Capture saved"),
            TrayState.TRAINING: ("Training Started", "Model training in progress"),
            TrayState.ERROR: ("Error", state.error_message or "An error occurred"),
        }

        if state.state in messages:
            title, body = messages[state.state]
            self.notifications.show(
                title,
                body,
                duration_ms=self.config.notification_duration_ms,
            )

    def _toggle_recording(self) -> None:
        """Toggle recording state."""
        if self.state.current.can_start_recording():
            self.start_recording()
        elif self.state.current.can_stop_recording():
            self.stop_recording()

    def _stop_recording_if_active(self) -> None:
        """Stop recording if currently active (for triple-ctrl)."""
        if self.state.current.can_stop_recording():
            self.stop_recording()

    def start_recording(self, name: Optional[str] = None) -> None:
        """Start a new capture session.

        Args:
            name: Optional name for the capture. If None, prompts user.
        """
        if not self.state.current.can_start_recording():
            return

        # Prompt for name if not provided
        if name is None and self.config.use_native_dialogs:
            name = self.platform.prompt_input(
                "New Recording",
                "Enter a name for this capture:",
            )
            if not name:
                return  # User cancelled

        # Use default name if still not set
        if not name:
            from datetime import datetime

            name = datetime.now().strftime("capture_%Y%m%d_%H%M%S")

        self.state.transition(TrayState.RECORDING_STARTING, current_capture=name)

        # Start capture in background thread
        threading.Thread(
            target=self._run_capture,
            args=(name,),
            daemon=True,
        ).start()

    def _run_capture(self, name: str) -> None:
        """Run capture in background thread.

        Args:
            name: Capture session name.
        """
        try:
            # Start capture via CLI subprocess
            self._capture_process = subprocess.Popen(
                ["openadapt", "record", name],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.state.transition(TrayState.RECORDING, current_capture=name)

            # Wait for process to complete (when stopped externally)
            self._capture_process.wait()

            # Check if we're still in recording state (vs explicit stop)
            if self.state.current.state == TrayState.RECORDING:
                self.state.transition(TrayState.IDLE)

        except FileNotFoundError:
            self.state.transition(
                TrayState.ERROR,
                error_message="openadapt CLI not found. Please install openadapt.",
            )
        except Exception as e:
            self.state.transition(TrayState.ERROR, error_message=str(e))

    def stop_recording(self) -> None:
        """Stop the current capture session."""
        if not self.state.current.can_stop_recording():
            return

        self.state.transition(TrayState.RECORDING_STOPPING)

        # Terminate capture process
        if self._capture_process and self._capture_process.poll() is None:
            try:
                # Send SIGTERM for graceful shutdown
                self._capture_process.terminate()

                # Wait briefly for process to cleanup
                try:
                    self._capture_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    # Force kill if not responding
                    self._capture_process.kill()
            except Exception as e:
                print(f"Error stopping capture process: {e}")

        self._capture_process = None
        self.state.transition(TrayState.IDLE)

    def _open_dashboard(self) -> None:
        """Open the web dashboard."""
        webbrowser.open(f"http://localhost:{self.config.dashboard_port}")

    # IPC event handlers

    def _on_ipc_recording_started(self, message) -> None:
        """Handle recording started IPC event."""
        data = message.data or {}
        self.state.transition(
            TrayState.RECORDING,
            current_capture=data.get("name"),
        )

    def _on_ipc_recording_stopped(self, message) -> None:
        """Handle recording stopped IPC event."""
        self.state.transition(TrayState.IDLE)

    def _on_ipc_recording_error(self, message) -> None:
        """Handle recording error IPC event."""
        data = message.data or {}
        self.state.transition(
            TrayState.ERROR,
            error_message=data.get("error", "Recording error"),
        )

    def _on_ipc_status_update(self, message) -> None:
        """Handle status update IPC event."""
        # Generic status updates - could update UI or log
        pass

    def _on_ipc_training_progress(self, message) -> None:
        """Handle training progress IPC event."""
        data = message.data or {}
        progress = data.get("progress", 0)
        self.state.transition(
            TrayState.TRAINING,
            training_progress=progress,
        )

    def run(self) -> None:
        """Run the application."""
        # Start hotkey listener
        self.hotkeys.start()

        # Platform-specific setup
        self.platform.setup()

        # Try to connect to IPC server (optional - won't block if unavailable)
        try:
            connected = self.ipc.connect()
            if connected:
                print("IPC connected successfully")
            else:
                print("IPC server not available - running in standalone mode")
        except Exception as e:
            print(f"IPC connection failed: {e} - running in standalone mode")

        # Run the tray icon (blocks)
        self.icon.run()

    def quit(self) -> None:
        """Quit the application."""
        # Stop any active recording
        if self.state.current.is_recording():
            self.stop_recording()

        # Cleanup components
        self.hotkeys.stop()
        self.ipc.close()
        self.notifications.cleanup()
        self.platform.cleanup()

        # Stop tray icon
        self.icon.stop()


def main() -> None:
    """Entry point for the tray application."""
    app = TrayApplication()
    try:
        app.run()
    except KeyboardInterrupt:
        app.quit()
    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

"""Main system tray application for OpenAdapt.

The tray is a lightweight status mirror + launcher. It owns no business logic:

* recording/compile actions are commands sent to the **desktop app** over a
  loopback socket (discovered via ``~/.openadapt/desktop_ipc.json``);
* the **break badge** is polled from the hosted control plane's
  needs-attention count endpoint;
* the desktop app is the source of truth for all state — the tray renders it.
"""

import sys
import threading
import webbrowser

import pystray

from openadapt_tray.config import ConfigLoadError, TrayConfig
from openadapt_tray.desktop import DesktopLaunchError, launch_native_desktop
from openadapt_tray.hosted import (
    CountResult,
    HostedPoller,
    InvalidCountPayload,
    route_break_click,
)
from openadapt_tray.icons import IconManager
from openadapt_tray.ipc import IPCClient, IPCMessageType
from openadapt_tray.menu import MenuBuilder
from openadapt_tray.notifications import NotificationManager
from openadapt_tray.platform import get_platform_handler
from openadapt_tray.platform.base import DialogUnavailableError
from openadapt_tray.shortcuts import HotkeyManager
from openadapt_tray.state import (
    LANE_BYOC,
    LANE_CLOUD,
    AppState,
    CredentialStatus,
    StateManager,
    SyncState,
    TrayState,
)


class TrayApplication:
    """Main system tray application."""

    def __init__(self, config: TrayConfig | None = None):
        """Initialize the tray application.

        Args:
            config: Optional configuration. If None, loads from file or defaults.
        """
        # An unreadable tray.json must not quietly become "default settings" --
        # the default lane is "cloud", and a byoc install silently moved onto
        # the hosted route is exactly the kind of wrong-but-confident state the
        # tray exists to expose. Keep the error and tell the user in run().
        self._config_error: ConfigLoadError | None = None
        if config is not None:
            self.config = config
        else:
            self.config, self._config_error = TrayConfig.load_or_defaults()
        self.state = StateManager()
        self.platform = get_platform_handler()

        # Seed the deployment lane from config so break-click routing is correct
        # before the desktop first reports it.
        self.state.set_deployment_lane(self.config.deployment_lane)

        # Initialize components
        self.icons = IconManager()
        self.notifications = NotificationManager()
        self.menu_builder = MenuBuilder(self)
        self.hotkeys = HotkeyManager(self.config.hotkeys)

        # IPC client — configured from the desktop discovery file when present.
        self.ipc = IPCClient.from_discovery() or IPCClient(
            port=self.config.desktop_ipc_port or IPCClient.DEFAULT_PORT,
        )

        # Cloud needs-attention poller (started in run()).
        self.hosted = HostedPoller(
            config=self.config,
            on_count=self._on_hosted_count,
            notifier=self.notifications,
            on_break_clicked=self.open_needs_attention,
            on_credential=self._on_hosted_credential,
            on_credential_clicked=self.login,
            set_offline=self._on_hosted_offline,
        )

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
        self._last_notified_tray_state = self.state.current.state
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
            self.open_desktop_app,
        )

        # Register triple-ctrl for stop recording (legacy compatibility)
        if self.config.stop_on_triple_ctrl:
            self.hotkeys.register(
                "<ctrl>+<ctrl>+<ctrl>",
                self._stop_recording_if_active,
            )

    def _setup_ipc_handlers(self) -> None:
        """Configure IPC message handlers (desktop → tray events)."""
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
            IPCMessageType.COMPILE_PROGRESS,
            self._on_ipc_compile_progress,
        )
        self.ipc.register_handler(
            IPCMessageType.SYNC_STATE,
            self._on_ipc_sync_state,
        )
        self.ipc.register_handler(
            IPCMessageType.BREAK_COUNT,
            self._on_ipc_break_count,
        )

    def _on_state_change(self, state: AppState) -> None:
        """Handle state changes.

        Args:
            state: New application state.
        """
        # Update icon (with break badge when automations need attention).
        self.icon.icon = self.icons.get(state.state, break_count=state.break_count)

        # Update menu
        self.icon.menu = self.menu_builder.build()

        # Show notification if appropriate
        if self.config.show_notifications:
            self._show_state_notification(state)

    def _show_state_notification(self, state: AppState) -> None:
        """Show notification for recording-lifecycle transitions.

        Args:
            state: Current application state.
        """
        if state.state == self._last_notified_tray_state:
            return

        messages = {
            TrayState.RECORDING: (
                "Recording Started",
                f"Capturing: {state.current_capture or 'session'}",
            ),
            TrayState.COMPILING: (
                "Compiling",
                f"Building a workflow from: {state.current_capture or 'recording'}",
            ),
            TrayState.IDLE: ("Recording Stopped", "Capture saved"),
            TrayState.ERROR: ("Error", state.error_message or "An error occurred"),
        }

        if state.state not in messages:
            self._last_notified_tray_state = state.state
            return

        title, body = messages[state.state]
        delivered = self.notifications.show(
            title,
            body,
            duration_ms=self.config.notification_duration_ms,
        )
        if delivered:
            self._last_notified_tray_state = state.state

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

    # --- desktop connection -------------------------------------------------

    def ensure_desktop_connection(self) -> bool:
        """Ensure the tray is connected to the desktop loopback socket.

        If the desktop app is not running (no discovery file / unreachable),
        launch it, wait briefly for the discovery file, then connect.

        Returns:
            True if connected.
        """
        if self.ipc.is_connected():
            return True

        # Try discovery + connect first (desktop may already be up).
        if self.ipc.refresh_from_discovery() and self.ipc.connect():
            return True

        # Desktop not running — launch it, then poll for the discovery file.
        if not self._launch_desktop_app():
            return False
        for _ in range(20):  # ~10s
            if self.ipc.refresh_from_discovery() and self.ipc.connect():
                return True
            threading.Event().wait(0.5)

        return False

    def _launch_desktop_app(self) -> bool:
        """Launch the installed native desktop application."""
        try:
            launch_native_desktop()
        except DesktopLaunchError as e:
            self.notifications.show(
                "Desktop app not found",
                "Install the OpenAdapt desktop app to record and manage workflows.",
            )
            print(f"Failed to launch desktop app: {e}")
            return False
        except Exception as e:
            print(f"Failed to launch desktop app: {e}")
            return False
        return True

    # --- recording actions (delegated to the desktop over IPC) --------------

    def start_recording(self, name: str | None = None) -> None:
        """Start a new capture session via the desktop app.

        Args:
            name: Optional name for the capture. If None, prompts the user.
        """
        if not self.state.current.can_start_recording():
            return

        # Prompt for name if not provided
        if name is None and self.config.use_native_dialogs:
            try:
                name = self.platform.prompt_input(
                    "New Recording",
                    "Enter a name for this capture:",
                )
            except DialogUnavailableError as e:
                # The user asked to record and the prompt never appeared. This
                # used to look identical to "the user cancelled", so the click
                # did nothing and said nothing. Honour the click with a default
                # name and say why there was no prompt.
                print(f"Could not show the capture-name dialog: {e}")
                self.notifications.show(
                    "No naming dialog available",
                    "Recording started with a default name.",
                )
            else:
                if not name:
                    return  # User cancelled

        # Use default name if still not set
        if not name:
            from datetime import datetime, timezone

            # The default name is shown to the user next to their other
            # captures, so it stays in LOCAL time -- made explicit rather than
            # leaning on a naive `datetime.now()`.
            local_now = datetime.now(tz=timezone.utc).astimezone()
            name = local_now.strftime("capture_%Y%m%d_%H%M%S")

        self.state.transition(TrayState.RECORDING_STARTING, current_capture=name)

        # Connect + dispatch on a background thread (launching the desktop app
        # may take a few seconds).
        threading.Thread(
            target=self._dispatch_start_recording,
            args=(name,),
            daemon=True,
        ).start()

    def _dispatch_start_recording(self, name: str) -> None:
        """Ensure the desktop is up and send the start-recording command."""
        if not self.ensure_desktop_connection():
            self.state.transition(
                TrayState.ERROR,
                error_message="Could not reach the desktop app to start recording.",
            )
            return
        if not self.ipc.send_start_recording(name):
            self.state.transition(
                TrayState.ERROR,
                error_message="Failed to send start-recording command.",
            )
        # The desktop confirms via a RECORDING_STARTED event.

    def stop_recording(self) -> None:
        """Stop the current capture session via the desktop app."""
        if not self.state.current.can_stop_recording():
            return

        self.state.transition(TrayState.RECORDING_STOPPING)
        if not self.ipc.send_stop_recording():
            self.state.transition(
                TrayState.ERROR,
                error_message="Failed to send stop-recording command.",
            )
        # The desktop confirms via RECORDING_STOPPED (then COMPILE_PROGRESS).

    # --- quick actions ------------------------------------------------------

    def open_desktop_app(self) -> None:
        """Open (or focus) the desktop app's workflow library."""
        if self.ipc.is_connected():
            if not self.ipc.send_open_workflow_library():
                self._report_desktop_open_failure()
            return
        # Not connected — launch/connect, then ask it to open the library.
        threading.Thread(
            target=self._open_desktop_app_async,
            daemon=True,
        ).start()

    def _open_desktop_app_async(self) -> None:
        """Background helper for :meth:`open_desktop_app`."""
        if self.ensure_desktop_connection():
            if not self.ipc.send_open_workflow_library():
                self._report_desktop_open_failure()
        else:
            self._report_desktop_open_failure()

    def _report_desktop_open_failure(self) -> None:
        """Report that the workflow-library command did not reach the desktop."""
        self.notifications.show(
            "Could not open the desktop app",
            "The tray could not send the workflow-library command.",
        )

    def open_cloud_dashboard(self) -> bool:
        """Open the hosted cloud dashboard in the system browser."""
        return self._open_browser(
            self.config.hosted_url,
            "Could not open the cloud dashboard",
        )

    def open_needs_attention(self) -> bool:
        """Route a needs-attention click by deployment lane (§3c).

        Returns:
            True if something opened. When nothing opened the user is told,
            rather than being left in front of an unchanged screen.
        """
        routed = route_break_click(
            self.config,
            ipc_client=self.ipc if self.ipc.is_connected() else None,
        )
        if not routed:
            self.notifications.show(
                "Could not open needs-attention",
                "Neither the desktop app nor a browser could be opened. "
                f"Open {self.config.hosted_url.rstrip('/')}/dashboard manually.",
            )
        return routed

    def login(self) -> bool:
        """Start the hosted login flow.

        The tray does not implement auth; it opens the ingest-token settings
        page (the desktop app owns the interactive providers) so the user can
        mint/paste a token that lands in the shared keychain.
        """
        return self._open_browser(
            f"{self.config.hosted_url.rstrip('/')}/dashboard/settings/ingest",
            "Could not open login settings",
        )

    def _open_browser(self, url: str, failure_title: str) -> bool:
        """Open a URL and keep a missing browser distinct from a successful open."""
        try:
            if webbrowser.open(url):
                return True
        except Exception as e:
            print(f"Failed to open {url}: {e}")
        self.notifications.show(
            failure_title,
            f"No usable browser opened. Open {url} manually.",
        )
        return False

    def pause_sync(self) -> bool:
        """Ask the desktop to pause the upload/sync queue."""
        if not self.ipc.is_connected() or not self.ipc.send_pause_sync():
            self.notifications.show(
                "Could not pause sync",
                "The desktop app did not accept the pause command.",
            )
            return False
        self.state.set_sync_state(SyncState.SYNCED)
        return True

    def resume_sync(self) -> bool:
        """Ask the desktop to resume the upload/sync queue."""
        if not self.ipc.is_connected() or not self.ipc.send_resume_sync():
            self.notifications.show(
                "Could not resume sync",
                "The desktop app did not accept the resume command.",
            )
            return False
        return True

    def _report_config_error(self) -> None:
        """Tell the user when the tray is running on defaults it did not choose."""
        if self._config_error is None:
            return
        self.notifications.show(
            "Settings could not be read",
            f"{self._config_error} — running on default settings "
            f"(lane: {self.config.deployment_lane}).",
            urgency="critical",
        )

    # --- hosted poller callbacks --------------------------------------------

    def _on_hosted_count(self, result: CountResult) -> None:
        """Apply a needs-attention count from the cloud poller."""
        self.state.set_break_count(result.count)

    def _on_hosted_credential(self, credential: CredentialStatus) -> None:
        """Apply the privacy-safe credential status from the cloud poller."""
        self.state.set_credential_status(credential)

    def _on_hosted_offline(self, offline: bool) -> None:
        """Reflect the cloud poller's online/offline status on the sync channel."""
        if offline:
            self.state.set_sync_state(SyncState.OFFLINE)
        elif self.state.current.is_offline():
            # Recovered connectivity — clear the offline marker.
            self.state.set_sync_state(SyncState.SYNCED)

    # --- IPC event handlers (desktop → tray) --------------------------------

    def _on_ipc_recording_started(self, message) -> None:
        """Handle recording started IPC event."""
        data = message.data or {}
        self.state.transition(
            TrayState.RECORDING,
            current_capture=data.get("capture_id") or data.get("name"),
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
        """Handle a full status update from the desktop (source of truth).

        Protocol v1 carries the canonical ``recording`` boolean and optional
        ``capture_id``. Hosted status fields remain orthogonal.
        """
        data = message.data or {}

        lane = data.get("deployment_lane")
        if lane in (LANE_CLOUD, LANE_BYOC):
            self.state.set_deployment_lane(lane)

        sync = data.get("sync_state")
        if sync is not None:
            self._apply_sync_state(sync)

        if "break_count" in data:
            self._apply_break_count(data.get("break_count"))

        recording = data.get("recording")
        if type(recording) is bool:
            self.state.transition(
                TrayState.RECORDING if recording else TrayState.IDLE,
                current_capture=(data.get("capture_id") if recording else None),
            )
            return

        # Compatibility with the pre-v1 status projection. Protocol v1 uses
        # the canonical ``recording`` boolean above.
        state_name = data.get("state")
        if isinstance(state_name, str):
            try:
                self.state.transition(TrayState[state_name.upper()])
            except KeyError:
                pass

    def _on_ipc_compile_progress(self, message) -> None:
        """Handle a compile-progress event (recording → workflow)."""
        data = message.data or {}
        state = str(data.get("state") or "").lower()
        capture_id = data.get("capture_id") or data.get("name")
        if state == "compiled":
            self.state.transition(TrayState.IDLE)
        elif state in {"failed", "review_failed"}:
            default_error = (
                "The action review could not open."
                if state == "review_failed"
                else "The recording could not be compiled."
            )
            self.state.transition(
                TrayState.ERROR,
                current_capture=capture_id,
                error_message=str(data.get("error") or default_error),
            )
        elif state == "compiling":
            self.state.transition(
                TrayState.COMPILING,
                current_capture=capture_id,
            )
        elif data.get("done") is True:
            # Compatibility with an older terminal projection.
            self.state.transition(TrayState.IDLE)

    def _on_ipc_sync_state(self, message) -> None:
        """Handle a sync-state event from the desktop."""
        data = message.data or {}
        self._apply_sync_state(data.get("state"))

    def _on_ipc_break_count(self, message) -> None:
        """Handle a break-count event pushed from the desktop."""
        data = message.data or {}
        self._apply_break_count(data.get("count"))

    def _apply_break_count(self, value: object) -> None:
        """Apply a validated count without turning an unreadable value into zero."""
        try:
            result = CountResult.from_payload({"count": value})
        except InvalidCountPayload as e:
            print(f"Ignored unusable IPC break count: {e}")
            return
        self.state.set_break_count(result.count)

    def _apply_sync_state(self, name: str | None) -> None:
        """Map a sync-state name from IPC onto the SyncState channel."""
        if not name:
            return
        mapping = {
            "synced": SyncState.SYNCED,
            "syncing": SyncState.SYNCING,
            "pushing": SyncState.SYNCING,
            "offline": SyncState.OFFLINE,
        }
        sync = mapping.get(str(name).lower())
        if sync is not None:
            self.state.set_sync_state(sync)

    def run(self) -> None:
        """Run the application."""
        # Say it out loud before anything relies on the settings.
        self._report_config_error()

        # Start hotkey listener
        self.hotkeys.start()

        # Platform-specific setup
        self.platform.setup()

        # Try to connect to the desktop IPC server (optional — non-blocking).
        try:
            if self.ipc.refresh_from_discovery() and self.ipc.connect():
                print("Connected to desktop app IPC")
            else:
                print("Desktop app not running — will launch on first action")
        except Exception as e:
            print(f"IPC connection failed: {e} - running in standalone mode")

        # Start the cloud needs-attention poller.
        self.hosted.start()

        # Run the tray icon (blocks)
        self.icon.run()

    def quit(self) -> None:
        """Quit the application."""
        # Stop any active recording
        if self.state.current.is_recording():
            self.stop_recording()

        # Cleanup components
        self.hosted.stop()
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

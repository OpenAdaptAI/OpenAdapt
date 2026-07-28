"""Application state management for OpenAdapt Tray.

The tray mirrors two ORTHOGONAL status channels sourced from the local desktop
app and the cloud control plane:

* ``TrayState`` — the recording/compile lifecycle (idle → recording → compiling).
* ``SyncState`` — the upload/sync channel (synced ↔ syncing ↔ offline).

They are independent: a machine can be ``COMPILING`` a freshly recorded
workflow while its previous push is still ``SYNCING``, or sit ``IDLE`` while
``OFFLINE``. Modelling them as one enum would force false exclusivity, so
``AppState`` carries them as two separate fields plus a ``break_count`` badge
sourced from the cloud needs-attention count.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto


class TrayState(Enum):
    """Recording / compile lifecycle states (the primary icon channel)."""

    IDLE = auto()
    RECORDING_STARTING = auto()
    RECORDING = auto()
    RECORDING_STOPPING = auto()
    COMPILING = auto()
    ERROR = auto()


class SyncState(Enum):
    """Upload / sync channel states (orthogonal to :class:`TrayState`)."""

    SYNCED = auto()
    SYNCING = auto()  # a.k.a. PUSHING — an upload is in flight
    OFFLINE = auto()

    # Backwards/spec-friendly alias: the spec names this channel "SYNCING/PUSHING".
    @classmethod
    def PUSHING(cls) -> "SyncState":
        """Alias for :attr:`SYNCING` (the spec uses SYNCING/PUSHING interchangeably)."""
        return cls.SYNCING


# Deployment lanes (drives break-click routing; learned from desktop/config).
LANE_CLOUD = "cloud"
LANE_BYOC = "byoc"


@dataclass
class AppState:
    """Current application state (two orthogonal channels + a break badge)."""

    # Recording / compile lifecycle.
    state: TrayState = TrayState.IDLE
    current_capture: str | None = None
    error_message: str | None = None

    # Sync channel (independent of the recording lifecycle).
    sync_state: SyncState = SyncState.SYNCED

    # Break / needs-attention badge (sourced from the cloud count endpoint).
    break_count: int = 0

    # Deployment lane — drives lane-aware break-click routing.
    deployment_lane: str = LANE_CLOUD

    def can_start_recording(self) -> bool:
        """Check if recording can be started."""
        return self.state == TrayState.IDLE

    def can_stop_recording(self) -> bool:
        """Check if recording can be stopped."""
        return self.state == TrayState.RECORDING

    def is_recording(self) -> bool:
        """Check if currently recording."""
        return self.state in (
            TrayState.RECORDING_STARTING,
            TrayState.RECORDING,
            TrayState.RECORDING_STOPPING,
        )

    def is_compiling(self) -> bool:
        """Check if a recording is being compiled into a workflow."""
        return self.state == TrayState.COMPILING

    def is_busy(self) -> bool:
        """Check if the application is busy with any recording operation."""
        return self.state not in (TrayState.IDLE, TrayState.ERROR)

    def is_syncing(self) -> bool:
        """Check if an upload is currently in flight."""
        return self.sync_state == SyncState.SYNCING

    def is_offline(self) -> bool:
        """Check if the sync channel is offline."""
        return self.sync_state == SyncState.OFFLINE

    def has_breaks(self) -> bool:
        """Check if any automations currently need attention."""
        return self.break_count > 0

    def is_byoc(self) -> bool:
        """Check if the active deployment lane keeps PHI on-machine (byoc)."""
        return self.deployment_lane == LANE_BYOC


class StateManager:
    """Manages application state transitions across both channels.

    Recording-lifecycle updates (:meth:`transition`) preserve the orthogonal
    sync channel, break count and deployment lane unless explicitly overridden,
    and vice-versa — so updating one channel never clobbers the other.
    """

    def __init__(self):
        self._state = AppState()
        self._listeners: list[Callable[[AppState], None]] = []

    def add_listener(self, callback: Callable[[AppState], None]) -> None:
        """Add a state change listener."""
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[AppState], None]) -> None:
        """Remove a state change listener."""
        if callback in self._listeners:
            self._listeners.remove(callback)

    def _emit(self) -> None:
        """Notify all listeners of the current state."""
        for listener in self._listeners:
            try:
                listener(self._state)
            except Exception as e:
                # Don't let a bad listener crash the app.
                print(f"Error in state listener: {e}")

    def transition(self, new_state: TrayState, **kwargs) -> None:
        """Transition the recording lifecycle and notify listeners.

        The orthogonal channels (``sync_state``, ``break_count``,
        ``deployment_lane``) are preserved from the current state unless passed
        explicitly in ``kwargs``.

        Args:
            new_state: The new recording-lifecycle state.
            **kwargs: Optional overrides (``current_capture``, ``error_message``,
                ``sync_state``, ``break_count``, ``deployment_lane``).
        """
        # Preserve current_capture across sub-states of an active recording.
        if "current_capture" not in kwargs and new_state in (
            TrayState.RECORDING,
            TrayState.RECORDING_STOPPING,
            TrayState.COMPILING,
        ):
            kwargs.setdefault("current_capture", self._state.current_capture)

        # Preserve the orthogonal channels unless explicitly overridden.
        kwargs.setdefault("sync_state", self._state.sync_state)
        kwargs.setdefault("break_count", self._state.break_count)
        kwargs.setdefault("deployment_lane", self._state.deployment_lane)

        self._state = AppState(state=new_state, **kwargs)
        self._emit()

    def set_sync_state(self, sync_state: SyncState) -> None:
        """Update ONLY the sync channel, preserving the recording lifecycle."""
        if self._state.sync_state == sync_state:
            return
        from dataclasses import replace

        self._state = replace(self._state, sync_state=sync_state)
        self._emit()

    def set_break_count(self, count: int) -> None:
        """Update ONLY the break/needs-attention badge count."""
        count = max(0, int(count))
        if self._state.break_count == count:
            return
        from dataclasses import replace

        self._state = replace(self._state, break_count=count)
        self._emit()

    def set_deployment_lane(self, lane: str) -> None:
        """Update ONLY the deployment lane (cloud|byoc)."""
        if self._state.deployment_lane == lane:
            return
        from dataclasses import replace

        self._state = replace(self._state, deployment_lane=lane)
        self._emit()

    @property
    def current(self) -> AppState:
        """Get the current application state."""
        return self._state

    def reset(self) -> None:
        """Reset the recording lifecycle to IDLE (sync channel preserved)."""
        self.transition(TrayState.IDLE)

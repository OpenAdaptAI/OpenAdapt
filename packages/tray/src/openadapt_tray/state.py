"""Application state management for OpenAdapt Tray."""

from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Optional, Callable, List


class TrayState(Enum):
    """Application states."""

    IDLE = auto()
    RECORDING_STARTING = auto()
    RECORDING = auto()
    RECORDING_STOPPING = auto()
    TRAINING = auto()
    TRAINING_PAUSED = auto()
    ERROR = auto()


@dataclass
class AppState:
    """Current application state."""

    state: TrayState = TrayState.IDLE
    current_capture: Optional[str] = None
    training_progress: Optional[float] = None
    error_message: Optional[str] = None

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

    def is_training(self) -> bool:
        """Check if currently training."""
        return self.state in (TrayState.TRAINING, TrayState.TRAINING_PAUSED)

    def is_busy(self) -> bool:
        """Check if the application is busy with any operation."""
        return self.state not in (TrayState.IDLE, TrayState.ERROR)


class StateManager:
    """Manages application state transitions."""

    def __init__(self):
        self._state = AppState()
        self._listeners: List[Callable[[AppState], None]] = []

    def add_listener(self, callback: Callable[[AppState], None]) -> None:
        """Add a state change listener."""
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[AppState], None]) -> None:
        """Remove a state change listener."""
        if callback in self._listeners:
            self._listeners.remove(callback)

    def transition(self, new_state: TrayState, **kwargs) -> None:
        """Transition to a new state and notify listeners.

        Args:
            new_state: The new state to transition to.
            **kwargs: Additional state attributes (current_capture, training_progress, error_message).
        """
        # Preserve certain fields if not explicitly set
        if "current_capture" not in kwargs and new_state in (
            TrayState.RECORDING,
            TrayState.RECORDING_STOPPING,
        ):
            kwargs.setdefault("current_capture", self._state.current_capture)

        self._state = AppState(state=new_state, **kwargs)

        for listener in self._listeners:
            try:
                listener(self._state)
            except Exception as e:
                # Don't let a bad listener crash the app
                print(f"Error in state listener: {e}")

    @property
    def current(self) -> AppState:
        """Get the current application state."""
        return self._state

    def reset(self) -> None:
        """Reset to initial state."""
        self.transition(TrayState.IDLE)

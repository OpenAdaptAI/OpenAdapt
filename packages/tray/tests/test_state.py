"""Tests for state management."""

import pytest

from openadapt_tray.state import TrayState, AppState, StateManager


class TestTrayState:
    """Tests for TrayState enum."""

    def test_all_states_defined(self):
        """Verify all expected states are defined."""
        expected_states = [
            "IDLE",
            "RECORDING_STARTING",
            "RECORDING",
            "RECORDING_STOPPING",
            "TRAINING",
            "TRAINING_PAUSED",
            "ERROR",
        ]
        actual_states = [s.name for s in TrayState]
        assert actual_states == expected_states


class TestAppState:
    """Tests for AppState dataclass."""

    def test_default_state(self):
        """Test default AppState values."""
        state = AppState()
        assert state.state == TrayState.IDLE
        assert state.current_capture is None
        assert state.training_progress is None
        assert state.error_message is None

    def test_can_start_recording_when_idle(self):
        """Test that recording can start when idle."""
        state = AppState(state=TrayState.IDLE)
        assert state.can_start_recording() is True

    def test_cannot_start_recording_when_recording(self):
        """Test that recording cannot start when already recording."""
        state = AppState(state=TrayState.RECORDING)
        assert state.can_start_recording() is False

    def test_can_stop_recording_when_recording(self):
        """Test that recording can stop when active."""
        state = AppState(state=TrayState.RECORDING)
        assert state.can_stop_recording() is True

    def test_cannot_stop_recording_when_idle(self):
        """Test that recording cannot stop when idle."""
        state = AppState(state=TrayState.IDLE)
        assert state.can_stop_recording() is False

    def test_is_recording_states(self):
        """Test is_recording for various states."""
        assert AppState(state=TrayState.RECORDING).is_recording() is True
        assert AppState(state=TrayState.RECORDING_STARTING).is_recording() is True
        assert AppState(state=TrayState.RECORDING_STOPPING).is_recording() is True
        assert AppState(state=TrayState.IDLE).is_recording() is False
        assert AppState(state=TrayState.TRAINING).is_recording() is False

    def test_is_training_states(self):
        """Test is_training for various states."""
        assert AppState(state=TrayState.TRAINING).is_training() is True
        assert AppState(state=TrayState.TRAINING_PAUSED).is_training() is True
        assert AppState(state=TrayState.IDLE).is_training() is False
        assert AppState(state=TrayState.RECORDING).is_training() is False

    def test_is_busy_states(self):
        """Test is_busy for various states."""
        assert AppState(state=TrayState.IDLE).is_busy() is False
        assert AppState(state=TrayState.ERROR).is_busy() is False
        assert AppState(state=TrayState.RECORDING).is_busy() is True
        assert AppState(state=TrayState.TRAINING).is_busy() is True


class TestStateManager:
    """Tests for StateManager class."""

    def test_initial_state_is_idle(self):
        """Test that initial state is IDLE."""
        manager = StateManager()
        assert manager.current.state == TrayState.IDLE

    def test_transition_updates_state(self):
        """Test that transition updates the state."""
        manager = StateManager()
        manager.transition(TrayState.RECORDING, current_capture="test")
        assert manager.current.state == TrayState.RECORDING
        assert manager.current.current_capture == "test"

    def test_listener_called_on_transition(self):
        """Test that listeners are called on state transition."""
        manager = StateManager()
        received_states = []

        def listener(state):
            received_states.append(state)

        manager.add_listener(listener)
        manager.transition(TrayState.RECORDING, current_capture="test")

        assert len(received_states) == 1
        assert received_states[0].state == TrayState.RECORDING

    def test_multiple_listeners(self):
        """Test that multiple listeners are all called."""
        manager = StateManager()
        call_counts = [0, 0]

        def listener1(state):
            call_counts[0] += 1

        def listener2(state):
            call_counts[1] += 1

        manager.add_listener(listener1)
        manager.add_listener(listener2)
        manager.transition(TrayState.RECORDING)

        assert call_counts == [1, 1]

    def test_remove_listener(self):
        """Test that removed listeners are not called."""
        manager = StateManager()
        call_count = [0]

        def listener(state):
            call_count[0] += 1

        manager.add_listener(listener)
        manager.transition(TrayState.RECORDING)
        assert call_count[0] == 1

        manager.remove_listener(listener)
        manager.transition(TrayState.IDLE)
        assert call_count[0] == 1  # Not incremented

    def test_reset_returns_to_idle(self):
        """Test that reset returns to IDLE state."""
        manager = StateManager()
        manager.transition(TrayState.RECORDING, current_capture="test")
        manager.reset()
        assert manager.current.state == TrayState.IDLE
        assert manager.current.current_capture is None

    def test_bad_listener_does_not_crash(self):
        """Test that a failing listener doesn't crash the manager."""
        manager = StateManager()

        def bad_listener(state):
            raise ValueError("Intentional error")

        manager.add_listener(bad_listener)

        # Should not raise
        manager.transition(TrayState.RECORDING)
        assert manager.current.state == TrayState.RECORDING

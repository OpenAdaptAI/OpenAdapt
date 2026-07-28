"""Tests for state management."""


from openadapt_tray.state import (
    LANE_BYOC,
    LANE_CLOUD,
    AppState,
    StateManager,
    SyncState,
    TrayState,
)


class TestTrayState:
    """Tests for TrayState enum."""

    def test_all_states_defined(self):
        """Verify all expected recording-lifecycle states are defined."""
        expected_states = [
            "IDLE",
            "RECORDING_STARTING",
            "RECORDING",
            "RECORDING_STOPPING",
            "COMPILING",
            "ERROR",
        ]
        actual_states = [s.name for s in TrayState]
        assert actual_states == expected_states

    def test_rl_states_removed(self):
        """The retired RL training states must not exist."""
        names = [s.name for s in TrayState]
        assert "TRAINING" not in names
        assert "TRAINING_PAUSED" not in names


class TestSyncState:
    """Tests for the orthogonal SyncState channel."""

    def test_sync_states_defined(self):
        """Verify the sync channel states."""
        names = {s.name for s in SyncState}
        assert names == {"SYNCED", "SYNCING", "OFFLINE"}

    def test_pushing_is_syncing_alias(self):
        """PUSHING is a spec alias for SYNCING."""
        assert SyncState.PUSHING() is SyncState.SYNCING


class TestAppState:
    """Tests for AppState dataclass."""

    def test_default_state(self):
        """Test default AppState values."""
        state = AppState()
        assert state.state == TrayState.IDLE
        assert state.current_capture is None
        assert state.error_message is None
        assert state.sync_state == SyncState.SYNCED
        assert state.break_count == 0
        assert state.deployment_lane == LANE_CLOUD

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
        assert AppState(state=TrayState.COMPILING).is_recording() is False

    def test_is_compiling(self):
        """Test is_compiling for various states."""
        assert AppState(state=TrayState.COMPILING).is_compiling() is True
        assert AppState(state=TrayState.RECORDING).is_compiling() is False

    def test_is_busy_states(self):
        """Test is_busy for various states."""
        assert AppState(state=TrayState.IDLE).is_busy() is False
        assert AppState(state=TrayState.ERROR).is_busy() is False
        assert AppState(state=TrayState.RECORDING).is_busy() is True
        assert AppState(state=TrayState.COMPILING).is_busy() is True

    def test_sync_and_break_helpers(self):
        """Test the orthogonal sync + break helpers."""
        assert AppState(sync_state=SyncState.SYNCING).is_syncing() is True
        assert AppState(sync_state=SyncState.OFFLINE).is_offline() is True
        assert AppState(break_count=3).has_breaks() is True
        assert AppState(break_count=0).has_breaks() is False
        assert AppState(deployment_lane=LANE_BYOC).is_byoc() is True
        assert AppState(deployment_lane=LANE_CLOUD).is_byoc() is False


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

    def test_transition_preserves_orthogonal_channels(self):
        """Recording transitions must not clobber sync/break/lane."""
        manager = StateManager()
        manager.set_sync_state(SyncState.SYNCING)
        manager.set_break_count(4)
        manager.set_deployment_lane(LANE_BYOC)

        manager.transition(TrayState.RECORDING, current_capture="w")

        assert manager.current.state == TrayState.RECORDING
        assert manager.current.sync_state == SyncState.SYNCING
        assert manager.current.break_count == 4
        assert manager.current.deployment_lane == LANE_BYOC

    def test_set_sync_state_preserves_recording(self):
        """Sync updates must not clobber the recording lifecycle."""
        manager = StateManager()
        manager.transition(TrayState.RECORDING, current_capture="w")
        manager.set_sync_state(SyncState.OFFLINE)

        assert manager.current.state == TrayState.RECORDING
        assert manager.current.current_capture == "w"
        assert manager.current.sync_state == SyncState.OFFLINE

    def test_set_break_count_clamps_and_notifies(self):
        """Break count updates clamp to >=0 and notify listeners once."""
        manager = StateManager()
        received = []
        manager.add_listener(lambda s: received.append(s.break_count))

        manager.set_break_count(3)
        manager.set_break_count(3)  # no change → no extra notification
        manager.set_break_count(-5)  # clamps to 0

        assert manager.current.break_count == 0
        assert received == [3, 0]

    def test_reset_preserves_sync_channel(self):
        """reset() returns recording to IDLE but keeps the sync channel."""
        manager = StateManager()
        manager.set_sync_state(SyncState.SYNCING)
        manager.transition(TrayState.RECORDING, current_capture="w")
        manager.reset()

        assert manager.current.state == TrayState.IDLE
        assert manager.current.current_capture is None
        assert manager.current.sync_state == SyncState.SYNCING

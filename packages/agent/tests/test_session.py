"""Tests for session management."""

import tempfile
from pathlib import Path

import pytest

from openadapt_agent.session import Session, SessionManager, SessionState


class TestSession:
    """Tests for the Session class."""

    def test_create_session(self):
        """Test creating a new session."""
        session = Session(
            session_id="test-123",
            goal="Test goal",
        )
        assert session.session_id == "test-123"
        assert session.goal == "Test goal"
        assert session.state == SessionState.CREATED
        assert session.step_count == 0

    def test_session_lifecycle(self):
        """Test session state transitions."""
        session = Session(session_id="test-123", goal="Test")

        # Start
        assert session.state == SessionState.CREATED
        session.start()
        assert session.state == SessionState.RUNNING
        assert session.started_at is not None

        # Pause
        session.pause()
        assert session.state == SessionState.PAUSED

        # Resume
        session.resume()
        assert session.state == SessionState.RUNNING

        # Complete
        session.complete(success=True, reason="Done")
        assert session.state == SessionState.COMPLETED
        assert session.success is True
        assert session.completion_reason == "Done"
        assert session.completed_at is not None

    def test_session_stop(self):
        """Test stopping a session."""
        session = Session(session_id="test-123", goal="Test")
        session.start()
        session.stop(reason="User cancelled")

        assert session.state == SessionState.STOPPED
        assert session.success is False
        assert session.completion_reason == "User cancelled"

    def test_session_serialization(self):
        """Test session to_dict and from_dict."""
        session = Session(
            session_id="test-123",
            goal="Test goal",
            metadata={"key": "value"},
        )
        session.start()

        data = session.to_dict()
        assert data["session_id"] == "test-123"
        assert data["goal"] == "Test goal"
        assert data["state"] == "running"
        assert data["metadata"] == {"key": "value"}

        restored = Session.from_dict(data)
        assert restored.session_id == session.session_id
        assert restored.goal == session.goal
        assert restored.state == session.state

    def test_invalid_state_transitions(self):
        """Test that invalid state transitions raise errors."""
        session = Session(session_id="test-123", goal="Test")

        # Can't pause before starting
        with pytest.raises(RuntimeError):
            session.pause()

        # Can't resume before pausing
        session.start()
        with pytest.raises(RuntimeError):
            session.resume()

        # Can't start twice
        with pytest.raises(RuntimeError):
            session.start()


class TestSessionManager:
    """Tests for the SessionManager class."""

    def test_create_session(self):
        """Test creating a session via manager."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SessionManager(session_dir=Path(tmpdir))
            session = manager.create_session(goal="Test goal")

            assert session.state == SessionState.RUNNING
            assert session.goal == "Test goal"
            assert (Path(tmpdir) / f"{session.session_id}.json").exists()

    def test_save_and_load_session(self):
        """Test saving and loading a session."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SessionManager(session_dir=Path(tmpdir))
            session = manager.create_session(goal="Test goal")
            session_id = session.session_id

            # Modify and save
            session.pause()
            manager.save_session(session)

            # Load and verify
            loaded = manager.load_session(session_id)
            assert loaded.session_id == session_id
            assert loaded.state == SessionState.PAUSED

    def test_list_sessions(self):
        """Test listing sessions."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SessionManager(session_dir=Path(tmpdir))

            # Create multiple sessions
            s1 = manager.create_session(goal="Goal 1")
            s2 = manager.create_session(goal="Goal 2")
            s2.pause()
            manager.save_session(s2)

            # List all
            sessions = manager.list_sessions()
            assert len(sessions) == 2

            # List by state
            running = manager.list_sessions(state=SessionState.RUNNING)
            assert len(running) == 1
            assert running[0].session_id == s1.session_id

            paused = manager.list_sessions(state=SessionState.PAUSED)
            assert len(paused) == 1
            assert paused[0].session_id == s2.session_id

    def test_delete_session(self):
        """Test deleting a session."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SessionManager(session_dir=Path(tmpdir))
            session = manager.create_session(goal="Test")
            session_id = session.session_id

            # Verify exists
            assert (Path(tmpdir) / f"{session_id}.json").exists()

            # Delete
            manager.delete_session(session_id)
            assert not (Path(tmpdir) / f"{session_id}.json").exists()

    def test_load_nonexistent_session(self):
        """Test loading a session that doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SessionManager(session_dir=Path(tmpdir))

            with pytest.raises(FileNotFoundError):
                manager.load_session("nonexistent-id")

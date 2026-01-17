"""Tests for the AgentExecutor."""

import tempfile
from pathlib import Path

import pytest

from openadapt_agent.executor import (
    AgentExecutor,
    Action,
    ActionType,
    Observation,
)
from openadapt_agent.config import AgentConfig, SafetyMode, ConfirmationMode
from openadapt_agent.session import SessionState


class MockPolicy:
    """Mock policy for testing."""

    def __init__(self, actions=None):
        self.actions = actions or []
        self.action_index = 0
        self.reset_count = 0

    def predict(self, observation, goal, history=None):
        if self.action_index < len(self.actions):
            action = self.actions[self.action_index]
            self.action_index += 1
            return action
        return Action(type=ActionType.DONE)

    def reset(self):
        self.action_index = 0
        self.reset_count += 1


class TestAgentExecutor:
    """Tests for the AgentExecutor class."""

    def test_create_executor(self):
        """Test creating an executor."""
        config = AgentConfig(safety_mode=SafetyMode.DISABLED)
        executor = AgentExecutor(config=config)

        assert executor.config.safety_mode == SafetyMode.DISABLED
        assert executor.current_session is None

    def test_start_session(self):
        """Test starting a session."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = AgentConfig(
                safety_mode=SafetyMode.DISABLED,
                session_dir=Path(tmpdir),
            )
            executor = AgentExecutor(config=config)

            session = executor.start_session(goal="Test goal")

            assert session is not None
            assert session.goal == "Test goal"
            assert session.state == SessionState.RUNNING
            assert executor.current_session == session

    def test_step_with_policy(self):
        """Test executing a step with a policy."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = AgentConfig(
                safety_mode=SafetyMode.DISABLED,
                session_dir=Path(tmpdir),
            )

            actions = [
                Action(type=ActionType.CLICK, x=0.5, y=0.5),
                Action(type=ActionType.DONE),
            ]
            policy = MockPolicy(actions=actions)
            executor = AgentExecutor(policy=policy, config=config)

            executor.start_session(goal="Test")

            # First step - click
            result = executor.step()
            assert result.success
            assert result.action.type == ActionType.CLICK

            # Second step - done
            result = executor.step()
            assert result.success
            assert result.action.type == ActionType.DONE
            assert executor.current_session.is_complete

    def test_step_with_explicit_action(self):
        """Test executing a step with an explicit action."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = AgentConfig(
                safety_mode=SafetyMode.DISABLED,
                session_dir=Path(tmpdir),
            )
            executor = AgentExecutor(config=config)
            executor.start_session(goal="Test")

            action = Action(type=ActionType.TYPE, text="hello")
            result = executor.step(action=action)

            assert result.success
            assert result.action.type == ActionType.TYPE
            assert result.action.text == "hello"

    def test_basic_safety_check(self):
        """Test basic safety checks without ML module."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = AgentConfig(
                safety_mode=SafetyMode.STANDARD,
                confirmation_mode=ConfirmationMode.AUTO_DENY,
                session_dir=Path(tmpdir),
                use_ml_safety=False,  # Don't try to load ML module
            )
            executor = AgentExecutor(config=config)
            executor.start_session(goal="Test")

            # Action with sensitive keyword should require confirmation
            action = Action(type=ActionType.TYPE, text="my password is secret123")
            result = executor.step(action=action)

            # Should fail because AUTO_DENY denies confirmations
            assert not result.success
            assert "not confirmed" in result.error.lower()

    def test_session_pause_resume(self):
        """Test pausing and resuming a session."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = AgentConfig(
                safety_mode=SafetyMode.DISABLED,
                session_dir=Path(tmpdir),
            )
            executor = AgentExecutor(config=config)
            session = executor.start_session(goal="Test")
            session_id = session.session_id

            # Pause
            executor.pause()
            assert executor.current_session.state == SessionState.PAUSED

            # Resume
            executor.resume_session(session_id)
            assert executor.current_session.state == SessionState.RUNNING

    def test_max_steps_limit(self):
        """Test that max steps limit is enforced."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = AgentConfig(
                safety_mode=SafetyMode.DISABLED,
                session_dir=Path(tmpdir),
            )
            config.execution.max_steps = 2

            # Policy that never says DONE
            actions = [Action(type=ActionType.WAIT, amount=0.1)] * 10
            policy = MockPolicy(actions=actions)
            executor = AgentExecutor(policy=policy, config=config)

            executor.start_session(goal="Test")

            # Execute steps until limit
            executor.step()
            executor.step()
            result = executor.step()  # Should hit limit

            assert not result.success
            assert "max steps" in result.error.lower()

    def test_no_session_error(self):
        """Test that step() fails without a session."""
        config = AgentConfig(safety_mode=SafetyMode.DISABLED)
        executor = AgentExecutor(config=config)

        with pytest.raises(RuntimeError, match="No active session"):
            executor.step()

    def test_stop_session(self):
        """Test stopping a session."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = AgentConfig(
                safety_mode=SafetyMode.DISABLED,
                session_dir=Path(tmpdir),
            )
            executor = AgentExecutor(config=config)
            executor.start_session(goal="Test")

            executor.stop(success=False, reason="Test stop")

            assert executor.current_session is None

    def test_policy_reset_on_session_start(self):
        """Test that policy is reset when starting a new session."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = AgentConfig(
                safety_mode=SafetyMode.DISABLED,
                session_dir=Path(tmpdir),
            )
            policy = MockPolicy(actions=[Action(type=ActionType.DONE)])
            executor = AgentExecutor(policy=policy, config=config)

            executor.start_session(goal="Test 1")
            assert policy.reset_count == 1

            executor.stop()
            executor.start_session(goal="Test 2")
            assert policy.reset_count == 2

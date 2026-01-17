"""
Session management for OpenAdapt Agent execution.

Provides session creation, persistence, pause/resume, and history tracking.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional
import json
import logging
import uuid


logger = logging.getLogger(__name__)


class SessionState(str, Enum):
    """States a session can be in."""
    CREATED = "created"      # Session created but not started
    RUNNING = "running"      # Session is actively executing
    PAUSED = "paused"        # Session is paused and can be resumed
    COMPLETED = "completed"  # Session finished successfully
    FAILED = "failed"        # Session finished with failure
    STOPPED = "stopped"      # Session manually stopped


@dataclass
class SessionStep:
    """A single step in a session's execution history."""
    step_number: int
    observation: Any  # Observation object or serialized dict
    action: Any       # Action object or serialized dict
    timestamp: datetime = field(default_factory=datetime.now)
    result: Optional[dict] = None


@dataclass
class Session:
    """An execution session for the agent.

    Sessions track the goal, state, and execution history of an agent run.
    They can be paused, resumed, and persisted for later continuation.

    Attributes:
        session_id: Unique identifier for this session
        goal: The task goal the agent is trying to accomplish
        state: Current state of the session
        created_at: When the session was created
        started_at: When execution started
        completed_at: When execution completed
        steps: List of execution steps
        metadata: Additional session metadata
    """
    session_id: str
    goal: str
    state: SessionState = SessionState.CREATED
    created_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    steps: list[SessionStep] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    completion_reason: Optional[str] = None
    success: Optional[bool] = None

    @property
    def step_count(self) -> int:
        """Number of steps executed in this session."""
        return len(self.steps)

    @property
    def is_complete(self) -> bool:
        """Whether the session has finished (successfully or not)."""
        return self.state in (
            SessionState.COMPLETED,
            SessionState.FAILED,
            SessionState.STOPPED,
        )

    @property
    def is_active(self) -> bool:
        """Whether the session is currently active (running or paused)."""
        return self.state in (SessionState.RUNNING, SessionState.PAUSED)

    @property
    def duration_seconds(self) -> Optional[float]:
        """Duration of the session in seconds."""
        if not self.started_at:
            return None
        end_time = self.completed_at or datetime.now()
        return (end_time - self.started_at).total_seconds()

    @property
    def history(self) -> list[tuple[Any, Any]]:
        """Get the observation-action history for policy context."""
        return [(step.observation, step.action) for step in self.steps]

    def start(self) -> None:
        """Start the session execution."""
        if self.state != SessionState.CREATED:
            raise RuntimeError(f"Cannot start session in state: {self.state}")
        self.state = SessionState.RUNNING
        self.started_at = datetime.now()
        logger.debug(f"Session {self.session_id} started")

    def pause(self) -> None:
        """Pause the session."""
        if self.state != SessionState.RUNNING:
            raise RuntimeError(f"Cannot pause session in state: {self.state}")
        self.state = SessionState.PAUSED
        logger.debug(f"Session {self.session_id} paused")

    def resume(self) -> None:
        """Resume a paused session."""
        if self.state != SessionState.PAUSED:
            raise RuntimeError(f"Cannot resume session in state: {self.state}")
        self.state = SessionState.RUNNING
        logger.debug(f"Session {self.session_id} resumed")

    def complete(self, success: bool, reason: Optional[str] = None) -> None:
        """Mark the session as completed.

        Args:
            success: Whether the session completed successfully
            reason: Optional reason for completion
        """
        self.state = SessionState.COMPLETED if success else SessionState.FAILED
        self.completed_at = datetime.now()
        self.success = success
        self.completion_reason = reason
        logger.debug(f"Session {self.session_id} completed: success={success}")

    def stop(self, reason: Optional[str] = None) -> None:
        """Stop the session manually."""
        self.state = SessionState.STOPPED
        self.completed_at = datetime.now()
        self.success = False
        self.completion_reason = reason or "Manually stopped"
        logger.debug(f"Session {self.session_id} stopped")

    def add_step(self, observation: Any, action: Any, result: Optional[dict] = None) -> None:
        """Add a step to the session history.

        Args:
            observation: The observation at this step
            action: The action taken
            result: Optional result/outcome of the action
        """
        step = SessionStep(
            step_number=len(self.steps),
            observation=observation,
            action=action,
            result=result,
        )
        self.steps.append(step)

    def to_dict(self) -> dict:
        """Serialize the session to a dictionary."""
        return {
            "session_id": self.session_id,
            "goal": self.goal,
            "state": self.state.value,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "step_count": self.step_count,
            "metadata": self.metadata,
            "completion_reason": self.completion_reason,
            "success": self.success,
            # Note: steps are serialized separately for large sessions
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        """Deserialize a session from a dictionary."""
        return cls(
            session_id=data["session_id"],
            goal=data["goal"],
            state=SessionState(data["state"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            started_at=datetime.fromisoformat(data["started_at"]) if data.get("started_at") else None,
            completed_at=datetime.fromisoformat(data["completed_at"]) if data.get("completed_at") else None,
            metadata=data.get("metadata", {}),
            completion_reason=data.get("completion_reason"),
            success=data.get("success"),
        )


class SessionManager:
    """Manages session creation, persistence, and retrieval.

    Sessions are stored as JSON files in the configured session directory.
    """

    def __init__(
        self,
        session_dir: Optional[Path] = None,
        auto_save_interval: float = 10.0,
    ):
        """Initialize the SessionManager.

        Args:
            session_dir: Directory for session storage.
                        Defaults to ~/.openadapt/agent/sessions
            auto_save_interval: Interval in seconds for auto-saving sessions
        """
        self.session_dir = session_dir or Path.home() / ".openadapt" / "agent" / "sessions"
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.auto_save_interval = auto_save_interval

        logger.debug(f"SessionManager initialized with session_dir={self.session_dir}")

    def create_session(
        self,
        goal: str,
        session_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> Session:
        """Create a new session.

        Args:
            goal: The task goal for this session
            session_id: Optional custom session ID (generates UUID if not provided)
            metadata: Optional session metadata

        Returns:
            The new Session object (already started)
        """
        session_id = session_id or str(uuid.uuid4())
        session = Session(
            session_id=session_id,
            goal=goal,
            metadata=metadata or {},
        )
        session.start()
        self.save_session(session)

        logger.info(f"Created session {session_id}")
        return session

    def save_session(self, session: Session) -> None:
        """Save a session to disk.

        Args:
            session: The session to save
        """
        session_file = self.session_dir / f"{session.session_id}.json"

        # Save session metadata
        with open(session_file, "w") as f:
            json.dump(session.to_dict(), f, indent=2)

        # Save steps separately if there are many
        if session.steps:
            steps_file = self.session_dir / f"{session.session_id}_steps.json"
            steps_data = []
            for step in session.steps:
                step_dict = {
                    "step_number": step.step_number,
                    "timestamp": step.timestamp.isoformat(),
                    "result": step.result,
                    # observation and action need custom serialization
                }
                steps_data.append(step_dict)

            with open(steps_file, "w") as f:
                json.dump(steps_data, f, indent=2)

        logger.debug(f"Saved session {session.session_id}")

    def load_session(self, session_id: str) -> Session:
        """Load a session from disk.

        Args:
            session_id: ID of the session to load

        Returns:
            The loaded Session object

        Raises:
            FileNotFoundError: If the session doesn't exist
        """
        session_file = self.session_dir / f"{session_id}.json"

        if not session_file.exists():
            raise FileNotFoundError(f"Session not found: {session_id}")

        with open(session_file) as f:
            data = json.load(f)

        session = Session.from_dict(data)

        # Load steps if available
        steps_file = self.session_dir / f"{session_id}_steps.json"
        if steps_file.exists():
            with open(steps_file) as f:
                steps_data = json.load(f)
            # TODO: Properly deserialize steps with observations and actions

        logger.debug(f"Loaded session {session_id}")
        return session

    def list_sessions(
        self,
        state: Optional[SessionState] = None,
        limit: int = 100,
    ) -> list[Session]:
        """List available sessions.

        Args:
            state: Optional filter by session state
            limit: Maximum number of sessions to return

        Returns:
            List of Session objects
        """
        sessions = []

        for session_file in sorted(
            self.session_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            # Skip step files
            if "_steps" in session_file.name:
                continue

            try:
                with open(session_file) as f:
                    data = json.load(f)
                session = Session.from_dict(data)

                if state is None or session.state == state:
                    sessions.append(session)

                if len(sessions) >= limit:
                    break

            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Failed to load session {session_file}: {e}")
                continue

        return sessions

    def delete_session(self, session_id: str) -> None:
        """Delete a session from disk.

        Args:
            session_id: ID of the session to delete
        """
        session_file = self.session_dir / f"{session_id}.json"
        steps_file = self.session_dir / f"{session_id}_steps.json"

        if session_file.exists():
            session_file.unlink()
        if steps_file.exists():
            steps_file.unlink()

        logger.info(f"Deleted session {session_id}")

    def get_active_sessions(self) -> list[Session]:
        """Get all active (running or paused) sessions."""
        active = []
        active.extend(self.list_sessions(state=SessionState.RUNNING))
        active.extend(self.list_sessions(state=SessionState.PAUSED))
        return active

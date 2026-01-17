"""
AgentExecutor - Production execution engine for GUI automation agents.

The AgentExecutor wraps trained models with safety gates, human-in-the-loop
confirmation, and audit logging to enable safe production automation.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional, Protocol
import logging

from openadapt_agent.config import AgentConfig, SafetyMode, ConfirmationMode
from openadapt_agent.session import Session, SessionManager, SessionState


logger = logging.getLogger(__name__)


class ActionType(str, Enum):
    """Types of actions the agent can execute."""
    CLICK = "click"
    TYPE = "type"
    KEY = "key"
    SCROLL = "scroll"
    WAIT = "wait"
    DONE = "done"


@dataclass
class Action:
    """An action to be executed by the agent.

    Attributes:
        type: The type of action (click, type, key, scroll, wait, done)
        x: X coordinate for click actions (0.0-1.0 normalized)
        y: Y coordinate for click actions (0.0-1.0 normalized)
        text: Text for type actions
        key: Key name for key actions
        direction: Direction for scroll actions
        amount: Amount for scroll actions
        target_element: Optional element identifier for SoM-style actions
        metadata: Additional action metadata
    """
    type: ActionType
    x: Optional[float] = None
    y: Optional[float] = None
    text: Optional[str] = None
    key: Optional[str] = None
    direction: Optional[str] = None
    amount: Optional[float] = None
    target_element: Optional[str] = None
    metadata: dict = field(default_factory=dict)


@dataclass
class Observation:
    """An observation of the current screen state.

    Attributes:
        screenshot_path: Path to the screenshot image
        screenshot_base64: Base64 encoded screenshot (alternative to path)
        accessibility_tree: Accessibility tree data if available
        window_title: Title of the active window
        timestamp: When the observation was captured
        metadata: Additional observation metadata
    """
    screenshot_path: Optional[str] = None
    screenshot_base64: Optional[str] = None
    accessibility_tree: Optional[dict] = None
    window_title: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: dict = field(default_factory=dict)


@dataclass
class ExecutionResult:
    """Result of executing an action.

    Attributes:
        success: Whether the action executed successfully
        action: The action that was executed
        observation_before: Observation before action execution
        observation_after: Observation after action execution
        error: Error message if execution failed
        blocked: Whether the action was blocked by safety checks
        confirmed: Whether the action required and received confirmation
        timestamp: When the execution completed
        duration_ms: How long the execution took
    """
    success: bool
    action: Action
    observation_before: Optional[Observation] = None
    observation_after: Optional[Observation] = None
    error: Optional[str] = None
    blocked: bool = False
    confirmed: bool = False
    timestamp: datetime = field(default_factory=datetime.now)
    duration_ms: Optional[float] = None


class PolicyProtocol(Protocol):
    """Protocol for agent policies that predict actions from observations."""

    def predict(
        self,
        observation: Observation,
        goal: str,
        history: Optional[list[tuple[Observation, Action]]] = None,
    ) -> Action:
        """Predict the next action given an observation and goal."""
        ...

    def reset(self) -> None:
        """Reset the policy state between tasks."""
        ...


class AgentExecutor:
    """Production execution engine for GUI automation agents.

    The AgentExecutor wraps a trained policy with:
    - Safety validation before action execution
    - Human-in-the-loop confirmation for risky actions
    - Session management with pause/resume capability
    - Comprehensive audit logging

    Example:
        ```python
        from openadapt_agent import AgentExecutor, AgentConfig

        # Create executor with policy
        config = AgentConfig(safety_mode=SafetyMode.STANDARD)
        executor = AgentExecutor(policy=my_policy, config=config)

        # Start a new session
        session = executor.start_session(goal="Open Notepad and type hello")

        # Run until completion or max steps
        while not session.is_complete:
            result = executor.step()
            if not result.success:
                print(f"Step failed: {result.error}")
                break

        # Get results
        print(f"Session completed: {session.state}")
        ```
    """

    def __init__(
        self,
        policy: Optional[PolicyProtocol] = None,
        config: Optional[AgentConfig] = None,
        confirmation_callback: Optional[Callable[[Action, str], bool]] = None,
    ):
        """Initialize the AgentExecutor.

        Args:
            policy: The agent policy for predicting actions. If None, executor
                   operates in manual mode where actions must be provided explicitly.
            config: Agent configuration. Uses defaults if not provided.
            confirmation_callback: Custom callback for action confirmation.
                                  Receives (action, reason) and returns bool.
        """
        self.policy = policy
        self.config = config or AgentConfig()
        self.confirmation_callback = confirmation_callback

        self._session_manager = SessionManager(
            session_dir=self.config.session_dir,
            auto_save_interval=self.config.auto_save_interval_seconds,
        )
        self._current_session: Optional[Session] = None
        self._safety_validator = self._init_safety_validator()

        logger.info(
            f"AgentExecutor initialized with safety_mode={self.config.safety_mode.value}"
        )

    def _init_safety_validator(self):
        """Initialize the safety validator if available."""
        if not self.config.use_ml_safety:
            return None

        try:
            from openadapt_ml.safety import SafetyValidator, SafetyConfig

            # Map our safety modes to openadapt-ml safety modes
            mode_mapping = {
                SafetyMode.DISABLED: "DISABLED",
                SafetyMode.PERMISSIVE: "PERMISSIVE",
                SafetyMode.STANDARD: "STANDARD",
                SafetyMode.STRICT: "STRICT",
                SafetyMode.PARANOID: "PARANOID",
            }

            ml_config = SafetyConfig(mode=mode_mapping[self.config.safety_mode])
            return SafetyValidator(ml_config)

        except ImportError:
            logger.warning(
                "openadapt-ml safety module not available. "
                "Install with: pip install openadapt-agent[ml]"
            )
            return None

    @property
    def current_session(self) -> Optional[Session]:
        """Get the current active session."""
        return self._current_session

    def start_session(
        self,
        goal: str,
        session_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> Session:
        """Start a new execution session.

        Args:
            goal: The task goal to accomplish
            session_id: Optional custom session ID
            metadata: Optional session metadata

        Returns:
            The new Session object
        """
        if self._current_session and self._current_session.state == SessionState.RUNNING:
            raise RuntimeError("A session is already running. Pause or stop it first.")

        self._current_session = self._session_manager.create_session(
            goal=goal,
            session_id=session_id,
            metadata=metadata,
        )

        if self.policy:
            self.policy.reset()

        logger.info(f"Started session {self._current_session.session_id} for goal: {goal}")
        return self._current_session

    def resume_session(self, session_id: str) -> Session:
        """Resume a paused session.

        Args:
            session_id: ID of the session to resume

        Returns:
            The resumed Session object
        """
        self._current_session = self._session_manager.load_session(session_id)
        self._current_session.resume()

        logger.info(f"Resumed session {session_id}")
        return self._current_session

    def step(
        self,
        action: Optional[Action] = None,
        observation: Optional[Observation] = None,
    ) -> ExecutionResult:
        """Execute one step of the agent.

        If action is not provided and a policy is configured, the policy
        will predict the next action from the current observation.

        Args:
            action: Optional action to execute (uses policy if not provided)
            observation: Optional current observation (captures if not provided)

        Returns:
            ExecutionResult with the outcome of the step
        """
        if not self._current_session:
            raise RuntimeError("No active session. Call start_session() first.")

        if self._current_session.state != SessionState.RUNNING:
            raise RuntimeError(f"Session is not running: {self._current_session.state}")

        # Check step limit
        if self._current_session.step_count >= self.config.execution.max_steps:
            self._current_session.complete(success=False, reason="Max steps reached")
            return ExecutionResult(
                success=False,
                action=action or Action(type=ActionType.DONE),
                error="Max steps reached",
            )

        # Capture observation if not provided
        if observation is None:
            observation = self._capture_observation()

        # Get action from policy if not provided
        if action is None:
            if not self.policy:
                raise RuntimeError("No action provided and no policy configured")
            action = self.policy.predict(
                observation=observation,
                goal=self._current_session.goal,
                history=self._current_session.history,
            )

        # Check for DONE action
        if action.type == ActionType.DONE:
            self._current_session.complete(success=True, reason="Agent signaled DONE")
            return ExecutionResult(
                success=True,
                action=action,
                observation_before=observation,
            )

        # Validate action with safety module
        validation_result = self._validate_action(action, observation)
        if validation_result.get("blocked"):
            self._log_blocked_action(action, validation_result.get("reason", "Unknown"))
            return ExecutionResult(
                success=False,
                action=action,
                observation_before=observation,
                blocked=True,
                error=f"Action blocked: {validation_result.get('reason')}",
            )

        # Request confirmation if required
        if validation_result.get("requires_confirmation"):
            confirmed = self._request_confirmation(
                action, validation_result.get("reason", "Action requires confirmation")
            )
            if not confirmed:
                return ExecutionResult(
                    success=False,
                    action=action,
                    observation_before=observation,
                    error="Action not confirmed by user",
                )

        # Execute the action
        start_time = datetime.now()
        try:
            self._execute_action(action)
            observation_after = self._capture_observation()
            duration_ms = (datetime.now() - start_time).total_seconds() * 1000

            # Record in session
            self._current_session.add_step(observation, action)

            return ExecutionResult(
                success=True,
                action=action,
                observation_before=observation,
                observation_after=observation_after,
                confirmed=validation_result.get("requires_confirmation", False),
                duration_ms=duration_ms,
            )

        except Exception as e:
            logger.error(f"Action execution failed: {e}")
            return ExecutionResult(
                success=False,
                action=action,
                observation_before=observation,
                error=str(e),
            )

    def pause(self) -> None:
        """Pause the current session."""
        if self._current_session:
            self._current_session.pause()
            self._session_manager.save_session(self._current_session)
            logger.info(f"Paused session {self._current_session.session_id}")

    def stop(self, success: bool = False, reason: Optional[str] = None) -> None:
        """Stop the current session.

        Args:
            success: Whether the session completed successfully
            reason: Optional reason for stopping
        """
        if self._current_session:
            self._current_session.complete(success=success, reason=reason or "User stopped")
            self._session_manager.save_session(self._current_session)
            logger.info(f"Stopped session {self._current_session.session_id}")
            self._current_session = None

    def _capture_observation(self) -> Observation:
        """Capture the current screen observation.

        Override this method to provide custom observation capture.
        """
        # TODO: Integrate with openadapt-capture for real observation capture
        return Observation(
            timestamp=datetime.now(),
            metadata={"source": "placeholder"},
        )

    def _validate_action(self, action: Action, observation: Observation) -> dict:
        """Validate an action using the safety module.

        Args:
            action: The action to validate
            observation: The current observation

        Returns:
            Dict with keys: allowed, blocked, requires_confirmation, reason
        """
        if self.config.safety_mode == SafetyMode.DISABLED:
            return {"allowed": True, "blocked": False, "requires_confirmation": False}

        if self._safety_validator:
            # Use openadapt-ml safety validator
            # TODO: Convert action/observation to expected format
            # result = self._safety_validator.validate(action, observation)
            # return {
            #     "allowed": result.allowed,
            #     "blocked": not result.allowed and not result.requires_confirmation,
            #     "requires_confirmation": result.requires_confirmation,
            #     "reason": result.reason,
            # }
            pass

        # Fallback: Basic safety checks
        return self._basic_safety_check(action)

    def _basic_safety_check(self, action: Action) -> dict:
        """Basic safety checks when openadapt-ml is not available."""
        # Type actions with sensitive keywords
        if action.type == ActionType.TYPE and action.text:
            sensitive_keywords = ["password", "secret", "key", "token", "sudo", "rm -rf"]
            text_lower = action.text.lower()
            for keyword in sensitive_keywords:
                if keyword in text_lower:
                    return {
                        "allowed": True,
                        "blocked": False,
                        "requires_confirmation": True,
                        "reason": f"Text contains sensitive keyword: {keyword}",
                    }

        return {"allowed": True, "blocked": False, "requires_confirmation": False}

    def _request_confirmation(self, action: Action, reason: str) -> bool:
        """Request human confirmation for an action.

        Args:
            action: The action requiring confirmation
            reason: Why confirmation is needed

        Returns:
            True if confirmed, False otherwise
        """
        mode = self.config.confirmation_mode

        if mode == ConfirmationMode.AUTO_ALLOW:
            logger.warning("Auto-allowing action (dangerous mode enabled)")
            return True

        if mode == ConfirmationMode.AUTO_DENY:
            logger.info("Auto-denying action (testing mode)")
            return False

        if mode == ConfirmationMode.CALLBACK and self.confirmation_callback:
            return self.confirmation_callback(action, reason)

        if mode == ConfirmationMode.CLI:
            return self._cli_confirm(action, reason)

        if mode == ConfirmationMode.GUI:
            # TODO: Integrate with openadapt-tray for GUI confirmation
            logger.warning("GUI confirmation not implemented, falling back to CLI")
            return self._cli_confirm(action, reason)

        return False

    def _cli_confirm(self, action: Action, reason: str) -> bool:
        """CLI-based confirmation prompt."""
        print(f"\n{'='*60}")
        print("ACTION CONFIRMATION REQUIRED")
        print(f"{'='*60}")
        print(f"Action: {action.type.value}")
        if action.x is not None and action.y is not None:
            print(f"Position: ({action.x:.3f}, {action.y:.3f})")
        if action.text:
            print(f"Text: {action.text[:50]}{'...' if len(action.text) > 50 else ''}")
        print(f"Reason: {reason}")
        print(f"{'='*60}")

        try:
            response = input("Allow this action? [y/N]: ").strip().lower()
            return response in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False

    def _execute_action(self, action: Action) -> None:
        """Execute an action on the system.

        Override this method to provide custom action execution.
        """
        # TODO: Integrate with pyautogui or platform-specific action execution
        logger.info(f"Executing action: {action.type.value}")

        if action.type == ActionType.CLICK:
            # TODO: pyautogui.click(x, y)
            pass
        elif action.type == ActionType.TYPE:
            # TODO: pyautogui.typewrite(action.text)
            pass
        elif action.type == ActionType.KEY:
            # TODO: pyautogui.press(action.key)
            pass
        elif action.type == ActionType.SCROLL:
            # TODO: pyautogui.scroll(action.amount)
            pass
        elif action.type == ActionType.WAIT:
            import time
            time.sleep(action.amount or 1.0)

    def _log_blocked_action(self, action: Action, reason: str) -> None:
        """Log a blocked action for audit purposes."""
        logger.warning(f"Blocked action: {action.type.value} - {reason}")

        if self.config.audit.enabled:
            # TODO: Write to audit log
            pass

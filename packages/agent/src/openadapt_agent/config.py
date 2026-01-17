"""
Configuration for the OpenAdapt Agent execution engine.

Provides settings for safety modes, execution parameters, and integration options.
"""

from enum import Enum
from typing import Optional
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class SafetyMode(str, Enum):
    """Safety enforcement modes for agent execution.

    These modes control how strictly the agent enforces safety checks
    before executing actions.
    """
    DISABLED = "disabled"      # No safety checks (testing only)
    PERMISSIVE = "permissive"  # Log warnings but allow most actions
    STANDARD = "standard"      # Block dangerous, confirm irreversible
    STRICT = "strict"          # Block suspicious, confirm most actions
    PARANOID = "paranoid"      # Block everything not explicitly allowed


class ConfirmationMode(str, Enum):
    """How to handle actions requiring human confirmation."""
    CLI = "cli"                # Command-line prompts
    GUI = "gui"                # Desktop GUI dialogs (requires openadapt-tray)
    CALLBACK = "callback"      # Custom callback function
    QUEUE = "queue"            # Queue for batch processing
    AUTO_DENY = "auto_deny"    # Automatically deny (for testing)
    AUTO_ALLOW = "auto_allow"  # Automatically allow (dangerous!)


class ExecutionConfig(BaseModel):
    """Configuration for action execution."""

    max_steps: int = Field(
        default=100,
        description="Maximum steps per session before automatic stop"
    )
    step_timeout_seconds: float = Field(
        default=30.0,
        description="Timeout for individual action execution"
    )
    retry_count: int = Field(
        default=3,
        description="Number of retries for failed actions"
    )
    retry_delay_seconds: float = Field(
        default=1.0,
        description="Delay between retries"
    )
    screenshot_on_error: bool = Field(
        default=True,
        description="Capture screenshot when errors occur"
    )


class AuditConfig(BaseModel):
    """Configuration for audit logging."""

    enabled: bool = Field(
        default=True,
        description="Enable audit logging"
    )
    log_dir: Optional[Path] = Field(
        default=None,
        description="Directory for audit logs (default: ~/.openadapt/agent/logs)"
    )
    include_screenshots: bool = Field(
        default=True,
        description="Include screenshots in audit logs"
    )
    retention_days: int = Field(
        default=30,
        description="Days to retain audit logs"
    )


class AgentConfig(BaseSettings):
    """Main configuration for the OpenAdapt Agent.

    Settings can be loaded from environment variables with the OPENADAPT_AGENT_ prefix.
    Example: OPENADAPT_AGENT_SAFETY_MODE=strict
    """

    model_config = {"env_prefix": "OPENADAPT_AGENT_"}

    # Safety settings
    safety_mode: SafetyMode = Field(
        default=SafetyMode.STANDARD,
        description="Safety enforcement mode"
    )
    confirmation_mode: ConfirmationMode = Field(
        default=ConfirmationMode.CLI,
        description="How to handle confirmation requests"
    )
    confirmation_timeout_seconds: float = Field(
        default=60.0,
        description="Timeout waiting for human confirmation"
    )

    # Execution settings
    execution: ExecutionConfig = Field(
        default_factory=ExecutionConfig,
        description="Action execution configuration"
    )

    # Audit settings
    audit: AuditConfig = Field(
        default_factory=AuditConfig,
        description="Audit logging configuration"
    )

    # Session settings
    session_dir: Optional[Path] = Field(
        default=None,
        description="Directory for session persistence (default: ~/.openadapt/agent/sessions)"
    )
    auto_save_interval_seconds: float = Field(
        default=10.0,
        description="Interval for auto-saving session state"
    )

    # Integration settings
    use_ml_safety: bool = Field(
        default=True,
        description="Use safety module from openadapt-ml if available"
    )

    @classmethod
    def from_file(cls, path: Path) -> "AgentConfig":
        """Load configuration from a YAML or JSON file."""
        import json

        if path.suffix in (".yaml", ".yml"):
            try:
                import yaml
                with open(path) as f:
                    data = yaml.safe_load(f)
            except ImportError:
                raise ImportError("PyYAML required for YAML config files: pip install pyyaml")
        else:
            with open(path) as f:
                data = json.load(f)

        return cls(**data)


# Default configuration instance
default_config = AgentConfig()

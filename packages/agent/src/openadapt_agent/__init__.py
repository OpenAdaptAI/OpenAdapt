"""
OpenAdapt Agent - Production execution engine for GUI automation agents.

This package provides the runtime infrastructure for executing trained OpenAdapt
models in production environments with safety gates, human-in-the-loop confirmation,
session management, and audit logging.
"""

__version__ = "0.1.0"

from openadapt_agent.executor import AgentExecutor
from openadapt_agent.session import Session, SessionManager, SessionState
from openadapt_agent.config import AgentConfig, SafetyMode

__all__ = [
    "AgentExecutor",
    "Session",
    "SessionManager",
    "SessionState",
    "AgentConfig",
    "SafetyMode",
]

# Optional imports from openadapt-ml safety module
try:
    from openadapt_ml.safety import (
        SafetyValidator,
        SafetyConfig,
        ValidationDecision,
        PatternCategory,
    )
    __all__.extend([
        "SafetyValidator",
        "SafetyConfig",
        "ValidationDecision",
        "PatternCategory",
    ])
    _HAS_SAFETY = True
except ImportError:
    _HAS_SAFETY = False


def has_safety_module() -> bool:
    """Check if the openadapt-ml safety module is available."""
    return _HAS_SAFETY

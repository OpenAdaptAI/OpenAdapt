"""OpenAdapt Tray - System tray application for OpenAdapt."""

__version__ = "0.1.0"

from openadapt_tray.app import TrayApplication, main
from openadapt_tray.state import TrayState, AppState, StateManager
from openadapt_tray.config import TrayConfig

__all__ = [
    "TrayApplication",
    "TrayState",
    "AppState",
    "StateManager",
    "TrayConfig",
    "main",
]

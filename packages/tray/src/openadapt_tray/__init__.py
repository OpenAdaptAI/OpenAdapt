"""OpenAdapt Tray - System tray application for OpenAdapt."""

__version__ = "0.1.1"

from openadapt_tray.app import TrayApplication, main
from openadapt_tray.state import TrayState, SyncState, AppState, StateManager
from openadapt_tray.config import TrayConfig
from openadapt_tray.hosted import HostedPoller, CountResult

__all__ = [
    "TrayApplication",
    "TrayState",
    "SyncState",
    "AppState",
    "StateManager",
    "TrayConfig",
    "HostedPoller",
    "CountResult",
    "main",
]

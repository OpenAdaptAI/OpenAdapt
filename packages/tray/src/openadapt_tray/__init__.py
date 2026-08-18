"""OpenAdapt Tray - System tray application for OpenAdapt."""

__version__ = "0.3.2"

from openadapt_tray.app import TrayApplication, main
from openadapt_tray.config import TrayConfig
from openadapt_tray.hosted import CountResult, HostedPoller
from openadapt_tray.state import (
    AppState,
    CredentialState,
    CredentialStatus,
    StateManager,
    SyncState,
    TrayState,
)

__all__ = [
    "AppState",
    "CountResult",
    "CredentialState",
    "CredentialStatus",
    "HostedPoller",
    "StateManager",
    "SyncState",
    "TrayApplication",
    "TrayConfig",
    "TrayState",
    "main",
]

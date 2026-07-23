"""Backend implementations of the `openadapt_flow.backend.Backend` protocol.

Backends are re-exported lazily so that importing this package does not
require their dependencies (playwright, requests) unless a backend is
actually used.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from openadapt_flow.backends.citrix_workspace import CitrixWorkspaceBackend
    from openadapt_flow.backends.linux_backend import LinuxBackend
    from openadapt_flow.backends.macos_backend import MacOSBackend
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.backends.remote_display import RemoteDisplayBackend
    from openadapt_flow.backends.windows_backend import WindowsBackend

__all__ = [
    "CitrixWorkspaceBackend",
    "MacOSBackend",
    "LinuxBackend",
    "PlaywrightBackend",
    "RemoteDisplayBackend",
    "WindowsBackend",
]


def __getattr__(name: str) -> object:
    if name == "CitrixWorkspaceBackend":
        from openadapt_flow.backends.citrix_workspace import (
            CitrixWorkspaceBackend,
        )

        return CitrixWorkspaceBackend
    if name == "MacOSBackend":
        from openadapt_flow.backends.macos_backend import MacOSBackend

        return MacOSBackend
    if name == "LinuxBackend":
        from openadapt_flow.backends.linux_backend import LinuxBackend

        return LinuxBackend
    if name == "PlaywrightBackend":
        from openadapt_flow.backends.playwright_backend import (
            PlaywrightBackend,
        )

        return PlaywrightBackend
    if name == "WindowsBackend":
        from openadapt_flow.backends.windows_backend import WindowsBackend

        return WindowsBackend
    if name == "RemoteDisplayBackend":
        from openadapt_flow.backends.remote_display import RemoteDisplayBackend

        return RemoteDisplayBackend
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

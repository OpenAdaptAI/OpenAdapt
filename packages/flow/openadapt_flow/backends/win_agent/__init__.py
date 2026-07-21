"""In-guest Windows agent server (the interactive-session shim).

The single public entry point is :func:`create_server`, a stdlib-only HTTP
server that runs INSIDE the Windows VM's *interactive* desktop session
(session 1) and exposes exactly the two endpoints ``WindowsBackend`` calls:

    GET  /screenshot        -> raw PNG bytes of the live desktop
    POST /execute_windows   -> exec() of bare Python (pyautogui / uiautomation)

Plus a ``GET /health`` liveness probe. See ``server.py`` for the full
contract, the session-0 rationale, and the loopback/bearer-token hardening.

Importing this package has NO heavy side effects (only stdlib at module load;
mss/PIL/pyautogui import lazily inside the request handlers), so CI on
macOS/Linux imports and mock-tests it freely without a Windows-only stack.
"""

from __future__ import annotations

from openadapt_flow.backends.win_agent.server import (
    AgentConfig,
    create_server,
    make_handler_class,
)
from openadapt_flow.backends.win_agent.tls import (
    CertBundle,
    fingerprint_from_pem_file,
    generate_self_signed_cert,
    normalize_fingerprint,
    pinned_session,
)

__all__ = [
    "AgentConfig",
    "CertBundle",
    "create_server",
    "fingerprint_from_pem_file",
    "generate_self_signed_cert",
    "make_handler_class",
    "normalize_fingerprint",
    "pinned_session",
]

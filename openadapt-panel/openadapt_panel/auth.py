"""Per-session loopback token auth for the control panel.

The panel triggers real system actions (recording, training, eval), so even on
127.0.0.1 every ``/api`` route is gated by a token minted at launch. The token
is handed to the browser via the initial URL (``/?token=...``); the SPA then
sends it as the ``X-Panel-Token`` header on fetches and as a ``?token=`` query
param on EventSource (SSE) connections, which cannot set headers.
"""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, status

TOKEN_HEADER = "X-Panel-Token"
TOKEN_QUERY = "token"


def generate_token() -> str:
    return secrets.token_urlsafe(32)


async def require_token(request: Request) -> None:
    """FastAPI dependency: reject requests without the session token.

    Auth is skipped entirely when ``app.state.enable_auth`` is False (used by
    tests). The expected token lives on ``app.state.panel_token``.
    """
    if not getattr(request.app.state, "enable_auth", True):
        return

    expected = getattr(request.app.state, "panel_token", None)
    provided = request.headers.get(TOKEN_HEADER) or request.query_params.get(
        TOKEN_QUERY
    )
    if not expected or not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid panel token.",
        )

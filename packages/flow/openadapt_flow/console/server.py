"""Serve the capability-authenticated operator console on loopback only.

The bind address is hardcoded to ``127.0.0.1``. Each launch generates an
unguessable bearer capability delivered in a URL fragment, which browsers do
not send in HTTP requests or access logs.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Optional

from openadapt_flow.console.decision_supervisor import DecisionSupervisorThread
from openadapt_flow.runtime.durable.attended_service import AttendedActionService

#: The only address the console ever binds. Not configurable.
LOOPBACK_HOST = "127.0.0.1"

DEFAULT_PORT = 7863


def serve(
    bundles_root: Path | str,
    runs_root: Path | str,
    skills_root: Path | str | None = None,
    *,
    allow_actions: bool = False,
    attend: bool = False,
    attended_service: Optional[AttendedActionService] = None,
    decision_supervisor: Optional[DecisionSupervisorThread] = None,
    port: int = DEFAULT_PORT,
) -> None:
    """Build the app and serve it on ``http://127.0.0.1:<port>`` (blocking).

    Args:
        decision_supervisor: When given, the outbound decision lane runs beside
            the server for as long as it serves. The console process is the
            right host for it because it already owns the deployment-bound
            action service a continuation needs, and because
            ``execute_attended_action`` takes a single-flight lease over the
            pause -- so an answer from a phone and one from this browser cannot
            both execute.
    """
    import uvicorn

    from openadapt_flow.console.app import create_app

    access_token = secrets.token_urlsafe(32)
    app = create_app(
        bundles_root,
        runs_root,
        skills_root,
        allow_actions=allow_actions,
        attend=attend,
        access_token=access_token,
        attended_service=attended_service,
    )
    # URL fragments are consumed entirely by the browser and are never sent in
    # HTTP requests or uvicorn access logs.  The UI removes the fragment before
    # routing and keeps the capability in sessionStorage only.
    print(
        "Open this private console URL in your browser:\n"
        f"  http://{LOOPBACK_HOST}:{port}/#token={access_token}"
    )
    if decision_supervisor is not None:
        decision_supervisor.start()
    try:
        uvicorn.run(app, host=LOOPBACK_HOST, port=port, log_level="info")
    finally:
        if decision_supervisor is not None:
            decision_supervisor.stop()

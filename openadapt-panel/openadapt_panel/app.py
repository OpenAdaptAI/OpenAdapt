"""FastAPI application factory for the control panel.

All ``/api`` routes are gated by the per-session token (auth.require_token) and
mounted from ``openadapt_panel.routes``. Route handlers import sibling packages
lazily inside their bodies, so the app boots cleanly headless and with zero
siblings installed. The static SPA is served unauthenticated (it carries no
data; everything sensitive comes from the token-gated API).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .auth import generate_token, require_token
from .jobs import JobManager
from .routes import captures, evals, jobs, models, settings, system, train

STATIC_DIR = Path(__file__).parent / "static"


def create_app(*, token: Optional[str] = None, enable_auth: bool = True) -> FastAPI:
    app = FastAPI(title="OpenAdapt Control Panel", version=__version__)
    app.state.panel_token = token or generate_token()
    app.state.enable_auth = enable_auth
    app.state.jobs = JobManager()

    # Token-gated API.
    api_deps = [Depends(require_token)]
    for module in (system, captures, evals, train, jobs, models, settings):
        app.include_router(module.router, prefix="/api", dependencies=api_deps)

    # Unauthenticated liveness check.
    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    # Static SPA.
    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app

"""FastAPI application factory for the control panel.

P0 exposes a single read-only endpoint (``/api/system``) and serves the static
frontend. Route handlers that touch sibling packages (Captures/Train/Eval, in
later phases) must import those siblings **inside the handler body**, never at
module top, so the app boots cleanly headless and with zero siblings installed.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .system import system_report

STATIC_DIR = Path(__file__).parent / "static"


def create_app() -> FastAPI:
    app = FastAPI(title="OpenAdapt Control Panel", version=__version__)

    @app.get("/api/system")
    def api_system() -> dict:
        """Read-only ecosystem/system status. Imports no sibling packages."""
        return system_report()

    @app.get("/api/health")
    def api_health() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    # Static assets (CSS/JS/built React output) live alongside index.html.
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app

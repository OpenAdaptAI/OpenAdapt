"""API endpoints for the dashboard."""


from pathlib import Path
import os

from fastapi import APIRouter, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
import uvicorn

from openadapt.app.dashboard.api.action_events import ActionEventsAPI
from openadapt.app.dashboard.api.recordings import RecordingsAPI
from openadapt.app.dashboard.api.scrubbing import ScrubbingAPI
from openadapt.app.dashboard.api.settings import SettingsAPI
from openadapt.build_utils import is_running_from_executable
from openadapt.config import config

app = FastAPI()

api = APIRouter()

action_events_app = ActionEventsAPI().attach_routes()
recordings_app = RecordingsAPI().attach_routes()
scrubbing_app = ScrubbingAPI().attach_routes()
settings_app = SettingsAPI().attach_routes()

api.include_router(action_events_app, prefix="/action-events")
api.include_router(recordings_app, prefix="/recordings")
api.include_router(scrubbing_app, prefix="/scrubbing")
api.include_router(settings_app, prefix="/settings")

app.include_router(api, prefix="/api")


def run_app() -> None:
    """Run the dashboard."""
    if is_running_from_executable():
        build_directory = Path(__file__).parent.parent / "out"

        def add_route(path: str) -> None:
            """Add a route to the dashboard."""

            def route() -> FileResponse:
                return FileResponse(build_directory / path)

            stripped_path = f'/{path.replace(".html", "")}'
            logger.info(f"Adding route: {stripped_path}")
            app.get(stripped_path)(route)

        for root, _, files in os.walk(build_directory):
            for file in files:
                if file.endswith(".html"):
                    path = os.path.relpath(os.path.join(root, file), build_directory)
                    add_route(path)

        app.mount("/", StaticFiles(directory=build_directory), name="static")

    uvicorn.run(app, port=config.DASHBOARD_SERVER_PORT)

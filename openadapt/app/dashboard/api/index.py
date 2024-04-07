"""API endpoints for the dashboard."""


from pathlib import Path
import os

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from openadapt.app.dashboard.api.recordings import RecordingsAPI
from openadapt.app.dashboard.api.settings import SettingsAPI
from openadapt.config import config

app = FastAPI()

api = FastAPI()

RecordingsAPI(api).attach_routes()
SettingsAPI(api).attach_routes()

app.mount("/api", api)


def run_app():
    if config.ENV == "build":
        build_directory = Path(__file__).parent.parent / "out"

        def add_route(path: str):
            def route():
                return FileResponse(build_directory / path)

            stripped_path = f'/{path.replace(".html", "")}'
            logger.info(f"Adding route: {stripped_path}")
            app.get(stripped_path)(route)

        for root, dirs, files in os.walk(build_directory):
            for file in files:
                if file.endswith(".html"):
                    path = os.path.relpath(os.path.join(root, file), build_directory)
                    add_route(path)

        app.mount("/", StaticFiles(directory=build_directory), name="static")

    import uvicorn

    uvicorn.run(app, port=config.DASHBOARD_SERVER_PORT)

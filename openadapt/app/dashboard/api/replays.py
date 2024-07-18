"""API endpoints for replays."""

from typing import Any, Literal

from fastapi import APIRouter

from openadapt.config import Config, config, persist_config
from openadapt.db import crud
from openadapt.utils import image2utf8


class ReplaysAPI:
    """API endpoints for replays."""

    def __init__(self) -> None:
        """Initialize the ReplaysAPI class."""
        self.app = APIRouter()

    def attach_routes(self) -> APIRouter:
        """Attach routes to the FastAPI app."""
        self.app.add_api_route("", self.get_replays, methods=["GET"])
        self.app.add_api_route(
            "/{replay_id}/logs", self.get_replay_logs, methods=["GET"]
        )
        return self.app

    @staticmethod
    def get_replays():
        """Get all replays."""
        session = crud.get_new_session(read_only=True)
        return crud.get_replays(session)

    @staticmethod
    def get_replay_logs(replay_id: int):
        """Get all logs for a replay."""
        session = crud.get_new_session(read_only=True)
        logs = crud.get_replay_logs(session, replay_id)
        unpickled_logs = []
        import base64
        import pickle

        def binary_to_base64_uri(binary_data):
            base64_data = base64.b64encode(binary_data).decode("utf-8")
            base64_uri = f"data:image/png;base64,{base64_data}"
            return base64_uri

        for log in logs:
            log.data = pickle.loads(log.data)
            if log.key == "screenshot":
                log.data = image2utf8(log.data)
            unpickled_logs.append(log)
        return unpickled_logs

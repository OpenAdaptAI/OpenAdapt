"""API endpoints for replays."""
import pickle

from fastapi import APIRouter, WebSocket
from loguru import logger

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
        self.replay_logs_route()
        return self.app

    @staticmethod
    def get_replays() -> list[dict]:
        """Get all replays."""
        session = crud.get_new_session(read_only=True)
        return crud.get_replays(session)

    def replay_logs_route(self) -> None:
        """Add the replay detail route as a websocket."""

        @self.app.websocket("/{replay_id}/logs")
        async def get_replay_logs(websocket: WebSocket, replay_id: int) -> None:
            """Get a specific replay and its logs."""
            await websocket.accept()
            session = crud.get_new_session(read_only=True)

            replay = crud.get_replay(session, replay_id)

            await websocket.send_json({"type": "replay", "value": replay.asdict()})

            logs = crud.get_replay_logs(session, replay_id)

            await websocket.send_json({"type": "num_logs", "value": len(logs)})

            for log in logs:
                log.data = pickle.loads(log.data)
                if log.key == "screenshot":
                    log.data = image2utf8(log.data)
                log_dict = log.asdict()
                if log.key == "window_event":
                    log_dict["data"] = log_dict["data"].asdict()
                if log.key == "action_event_dict":
                    log_dict["data"]["reducer_names"] = list(
                        log_dict["data"]["reducer_names"]
                    )
                if log.key == "segmentation":
                    log_dict["data"] = log_dict["data"].asdict()
                try:
                    await websocket.send_json({"type": "log", "value": log_dict})
                except Exception as e:
                    logger.error(f"Error sending log: {e}")
                    logger.info(log_dict["data"])
                    logger.info(log_dict["key"])

            await websocket.close()

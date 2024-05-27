"""API endpoints for recordings."""

from fastapi import APIRouter, WebSocket
from loguru import logger

from openadapt.app import cards
from openadapt.db import crud
from openadapt.events import get_events
from openadapt.models import Recording
from openadapt.utils import display_event, image2utf8, row2dict


class RecordingsAPI:
    """API endpoints for recordings."""

    def __init__(self) -> None:
        """Initialize the RecordingsAPI class."""
        self.app = APIRouter()

    def attach_routes(self) -> APIRouter:
        """Attach routes to the FastAPI app."""
        self.app.add_api_route("", self.get_recordings, response_model=None)
        self.app.add_api_route(
            "/scrubbed", self.get_scrubbed_recordings, response_model=None
        )
        self.app.add_api_route("/start", self.start_recording)
        self.app.add_api_route("/stop", self.stop_recording)
        self.app.add_api_route("/status", self.recording_status)
        self.recording_detail_route()
        return self.app

    @staticmethod
    def get_recordings() -> dict[str, list[Recording]]:
        """Get all recordings."""
        session = crud.get_new_session()
        recordings = crud.get_all_recordings(session)
        return {"recordings": recordings}

    @staticmethod
    def get_scrubbed_recordings() -> dict[str, list[Recording]]:
        """Get all scrubbed recordings."""
        session = crud.get_new_session()
        recordings = crud.get_all_scrubbed_recordings(session)
        return {"recordings": recordings}

    @staticmethod
    async def start_recording() -> dict[str, str]:
        """Start a recording session."""
        await crud.acquire_db_lock()
        cards.quick_record()
        return {"message": "Recording started"}

    @staticmethod
    def stop_recording() -> dict[str, str]:
        """Stop a recording session."""
        cards.stop_record()
        crud.release_db_lock()
        return {"message": "Recording stopped"}

    @staticmethod
    def recording_status() -> dict[str, bool]:
        """Get the recording status."""
        return {"recording": cards.is_recording()}

    def recording_detail_route(self) -> None:
        """Add the recording detail route as a websocket."""

        @self.app.websocket("/{recording_id}")
        async def get_recording_detail(websocket: WebSocket, recording_id: int) -> None:
            """Get a specific recording and its action events."""
            await websocket.accept()
            session = crud.get_new_session()
            with session:
                recording = crud.get_recording_by_id(recording_id, session)

                await websocket.send_json(
                    {"type": "recording", "value": recording.asdict()}
                )

                action_events = get_events(recording, session=session)

                await websocket.send_json(
                    {"type": "num_events", "value": len(action_events)}
                )

                def convert_to_str(event_dict: dict) -> dict:
                    """Convert the keys to strings."""
                    if "key" in event_dict:
                        event_dict["key"] = str(event_dict["key"])
                    if "canonical_key" in event_dict:
                        event_dict["canonical_key"] = str(event_dict["canonical_key"])
                    if "reducer_names" in event_dict:
                        event_dict["reducer_names"] = list(event_dict["reducer_names"])
                    if "children" in event_dict:
                        for child_event in event_dict["children"]:
                            convert_to_str(child_event)

                for action_event in action_events:
                    event_dict = row2dict(action_event)
                    try:
                        image = display_event(action_event)
                        width, height = image.size
                        image = image2utf8(image)
                    except Exception:
                        logger.info("Failed to display event")
                        image = None
                        width, height = 0, 0
                    event_dict["screenshot"] = image
                    event_dict["dimensions"] = {"width": width, "height": height}

                    convert_to_str(event_dict)
                    await websocket.send_json(
                        {"type": "action_event", "value": event_dict}
                    )

                await websocket.close()

"""API endpoints for recordings."""

from fastapi import FastAPI, WebSocket
from loguru import logger

from openadapt.app.cards import is_recording, quick_record, stop_record
from openadapt.db import crud
from openadapt.events import get_events
from openadapt.models import Recording
from openadapt.utils import display_event, image2utf8, row2dict


class RecordingsAPI:
    """API endpoints for recordings."""

    def __init__(self, app: FastAPI) -> None:
        """Initialize the RecordingsAPI class."""
        self.app = app

    def attach_routes(self) -> None:
        """Attach routes to the FastAPI app."""
        self.app.add_api_route("/recordings", self.get_recordings, response_model=None)
        self.app.add_api_route("/recordings/start", self.start_recording)
        self.app.add_api_route("/recordings/stop", self.stop_recording)
        self.app.add_api_route("/recordings/status", self.recording_status)
        self.recording_detail_route()

    @staticmethod
    def get_recordings() -> dict[str, list[Recording]]:
        """Get all recordings."""
        session = crud.get_new_session()
        with session:
            recordings = crud.get_all_recordings(session)
            return {"recordings": recordings}

    @staticmethod
    async def start_recording() -> dict[str, str]:
        """Start a recording session."""
        await crud.acquire_db_lock()
        quick_record()
        return {"message": "Recording started"}

    @staticmethod
    async def stop_recording() -> dict[str, str]:
        """Stop a recording session."""
        stop_record()
        await crud.release_db_lock()
        return {"message": "Recording stopped"}

    @staticmethod
    def recording_status() -> dict[str, bool]:
        """Get the recording status."""
        return {"recording": is_recording()}

    def recording_detail_route(self) -> None:
        """Add the recording detail route as a websocket."""

        @self.app.websocket("/recordings/{recording_id}")
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
                    if event_dict["key"]:
                        event_dict["key"] = str(event_dict["key"])
                    if event_dict["canonical_key"]:
                        event_dict["canonical_key"] = str(event_dict["canonical_key"])
                    if event_dict["reducer_names"]:
                        event_dict["reducer_names"] = list(event_dict["reducer_names"])
                    await websocket.send_json(
                        {"type": "action_event", "value": event_dict}
                    )

                await websocket.close()

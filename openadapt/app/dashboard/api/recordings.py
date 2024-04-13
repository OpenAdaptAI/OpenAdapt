"""API endpoints for recordings."""

from fastapi import FastAPI

from openadapt.app.cards import is_recording, quick_record, stop_record
from openadapt.db import crud
from openadapt.events import get_events
from openadapt.models import Recording
from openadapt.utils import display_event, image2utf8, row2dict

from loguru import logger


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
        self.app.add_api_route("/api/recordings/{recording_id}", response_model=None)

    @staticmethod
    def get_recordings() -> dict[str, list[Recording]]:
        """Get all recordings."""
        recordings = crud.get_all_recordings()
        return {"recordings": recordings}

    @staticmethod
    def start_recording() -> dict[str, str]:
        """Start a recording session."""
        quick_record()
        return {"message": "Recording started"}

    @staticmethod
    def stop_recording() -> dict[str, str]:
        """Stop a recording session."""
        stop_record()
        return {"message": "Recording stopped"}

    @staticmethod
    def recording_status() -> dict[str, bool]:
        """Get the recording status."""
        return {"recording": is_recording()}
    
    @staticmethod
    def get_recording(recording_id: int) -> dict[str, Recording]:
        """Get a specific recording and its action events."""
        crud.new_session()

        recording = crud.get_recording_by_id(recording_id)

        action_events = get_events(recording)
        event_dicts = []

        for action_event in action_events:
            event_dict = row2dict(action_event)
            try:
                image = image2utf8(display_event(action_event))
            except Exception as e:
                logger.exception("Failed to display event: {}", e)
                image = None
            event_dict["screenshot"] = image
            event_dicts.append(event_dict)

        return {"recording": recording, "action_events": event_dicts}

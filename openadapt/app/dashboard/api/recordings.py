from fastapi import FastAPI

from openadapt.app.cards import is_recording, quick_record, stop_record
from openadapt.db import crud
from openadapt.models import Recording


class RecordingsAPI:
    def __init__(self, app: FastAPI):
        self.app = app

    def attach_routes(self):
        self.app.add_api_route(
            "/api/recordings", self.get_recordings, response_model=None
        )
        self.app.add_api_route("/api/recordings/start", self.start_recording)
        self.app.add_api_route("/api/recordings/stop", self.stop_recording)
        self.app.add_api_route("/api/recordings/status", self.recording_status)

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

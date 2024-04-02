"""API endpoints for the dashboard."""


from fastapi import FastAPI

from openadapt.app.dashboard.api.recordings import RecordingsAPI
from openadapt.app.dashboard.api.settings import SettingsAPI

app = FastAPI()

RecordingsAPI(app).attach_routes()
SettingsAPI(app).attach_routes()


# @app.get("/api/recordings", response_model=None)
# def get_recordings() -> dict[str, list[Recording]]:
#     """Get all recordings."""
#     recordings = crud.get_all_recordings()
#     return {"recordings": recordings}


# @app.get("/api/recordings/start")
# def start_recording() -> dict[str, str]:
#     """Start a recording session."""
#     quick_record()
#     return {"message": "Recording started"}


# @app.get("/api/recordings/stop")
# def stop_recording() -> dict[str, str]:
#     """Stop a recording session."""
#     stop_record()
#     return {"message": "Recording stopped"}


# @app.get("/api/recordings/status")
# def recording_status() -> dict[str, bool]:
#     """Get the recording status."""
#     return {"recording": is_recording()}

"""API endpoints for the dashboard."""


from fastapi import FastAPI
import sqlalchemy as sa

from openadapt.app.cards import is_recording, quick_record, stop_record
from openadapt.db.db import Session
from openadapt.models import Recording

app = FastAPI()
db = Session()


def get_all_recordings() -> list[Recording]:
    """Get all recordings.

    Returns:
        list[Recording]: A list of all recordings.
    """
    return db.query(Recording).order_by(sa.desc(Recording.timestamp)).all()


@app.get("/api/recordings", response_model=None)
def get_recordings() -> dict[str, list[Recording]]:
    """Get all recordings."""
    recordings = get_all_recordings()
    return {"recordings": recordings}


@app.get("/api/recordings/start")
def start_recording() -> dict[str, str]:
    """Start a recording session."""
    quick_record()
    return {"message": "Recording started"}


@app.get("/api/recordings/stop")
def stop_recording() -> dict[str, str]:
    """Stop a recording session."""
    stop_record()
    return {"message": "Recording stopped"}


@app.get("/api/recordings/status")
def recording_status() -> dict[str, bool]:
    """Get the recording status."""
    return {"recording": is_recording()}

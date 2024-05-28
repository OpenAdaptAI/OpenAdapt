"""API endpoints for recordings."""

from fastapi import APIRouter
from loguru import logger

from openadapt.db import crud


class ActionEventsAPI:
    """API endpoints for action events."""

    def __init__(self) -> None:
        """Initialize the ActionEventsAPI class."""
        self.app = APIRouter()

    def attach_routes(self) -> APIRouter:
        """Attach routes to the FastAPI app."""
        self.app.add_api_route("/{event_id}", self.disable_event, methods=["DELETE"])
        return self.app

    @staticmethod
    def disable_event(event_id: int) -> dict[str, str]:
        """Disable an action event.

        Args:
            event_id (int): The ID of the event to disable.

        Returns:
            dict: The response message and status code.
        """
        if not crud.acquire_db_lock():
            return {"message": "Database is locked", "status": "error"}
        session = crud.get_new_session(read_and_write=True)
        try:
            crud.disable_action_event(session, event_id)
        except Exception as e:
            logger.error(f"Error deleting event: {e}")
            session.rollback()
            crud.release_db_lock()
            return {"message": "Error deleting event", "status": "error"}
        crud.release_db_lock()
        return {"message": "Event deleted", "status": "success"}

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
    def disable_event(event_id: str) -> dict[str, str]:
        """Disable an action event.

        Args:
            event_id (int): The ID of the event to disable.

        Returns:
            dict: The response message and status code.
        """
        Delete_events = []
        if "œ" in event_id:
            try:
                event_range = event_id.split("œ") 
                for event_ids in event_range:
                    event_ids = event_ids.strip()
                    if event_ids != "" : 
                        Delete_events.append( int(event_ids) )
            except Exception as e:
                logger.error(f"Error deleting event: {e}") 
                return {"message": "Error deleting event", "status": "error"}
        else : 
            try :
                event_id = event_id.strip()
                if event_id != "" : 
                    Delete_events.append(int(event_id) )
            except Exception as e:
                logger.error(f"Error deleting event: {e}") 
                return {"message": "Error deleting event", "status": "error"}
        #
        if not crud.acquire_db_lock():
            return {"message": "Database is locked", "status": "error"}
        session = crud.get_new_session(read_and_write=True)
        try:
            for event_id in Delete_events : 
                crud.disable_action_event(session, event_id)
        except Exception as e:
            logger.error(f"Error deleting event: {e}")
            session.rollback()
            crud.release_db_lock()
            return {"message": "Error deleting event", "status": "error"}
        crud.release_db_lock()
        return {"message": "Event deleted", "status": "success"}

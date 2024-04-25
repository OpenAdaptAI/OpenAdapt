"""API endpoints for scrubbing."""

import json
import time

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from openadapt.config import config
from openadapt.db import crud
from openadapt.privacy.providers import ScrubProvider
from openadapt.scrub import get_scrubbing_process, scrub


class ScrubbingAPI:
    """API endpoints for scrubbing."""

    def __init__(self) -> None:
        """Initialize the ScrubbingAPI class."""
        self.app = APIRouter()

    def attach_routes(self) -> APIRouter:
        """Attach routes to the FastAPI router."""
        self.app.add_api_route("/providers", self.get_scrubbing_provider_options)
        self.app.add_api_route(
            "/scrub/{recording_id}/{provider_id}",
            self.scrub_recording,
            methods=["POST"],
        )
        self.app.add_api_route("/status", self.get_scrubbing_status)
        self.app.add_api_route("/updates", self.get_scrubbing_updates)
        return self.app

    @staticmethod
    def get_scrubbing_provider_options() -> dict[str, str]:
        """Get the scrubbing provider options.

        Returns:
            dict[str, str]: The scrubbing provider options.
        """
        return ScrubProvider.as_options()

    @staticmethod
    async def scrub_recording(recording_id: int, provider_id: str) -> dict[str, str]:
        """Scrub a recording.

        Args:
            recording_id (int): The recording ID.
            provider_id (str): The provider ID.

        Returns:
            dict[str, str]: A dictionary with the status of the scrubbing.
        """
        if not config.SCRUB_ENABLED:
            return {"message": "Scrubbing is not enabled", "status": "failed"}
        scrubbing_proc = get_scrubbing_process()
        if scrubbing_proc.is_running():
            return {
                "message": "Scrubbing already in progress for a recording",
                "status": "failed",
            }
        if provider_id not in ScrubProvider.get_available_providers():
            return {"message": "Provider not supported", "status": "failed"}
        await crud.acquire_db_lock()
        scrub(recording_id, provider_id, release_lock=True)
        scrubbing_proc = get_scrubbing_process()
        while not scrubbing_proc.is_running():
            pass
        return {"message": "Scrubbing started", "status": "success"}

    @staticmethod
    def get_scrubbing_status() -> dict[str, bool]:
        """Get the status of the scrubbing process.

        Returns:
            dict[str, bool]: A dictionary with the status of the scrubbing process.
        """
        scrubbing_proc = get_scrubbing_process()
        return {"status": scrubbing_proc.is_running()}

    @staticmethod
    def get_scrubbing_updates() -> StreamingResponse:
        """Get the updates of the scrubbing process.

        Returns:
            StreamingResponse: A streaming response with the updates of the scrubbing process.
        """
        scrubbing_proc = get_scrubbing_process()

        def fetch_scrubbing_data():
            while scrubbing_proc.is_running():
                data = json.dumps(scrubbing_proc.fetch_updated_data())
                yield data
                time.sleep(1)
            yield json.dumps(scrubbing_proc.fetch_updated_data())

        return StreamingResponse(fetch_scrubbing_data())

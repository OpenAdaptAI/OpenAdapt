"""API endpoints for settings."""

from typing import Any, Literal

from fastapi import APIRouter

from openadapt.config import Config, config, persist_config


class SettingsAPI:
    """API endpoints for settings."""

    def __init__(self) -> None:
        """Initialize the SettingsAPI class."""
        self.app = APIRouter()

    def attach_routes(self) -> APIRouter:
        """Attach routes to the FastAPI app."""
        self.app.add_api_route("", self.get_settings, methods=["GET"])
        self.app.add_api_route("", self.set_settings, methods=["POST"])
        return self.app

    Category = Literal[
        "api_keys",
        "scrubbing",
        "record_and_replay",
        "general",
        "onboarding",
        "recording_upload",
    ]

    @staticmethod
    def get_settings(category: Category) -> dict[str, Any]:
        """Get all settings."""
        compact_settings = dict()
        for key in Config.classifications[category]:
            compact_settings[key] = getattr(config, key)
        return compact_settings

    @staticmethod
    def set_settings(body: Config) -> dict[str, str]:
        """Set settings."""
        persist_config(body)
        return {"status": "success"}

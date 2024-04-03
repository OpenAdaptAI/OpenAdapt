from typing import Any

from fastapi import FastAPI

from openadapt.config import Config, config, persist_config


class SettingsAPI:
    def __init__(self, app: FastAPI):
        self.app = app

    def attach_routes(self):
        self.app.add_api_route("/api/settings", self.get_settings, methods=["GET"])
        self.app.add_api_route("/api/settings", self.set_settings, methods=["POST"])

    @staticmethod
    def get_settings() -> dict[str, Any]:
        """Get all settings."""
        return config.model_dump()

    @staticmethod
    def set_settings(body: Config) -> dict[str, str]:
        """Set settings."""
        persist_config(body)
        return {"status": "success"}

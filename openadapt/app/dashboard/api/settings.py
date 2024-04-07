from typing import Any, Literal, Union

from fastapi import FastAPI

from openadapt.config import Config, config, persist_config


class SettingsAPI:
    def __init__(self, app: FastAPI):
        self.app = app

    def attach_routes(self):
        self.app.add_api_route("/settings", self.get_settings, methods=["GET"])
        self.app.add_api_route("/settings", self.set_settings, methods=["POST"])

    Category = Union[Literal["api_keys", "scrubbing", "record_and_replay"]]

    @staticmethod
    def get_settings(category: Category) -> dict[str, Any]:
        """Get all settings."""
        settings = config.model_dump()
        compact_settings = dict()
        for key in Config.classifications[category]:
            compact_settings[key] = settings[key]
        return compact_settings

    @staticmethod
    def set_settings(body: Config) -> dict[str, str]:
        """Set settings."""
        persist_config(body)
        return {"status": "success"}

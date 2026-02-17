"""Configuration management using pydantic-settings.

Loads settings from environment variables and .env file.
Includes all legacy OpenAdapt recording configuration values.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings

STOP_STRS = [
    "oa.stop",
]
SPECIAL_CHAR_STOP_SEQUENCES = [["ctrl", "ctrl", "ctrl"]]


class Settings(BaseSettings):
    """Application settings loaded from environment variables or .env file.

    Priority order for configuration values:
    1. Environment variables
    2. .env file
    3. Default values

    Recording config values are copied from legacy OpenAdapt config.py.
    """

    # API keys
    openai_api_key: str | None = None

    # Record and replay (from legacy OpenAdapt config.defaults.json)
    RECORD_WINDOW_DATA: bool = False
    RECORD_READ_ACTIVE_ELEMENT_STATE: bool = False
    RECORD_VIDEO: bool = True
    RECORD_AUDIO: bool = False
    RECORD_BROWSER_EVENTS: bool = False
    # if false, only write video events corresponding to screenshots
    RECORD_FULL_VIDEO: bool = False
    RECORD_IMAGES: bool = False
    # useful for debugging but expensive computationally
    LOG_MEMORY: bool = False
    VIDEO_ENCODING: str = "libx264"
    VIDEO_PIXEL_FORMAT: str = "yuv444p"
    # sequences that when typed, will stop the recording of ActionEvents
    STOP_SEQUENCES: list[list[str]] = [
        list(stop_str) for stop_str in STOP_STRS
    ] + SPECIAL_CHAR_STOP_SEQUENCES

    # Performance plotting
    PLOT_PERFORMANCE: bool = True

    # Browser Events Record (extension) configurations
    BROWSER_WEBSOCKET_SERVER_IP: str = "localhost"
    BROWSER_WEBSOCKET_PORT: int = 8765
    BROWSER_WEBSOCKET_MAX_SIZE: int = 2**22  # 4MB

    # Database
    DB_ECHO: bool = False

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",  # ignore extra env vars
    }


config = Settings()
# Keep backward-compatible alias
settings = config

"""Configuration management using pydantic-settings.

Loads settings from environment variables and .env file.
Includes all legacy OpenAdapt recording configuration values.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, fields

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


# ---------------------------------------------------------------------------
# RecordingConfig: user-facing overrides for Recorder constructor
# ---------------------------------------------------------------------------

# Mapping from RecordingConfig field names to Settings attribute names
_FIELD_TO_CONFIG_ATTR = {
    "capture_video": "RECORD_VIDEO",
    "capture_audio": "RECORD_AUDIO",
    "capture_images": "RECORD_IMAGES",
    "capture_window_data": "RECORD_WINDOW_DATA",
    "capture_browser_events": "RECORD_BROWSER_EVENTS",
    "capture_full_video": "RECORD_FULL_VIDEO",
    "video_encoding": "VIDEO_ENCODING",
    "video_pixel_format": "VIDEO_PIXEL_FORMAT",
    "stop_sequences": "STOP_SEQUENCES",
    "log_memory": "LOG_MEMORY",
    "plot_performance": "PLOT_PERFORMANCE",
}


@dataclass
class RecordingConfig:
    """User-facing recording options. ``None`` means 'use default from config'."""

    capture_video: bool | None = None
    capture_audio: bool | None = None
    capture_images: bool | None = None
    capture_window_data: bool | None = None
    capture_browser_events: bool | None = None
    capture_full_video: bool | None = None
    video_encoding: str | None = None
    video_pixel_format: str | None = None
    stop_sequences: list[list[str]] | None = None
    log_memory: bool | None = None
    plot_performance: bool | None = None


@contextmanager
def config_override(recording_config: RecordingConfig):
    """Temporarily override config settings from a RecordingConfig.

    Saves original values, applies non-None overrides, yields, then restores.
    """
    originals: dict[str, object] = {}
    for field in fields(recording_config):
        value = getattr(recording_config, field.name)
        if value is not None:
            config_attr = _FIELD_TO_CONFIG_ATTR[field.name]
            originals[config_attr] = getattr(config, config_attr)
            object.__setattr__(config, config_attr, value)
    try:
        yield
    finally:
        for config_attr, original_value in originals.items():
            object.__setattr__(config, config_attr, original_value)

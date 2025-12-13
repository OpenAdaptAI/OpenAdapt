"""Configuration management using pydantic-settings.

Loads settings from environment variables and .env file.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables or .env file.

    Priority order for configuration values:
    1. Environment variables
    2. .env file
    3. Default values (None for API keys)
    """

    openai_api_key: str | None = None

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",  # ignore extra env vars
    }


settings = Settings()

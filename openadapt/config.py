"""Shared configuration for OpenAdapt packages.

This module provides a unified configuration interface that loads settings
from environment variables and .env files.

Usage:
    from openadapt.config import settings

    # Access API keys
    anthropic_key = settings.anthropic_api_key

    # Access paths
    capture_dir = settings.capture_dir

    # Access model settings
    default_model = settings.default_model
"""

from pathlib import Path
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class OpenAdaptSettings(BaseSettings):
    """Unified settings for all OpenAdapt packages.

    Settings can be configured via:
    1. Environment variables (OPENADAPT_* prefix)
    2. .env file in current directory
    3. Default values

    Environment variable names are case-insensitive and use underscores.
    Example: OPENADAPT_CAPTURE_DIR or openadapt_capture_dir
    """

    model_config = SettingsConfigDict(
        env_prefix="OPENADAPT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # Ignore unknown env vars
    )

    # =========================================================================
    # API Keys (no prefix, for compatibility with other tools)
    # =========================================================================
    anthropic_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    google_api_key: Optional[str] = None
    lambda_api_key: Optional[str] = None

    # =========================================================================
    # Paths
    # =========================================================================
    # Default directory for capture recordings
    capture_dir: Path = Path.home() / ".openadapt" / "captures"

    # Default directory for training outputs
    training_output_dir: Path = Path.home() / ".openadapt" / "training"

    # Default directory for benchmark results
    benchmark_results_dir: Path = Path.home() / ".openadapt" / "benchmarks"

    # Model cache directory
    model_cache_dir: Path = Path.home() / ".openadapt" / "models"

    # Embedding cache directory
    embedding_cache_dir: Path = Path.home() / ".openadapt" / "embeddings"

    # =========================================================================
    # ML Settings
    # =========================================================================
    # Default model for training/inference
    default_model: str = "Qwen/Qwen3-VL-2B-Instruct"

    # Default device for inference
    default_device: str = "auto"  # "auto", "cuda", "mps", "cpu"

    # Default batch size for training
    default_batch_size: int = 1

    # Default learning rate
    default_learning_rate: float = 2e-5

    # =========================================================================
    # Capture Settings
    # =========================================================================
    # Enable audio recording by default
    capture_audio: bool = True

    # Enable Whisper transcription by default
    capture_transcribe: bool = False

    # Screenshot interval in seconds (0 for event-driven only)
    capture_screenshot_interval: float = 0.0

    # =========================================================================
    # Evaluation Settings
    # =========================================================================
    # Default max steps for evaluation
    eval_max_steps: int = 15

    # Default number of tasks for mock evaluation
    eval_default_tasks: int = 10

    # Default evaluation agent
    eval_default_agent: str = "api-claude"

    # =========================================================================
    # Server Settings
    # =========================================================================
    # Default server port
    server_port: int = 8080

    # Default server host
    server_host: str = "127.0.0.1"

    # Auto-open browser
    server_auto_open: bool = True

    # =========================================================================
    # Azure Settings (for cloud evaluation)
    # =========================================================================
    azure_subscription_id: Optional[str] = None
    azure_ml_resource_group: Optional[str] = None
    azure_ml_workspace_name: Optional[str] = None
    azure_docker_image: Optional[str] = None

    # =========================================================================
    # Grounding Settings
    # =========================================================================
    # OmniParser server URL
    omniparser_url: Optional[str] = None

    # UI-TARS server URL
    uitars_url: Optional[str] = None

    # =========================================================================
    # Retrieval Settings
    # =========================================================================
    # Default embedding dimension
    retrieval_embedding_dim: int = 512

    # Default number of results
    retrieval_top_k: int = 5

    def ensure_directories(self) -> None:
        """Create all configured directories if they don't exist."""
        for path in [
            self.capture_dir,
            self.training_output_dir,
            self.benchmark_results_dir,
            self.model_cache_dir,
            self.embedding_cache_dir,
        ]:
            path.mkdir(parents=True, exist_ok=True)

    def get_device(self) -> str:
        """Get the appropriate device for ML operations.

        Returns:
            Device string: "cuda", "mps", or "cpu"
        """
        if self.default_device != "auto":
            return self.default_device

        try:
            import torch
            if torch.cuda.is_available():
                return "cuda"
            elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                return "mps"
        except ImportError:
            pass

        return "cpu"


# Global settings instance
settings = OpenAdaptSettings()


# Convenience function to reload settings (useful for testing)
def reload_settings() -> OpenAdaptSettings:
    """Reload settings from environment and .env file.

    Returns:
        New OpenAdaptSettings instance.
    """
    global settings
    settings = OpenAdaptSettings()
    return settings

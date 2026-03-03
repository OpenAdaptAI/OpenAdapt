"""Engine configuration using pydantic-settings.

Settings are loaded from environment variables (prefixed with OPENADAPT_)
and validated at startup. The build profile (enterprise, community, full)
determines which storage backends are available.

Configuration validation chain (from design doc Section 7.6):
    1. Build includes backend? -> Hard error if mismatch.
    2. Credentials valid? -> Verify S3 bucket access, HF token, etc.
    3. Log the active configuration to audit.jsonl.

See design doc Section 7.6 for the full .env specification.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings


class EngineConfig(BaseSettings):
    """Configuration for the OpenAdapt Desktop engine.

    All settings can be overridden via environment variables with the
    OPENADAPT_ prefix (e.g., OPENADAPT_STORAGE_MODE=enterprise).
    """

    model_config = {"env_prefix": "OPENADAPT_"}

    # --- General ---
    data_dir: Path = Field(
        default=Path.home() / ".openadapt",
        description="Root directory for all OpenAdapt data (captures, archives, databases).",
    )
    log_level: str = Field(
        default="INFO",
        description="Logging level (DEBUG, INFO, WARNING, ERROR).",
    )

    # --- Storage ---
    storage_mode: str = Field(
        default="air-gapped",
        description=(
            "Storage mode: air-gapped (local only), enterprise (S3), "
            "community (HF Hub + R2), full (all backends)."
        ),
    )
    max_storage_gb: float = Field(
        default=50.0,
        description="Maximum disk usage in GB before automatic cleanup.",
    )
    retention_days: int = Field(
        default=30,
        description="How many days to retain captures before deletion.",
    )
    archive_after_hours: int = Field(
        default=24,
        description="Compress hot-tier captures after this many hours.",
    )

    # --- Recording ---
    recording_quality: str = Field(
        default="standard",
        description="Recording quality preset: low, standard, high, lossless.",
    )
    idle_fps: float = Field(
        default=0.1,
        description="Frame rate when user is idle (frames per second).",
    )
    active_fps: float = Field(
        default=10.0,
        description="Frame rate when user is active (frames per second).",
    )
    burst_fps: float = Field(
        default=30.0,
        description="Frame rate during burst capture (frames per second).",
    )

    # --- Upload ---
    upload_require_review: bool = Field(
        default=True,
        description="Require user review before any upload.",
    )
    upload_bandwidth_limit_mbps: float = Field(
        default=5.0,
        description="Maximum upload bandwidth in MB/s.",
    )
    upload_schedule: str = Field(
        default="idle",
        description="Upload schedule: idle, always, manual, or cron expression.",
    )

    # --- S3 (enterprise) ---
    s3_bucket: str = Field(default="", description="S3 bucket name.")
    s3_region: str = Field(default="us-east-1", description="AWS region for S3.")
    s3_access_key_id: str = Field(default="", description="AWS access key ID.")
    s3_secret_access_key: str = Field(default="", description="AWS secret access key.")
    s3_endpoint: str = Field(
        default="",
        description="Custom S3 endpoint (for MinIO, R2).",
    )

    # --- HuggingFace Hub (community) ---
    hf_repo: str = Field(
        default="OpenAdaptAI/desktop-recordings",
        description="HuggingFace dataset repository.",
    )
    hf_token: str = Field(default="", description="HuggingFace API token.")

    # --- Federated learning ---
    fl_enabled: bool = Field(default=False, description="Enable federated learning.")
    fl_server: str = Field(
        default="https://fl.openadapt.ai",
        description="Federated learning aggregation server URL.",
    )

    # --- Audit ---
    network_audit_log: bool = Field(
        default=True,
        description="Log all outbound network requests to audit.jsonl.",
    )
    audit_log_path: Path = Field(
        default=Path.home() / ".openadapt" / "audit.jsonl",
        description="Path to the network audit log file.",
    )
